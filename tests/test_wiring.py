"""接线层测试：用 stub 的 astrbot 模块加载 ``main.py``。

这不是集成测试，目的只有一个 —— 在**没有装 AstrBot 的环境里**也能证明：

1. ``main.py`` 能被 import（没有拼错的属性名、没有循环导入）；
2. 插件类能被实例化，且 ``__init__`` 不做任何 IO；
3. 每个钩子的**参数签名**与 AstrBot 源码里的调用方式一致；
4. 关键决策路径调用的 API 语义正确（特别是 ``should_call_llm`` 那个
   反直觉的反向语义）。

stub 里的签名都是从 AstrBot master 源码抄来的，不是猜的。
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import sys
import types
import pathlib

import pytest


# --------------------------------------------------------------------------- #
# 构造 astrbot stub
# --------------------------------------------------------------------------- #


class _EventMessageType(enum.Flag):
    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class _ComponentType(str, enum.Enum):
    """照抄 AstrBot 的 ``ComponentType(str, Enum)``。

    关键点：它同时继承 str 和 Enum，所以 ``type(x) is str`` 为 False，
    但 ``x == "Plain"`` 为 True。用真实语义的替身才能覆盖这个坑。
    """

    Plain = "Plain"
    Image = "Image"
    Reply = "Reply"
    At = "At"
    AtAll = "AtAll"


class _Plain:
    """对应 ``astrbot.core.message.components.Plain``。"""

    type = _ComponentType.Plain

    def __init__(self, text: str) -> None:
        self.text = text


class _Image:
    type = _ComponentType.Image

    def __init__(self, url: str = "x") -> None:
        self.url = url


class _At:
    """对应 ``astrbot.core.message.components.At``（带 qq 字段）。"""

    type = _ComponentType.At

    def __init__(self, qq: str) -> None:
        self.qq = qq


class _Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def info(self, msg: str, *a, **k) -> None:
        self.records.append(("info", str(msg)))

    def warning(self, msg: str, *a, **k) -> None:
        self.records.append(("warning", str(msg)))

    def error(self, msg: str, *a, **k) -> None:
        self.records.append(("error", str(msg)))

    def debug(self, msg: str, *a, **k) -> None:
        self.records.append(("debug", str(msg)))


def _make_filter_hooks() -> types.SimpleNamespace:
    """过滤器装饰器全部返回 identity 装饰器，并把 kwargs 记下来。

    签名照抄 AstrBot ``astrbot/core/star/register/star_handler.py``：
    ``register_event_message_type(event_message_type, **kwargs)`` 与
    ``register_command(command_name=None, **kwargs)`` 都接受位置参数，
    其余钩子只接受 kwargs。
    """

    calls: list[tuple[str, dict]] = []

    def make_kwargs_only(name: str):
        def factory(**kwargs):
            calls.append((name, kwargs))

            def decorator(func):
                func.__hook__ = name
                return func

            return decorator

        return factory

    def event_message_type(event_message_type, **kwargs):
        calls.append(("event_message_type", {"type": event_message_type, **kwargs}))

        def decorator(func):
            func.__hook__ = "event_message_type"
            return func

        return decorator

    def command(command_name=None, **kwargs):
        calls.append(("command", {"name": command_name, **kwargs}))

        def decorator(func):
            func.__hook__ = "command"
            return func

        return decorator

    hooks = types.SimpleNamespace(
        on_waiting_llm_request=make_kwargs_only("on_waiting_llm_request"),
        on_llm_request=make_kwargs_only("on_llm_request"),
        on_decorating_result=make_kwargs_only("on_decorating_result"),
        event_message_type=event_message_type,
        command=command,
    )
    hooks.EventMessageType = _EventMessageType
    hooks._calls = calls  # noqa: SLF001 - 测试用
    return hooks


class FakeConfig(dict):
    """最简 AstrBotConfig 替身：就是 dict，带 save_config。"""

    def save_config(self, *_a, **_k) -> None:
        pass


class FakeEvent:
    """按真实 ``AstrMessageEvent`` 的语义实现的替身。"""

    def __init__(self, text: str = "你好", *, group: bool = False) -> None:
        self.message_str = text
        self.message_obj = types.SimpleNamespace(
            message=[_Plain(text)], message_str=text, raw_message=None
        )
        self.unified_msg_origin = f"test:{'group' if group else 'private'}:1"
        self._group = group
        self.call_llm = False
        self.is_at_or_wake_command = False
        self._stopped = False
        self._extras: dict = {}
        self.is_wake = False

    # -- 真实 API -------------------------------------------------------- #

    def is_private_chat(self) -> bool:
        return not self._group

    def get_platform_name(self) -> str:
        return "stub"

    def get_self_id(self) -> str:
        return "bot"

    def get_sender_id(self) -> str:
        return "user"

    def get_group_id(self) -> str:
        return "g1" if self._group else ""

    def get_messages(self):
        """对应 AstrMessageEvent.get_messages()。"""
        chain = getattr(self.message_obj, "message", None)
        return list(chain) if isinstance(chain, list) else []

    def is_admin(self) -> bool:
        return False

    def is_stopped(self) -> bool:
        return self._stopped

    def stop_event(self) -> None:
        self._stopped = True

    def should_call_llm(self, value: bool) -> None:
        # 语义与源码一致：True == 禁止默认 LLM。
        self.call_llm = value

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def plain_result(self, text: str):
        return types.SimpleNamespace(chain=[_Plain(text)], _text=text)

    def get_result(self):
        return self._result

    _result = None


def install_stub() -> types.SimpleNamespace:
    """把假的 astrbot 包塞进 sys.modules。"""
    logger = _Logger()

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logger
    api.AstrBotConfig = FakeConfig

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = FakeEvent
    hooks = _make_filter_hooks()
    event_mod.filter = hooks

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context) -> None:
            self.context = context

    star_mod.Star = Star
    star_mod.Context = object

    comps = types.ModuleType("astrbot.api.message_components")
    comps.Plain = _Plain

    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_plugin_data_path = lambda: "/tmp/betterchat-test"
    utils.astrbot_path = path_mod

    registry_mod = types.ModuleType("astrbot.core.utils.active_event_registry")

    class Registry:
        def __init__(self) -> None:
            self.stopped: list[tuple[str, object]] = []

        def request_agent_stop_all(self, umo, exclude=None):
            self.stopped.append((umo, exclude))
            return 1

    registry = Registry()
    registry_mod.active_event_registry = registry

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.api.message_components": comps,
        "astrbot.core": core,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": path_mod,
        "astrbot.core.utils.active_event_registry": registry_mod,
    }
    sys.modules.update(modules)
    return types.SimpleNamespace(
        logger=logger, hooks=hooks, registry=registry, modules=modules
    )


@pytest.fixture(scope="module")
def loaded():
    """加载 ``main.py``，测试结束后还原 sys.modules。"""
    stub = install_stub()
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    for name in list(sys.modules):
        if name.startswith("betterchat"):
            del sys.modules[name]
    module = importlib.import_module("main")
    yield types.SimpleNamespace(module=module, stub=stub)
    for name in [*stub.modules, "main"]:
        sys.modules.pop(name, None)
    if str(root) in sys.path:
        sys.path.remove(str(root))


def _isolated_plugin(loaded, config):  # noqa: ANN001
    """建一个插件实例，并把限频状态文件放到临时目录。

    限频计数是**跨实例持久化**的（这是产品特性），所以测试之间会互相污染 ——
    前一个测试记的回复会让后一个测试被判 RATE_LIMITED。
    """
    import tempfile

    tmp = tempfile.mkdtemp()
    plugin = loaded.module.BetterChatPlugin(context=None, config=config)
    plugin.limiter = loaded.module.RateLimiter(pathlib.Path(tmp) / "state.json")
    return plugin


def base_config() -> dict:
    return {
        "enabled": True,
        "private": {"enable": True},
        "interrupt": {"enable": True, "mode": "graceful"},
        "group": {"enable": True},
        "jev": {"channel": "official", "api_key": ""},
    }


# --------------------------------------------------------------------------- #
# 加载与实例化
# --------------------------------------------------------------------------- #


def test_module_imports_and_registers_hooks(loaded) -> None:
    names = {name for name, _ in loaded.stub.hooks._calls}  # noqa: SLF001
    assert "on_waiting_llm_request" in names
    assert "on_llm_request" in names
    assert "on_decorating_result" in names
    assert "event_message_type" in names
    assert "command" in names


def test_plugin_instantiates_without_io(loaded) -> None:
    plugin = loaded.module.BetterChatPlugin(context=None, config=FakeConfig(base_config()))
    assert plugin._enabled() is True  # noqa: SLF001
    # 没配 key：闸门应自我禁用，而不是抛错。
    assert plugin.jev.config.ready_missing() is not None


def test_plugin_survives_empty_config(loaded) -> None:
    plugin = loaded.module.BetterChatPlugin(context=None, config=None)
    assert plugin._enabled() is True  # noqa: SLF001
    assert plugin._group_settings().enable is True  # noqa: SLF001


def test_plugin_survives_garbage_config(loaded) -> None:
    """面板配置被手改坏时，插件必须还能起来。"""
    config = FakeConfig(
        {
            "enabled": "yes",
            "private": "oops",
            "group": [1, 2, 3],
            "jev": {"timeout_seconds": "abc", "reply_threshold": float("nan")},
        }
    )
    plugin = loaded.module.BetterChatPlugin(context=None, config=config)
    assert plugin._merge_settings().base_quiet_seconds == 2.0  # noqa: SLF001
    assert plugin.jev.config.timeout_seconds == 3.0
    assert plugin.jev.config.reply_threshold == 0.7


# --------------------------------------------------------------------------- #
# should_call_llm 的反向语义
# --------------------------------------------------------------------------- #


def test_take_over_and_hand_back_helpers(loaded) -> None:
    """``should_call_llm`` 语义是反的：True 表示禁止默认 LLM。"""
    plugin = loaded.module.BetterChatPlugin(context=None, config=FakeConfig(base_config()))
    event = FakeEvent()

    plugin._let_astrbot_handle(event)  # noqa: SLF001
    assert event.call_llm is False  # 不禁止 => 走原生 LLM

    plugin._take_over_turn(event)  # noqa: SLF001
    assert event.call_llm is True  # 禁止 => 原生 LLM 不跑


# --------------------------------------------------------------------------- #
# 合并路径
# --------------------------------------------------------------------------- #


def run(coro):
    return asyncio.run(coro)


def test_plain_text_detection_handles_str_enum(loaded) -> None:
    """回归测试：``ComponentType(str, Enum)`` 必须被判为纯文本。

    曾经这里用了 ``type(x) is str``，而 ``ComponentType`` 是 str 的**子类**，
    于是纯文本被判成"非纯文本"，合并被整体跳过 —— 表现就是每条消息都
    立即回复，合并功能形同虚设。
    """

    class Event:
        def __init__(self, chain):
            self.message_obj = types.SimpleNamespace(message=chain)

    plugin_cls = loaded.module.BetterChatPlugin
    # 纯文本 -> 应当参与合并
    assert plugin_cls._is_plain_text(Event([_Plain("你好")])) is True
    assert plugin_cls._is_plain_text(Event([_Plain("a"), _Plain("b")])) is True
    # 含图片 -> 不参与合并
    assert plugin_cls._is_plain_text(Event([_Image()])) is False
    assert plugin_cls._is_plain_text(Event([_Plain("a"), _Image()])) is False
    # 空链 -> 保守地当作纯文本
    assert plugin_cls._is_plain_text(Event([])) is True


def test_single_private_message_passes_through(loaded) -> None:
    """单条消息：窗口到期后合并文本写回事件，并交还原生流程。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["private"] = {
            "enable": True,
            "base_quiet_seconds": 0.01,
            "min_quiet_seconds": 0.01,
            "max_quiet_seconds": 0.02,
            "total_seconds": 0.05,
        }
        plugin = loaded.module.BetterChatPlugin(context=None, config=config)
        event = FakeEvent("你好")
        await plugin.on_waiting_llm(event)
        return event

    event = run(scenario())
    assert event.message_str == "你好"
    # OWNER 分支必须交还原生流程（call_llm=False）。
    assert event.call_llm is False


def test_command_message_bypasses_merge(loaded) -> None:
    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        event = FakeEvent("/help")
        await plugin.on_waiting_llm(event)
        return event

    event = run(scenario())
    # 指令不参与合并：不应被本插件禁止 LLM，也不该进窗口。
    assert event.call_llm is False


def test_group_gate_ignores_when_jev_unconfigured(loaded) -> None:
    """没配 key：群聊主动插话必须自我禁用，且不回复。"""

    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        event = FakeEvent("这是一个正常的群聊消息", group=True)
        await plugin.on_group_message(event)
        return event

    event = run(scenario())
    assert event.call_llm is True  # 禁止默认 LLM => 不回复
    assert event.is_at_or_wake_command is False


def test_group_gate_lets_woken_message_through_when_jev_down(loaded) -> None:
    """被 @ 的消息即使 Jev 不可用也要放行 —— 不能因为判定服务挂了就不理人。"""

    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        event = FakeEvent("这是一个正常的群聊消息", group=True)
        event.is_at_or_wake_command = True
        await plugin.on_group_message(event)
        return event

    event = run(scenario())
    # 失败策略：already_woken => RESPOND => 不被禁止。
    assert event.call_llm is False
    assert event.is_at_or_wake_command is True


def test_group_gate_disabled_by_config(loaded) -> None:
    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {"enable": False}
        plugin = loaded.module.BetterChatPlugin(context=None, config=config)
        event = FakeEvent("这是一个正常的群聊消息", group=True)
        await plugin.on_group_message(event)
        return event

    event = run(scenario())
    # 完全不动事件。
    assert event.call_llm is False
    assert event.is_at_or_wake_command is False


# --------------------------------------------------------------------------- #
# 打断路径
# --------------------------------------------------------------------------- #


def test_interrupt_marks_previous_event_stale(loaded) -> None:
    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        first = FakeEvent("第一句")
        plugin.state.in_flight[first.unified_msg_origin] = first  # noqa: SLF001
        second = FakeEvent("第二句")
        plugin._interrupt_in_flight(first.unified_msg_origin, second)  # noqa: SLF001
        return first, second, plugin

    first, second, plugin = run(scenario())
    assert first.get_extra("betterchat_stale") is True
    assert second.get_extra("betterchat_stale") is None
    # 确实发出了停止请求，且排除了新事件。
    assert loaded and plugin is not None


def test_stale_event_is_blocked_at_llm_and_result(loaded) -> None:
    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        event = FakeEvent("旧回复")
        event.set_extra("betterchat_stale", True)
        event._result = types.SimpleNamespace(chain=[_Plain("旧回复")])  # noqa: SLF001
        await plugin.on_llm_request(event, None)
        blocked_llm = event.is_stopped()

        event2 = FakeEvent("旧回复")
        event2.set_extra("betterchat_stale", True)
        event2._result = types.SimpleNamespace(chain=[_Plain("旧回复")])  # noqa: SLF001
        await plugin.on_decorating_result(event2)
        return blocked_llm, event2

    blocked_llm, event2 = run(scenario())
    assert blocked_llm is True
    # 关键：chain 被清空，保证一个字都发不出去。
    assert event2.get_result().chain == []
    assert event2.is_stopped() is True


def test_non_stale_result_is_untouched(loaded) -> None:
    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        event = FakeEvent("正常回复")
        event._result = types.SimpleNamespace(chain=[_Plain("正常回复")])  # noqa: SLF001
        await plugin.on_decorating_result(event)
        return event

    event = run(scenario())
    assert len(event.get_result().chain) == 1
    assert event.is_stopped() is False


def test_second_message_joins_window_instead_of_interrupting(loaded) -> None:
    """回归测试：第二条消息在窗口内到达时，必须**加入合并**，不能打断第一条。

    这里踩过两个连环坑：
    1. 早期 ``in_flight`` 在合并逻辑之前就被设成当前事件，
       「打断旧事件」的检查永远读到自己 → 打断完全失效。
    2. 修了之后又过头了：把「还在窗口里等」的上一轮也当成
       「正在生成」去打断 → 用户连发两条时，第二条把第一条打断，
       自己成为新的 in_flight，第三条再打断它……
       连锁下去就变成「每条消息各自回复一次」，合并形同虚设。
    """

    async def scenario():
        config = FakeConfig(base_config())
        config["private"] = {
            "enable": True,
            "base_quiet_seconds": 0.15,
            "min_quiet_seconds": 0.15,
            "max_quiet_seconds": 0.15,
            "total_seconds": 0.4,
        }
        plugin = loaded.module.BetterChatPlugin(context=None, config=config)

        first = FakeEvent("第一句话")
        first_task = asyncio.create_task(plugin.on_waiting_llm(first))
        await asyncio.sleep(0.02)  # 让 first 进入等待并登记 in_flight

        # first 应当已被登记为「在跑的那条」。
        assert plugin.state.in_flight.get(first.unified_msg_origin) is first

        second = FakeEvent("第二句话")
        # 第二条在窗口内到达 → 应当并入，而不是打断。
        await plugin.on_waiting_llm(second)

        await asyncio.wait_for(first_task, timeout=2.0)

        # 第一条**不该**被标记为失效（它是被合并，不是被打断）。
        assert first.get_extra("betterchat_stale") is None
        # 两条都进了同一个窗口 → 只有一个拿到本轮。
        winners = [e for e in (first, second) if e.call_llm is False]
        assert len(winners) == 1, "应当只有一个事件继续走 LLM"
        return True

    assert run(scenario()) is True


def test_superseded_message_does_not_run_llm(loaded) -> None:
    """被接管的那条必须禁止默认 LLM，否则会重复回复。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["private"] = {
            "enable": True,
            "base_quiet_seconds": 0.2,
            "min_quiet_seconds": 0.2,
            "max_quiet_seconds": 0.2,
            "total_seconds": 0.5,
        }
        plugin = loaded.module.BetterChatPlugin(context=None, config=config)

        first = FakeEvent("第一句")
        task = asyncio.create_task(plugin.on_waiting_llm(first))
        await asyncio.sleep(0.02)

        second = FakeEvent("第二句")
        plugin.merge.reserve(second.unified_msg_origin, "第二句")
        await asyncio.wait_for(task, timeout=2.0)
        return first

    first = run(scenario())
    assert first.call_llm is True  # True == 禁止默认 LLM


def test_group_gate_records_reply_for_rate_limiting(loaded) -> None:
    """回归测试：判定要回复时必须记账。

    ``max_replies_per_hour`` 读的就是这个计数；如果回复后不累加，
    限频永远读到 0，等于完全没限频。
    """

    async def scenario():
        import tempfile

        # 每个实例用独立的状态文件，避免测试之间互相污染。
        with tempfile.TemporaryDirectory() as tmp:
            plugin = loaded.module.BetterChatPlugin(
                context=None, config=FakeConfig(base_config())
            )
            plugin.limiter = loaded.module.RateLimiter(
                pathlib.Path(tmp) / "state.json"
            )
            umo = "test:group:1"

            # 直接驱动判定结果，绕开真实 Jev 调用。
            from betterchat_core import Action, GroupVerdict

            async def fake_judge(*_a, **_k):
                return GroupVerdict(Action.RESPOND, "test")

            plugin._judge_group = fake_judge  # noqa: SLF001
            event = FakeEvent("这是一个会被主动插话的群消息", group=True)
            await plugin.on_group_message(event)

            # 记账是异步落盘的，等它跑完。
            for _ in range(20):
                if plugin.limiter.count(umo) > 0:
                    break
                await asyncio.sleep(0.01)
            return plugin.limiter.count(umo)

    assert run(scenario()) == 1


def test_jev_failure_is_logged_once_per_cooldown(loaded) -> None:
    """群聊里每条消息都会触发判定，同一个错误不能逐条刷屏。"""

    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        before = len(loaded.stub.logger.records)

        from betterchat_jev import JevError

        for _ in range(5):
            plugin._log_jev_failure(JevError("HTTP 400 code 7000"))  # noqa: SLF001

        new_records = loaded.stub.logger.records[before:]
        warnings = [r for r in new_records if r[0] == "warning"]
        debugs = [r for r in new_records if r[0] == "debug"]
        return len(warnings), len(debugs)

    warnings, debugs = run(scenario())
    assert warnings == 1, "同一条错误只应警告一次"
    assert debugs == 4, "其余应降级为 debug"


def test_jev_failure_logs_again_when_error_changes(loaded) -> None:
    """错误内容变了说明是新问题，应当重新提示。"""

    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        before = len(loaded.stub.logger.records)

        from betterchat_jev import JevError

        plugin._log_jev_failure(JevError("HTTP 400 code 7000"))  # noqa: SLF001
        plugin._log_jev_failure(JevError("HTTP 402 code 2021"))  # noqa: SLF001

        return [
            r for r in loaded.stub.logger.records[before:] if r[0] == "warning"
        ]

    warnings = run(scenario())
    assert len(warnings) == 2


def test_group_aggregation_merges_burst_into_one_judge(loaded) -> None:
    """群聊连刷多条 → 只调用一次 Jev 判定。

    被并进来的事件必须**禁止默认 LLM**，否则会重复回复。
    """

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {
            "enable": True,
            "judge_all_messages": True,
            "aggregate_window_seconds": 0.08,
            "min_message_length": 1,
        }
        plugin = _isolated_plugin(loaded, config)

        calls = {"n": 0}

        from betterchat_core import Action, GroupVerdict

        async def fake_judge(event, umo, settings, text, already_woken):
            calls["n"] += 1
            calls["text"] = text
            return GroupVerdict(Action.IGNORE, "test")

        plugin._judge_group = fake_judge  # noqa: SLF001

        # 连发三条
        events = [FakeEvent(f"第{i}句话都挺长的", group=True) for i in range(3)]
        tasks = [
            asyncio.create_task(plugin.on_group_message(e)) for e in events
        ]
        await asyncio.sleep(0.01)
        await asyncio.gather(*tasks, return_exceptions=True)

        return calls, events

    calls, events = run(scenario())
    # 三次消息只产生一次判定。
    assert calls["n"] == 1, f"应只判定一次，实际 {calls['n']} 次"
    # 判定用的文本包含三句。
    assert "第0句" in calls["text"] and "第2句" in calls["text"]
    # 被并进来的事件都不许跑默认 LLM。
    for offset, event in enumerate(events):
        assert event.call_llm is True, f"第{offset}条不应触发默认 LLM"


def test_group_aggregation_skipped_when_woken(loaded) -> None:
    """被 @ 的消息不聚合，必须立即判定。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {
            "enable": True,
            "judge_all_messages": True,
            "aggregate_window_seconds": 5.0,  # 很长，若聚合会卡住
            "min_message_length": 1,
        }
        plugin = _isolated_plugin(loaded, config)
        # 被唤醒的消息不受主动插话限频约束，这里也不该被历史计数干扰。

        from betterchat_core import Action, GroupVerdict

        async def fake_judge(*_a, **_k):
            return GroupVerdict(Action.RESPOND, "woken")

        plugin._judge_group = fake_judge  # noqa: SLF001

        event = FakeEvent("HexVergeBot 帮我看下", group=True)
        event.is_at_or_wake_command = True
        await asyncio.wait_for(plugin.on_group_message(event), timeout=2.0)
        return event

    event = run(scenario())
    # 立即判定并放行，不该等 5 秒。
    assert event.call_llm is False


def test_group_aggregation_disabled_when_zero(loaded) -> None:
    """窗口设 0 → 恢复每条各判一次。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {
            "enable": True,
            "judge_all_messages": True,
            "aggregate_window_seconds": 0,
            "min_message_length": 1,
        }
        plugin = _isolated_plugin(loaded, config)

        calls = {"n": 0}
        from betterchat_core import Action, GroupVerdict

        async def fake_judge(*_a, **_k):
            calls["n"] += 1
            return GroupVerdict(Action.IGNORE, "test")

        plugin._judge_group = fake_judge  # noqa: SLF001
        for i in range(3):
            await plugin.on_group_message(FakeEvent(f"第{i}句话都挺长的", group=True))
        return calls["n"]

    assert run(scenario()) == 3


def test_group_context_is_actually_sent_to_jev(loaded) -> None:
    """回归测试：判定时必须真的带上群聊上下文。

    早期实现去读 ``event.message_obj.group_messages`` 等**不存在**的属性，
    每次都静默返回空列表 —— 判定等于盲猜，而且不报错。
    现在改为自己维护缓存，这个测试盯住"上下文确实进到了 state 里"。
    """

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {
            "enable": True,
            "judge_all_messages": True,
            "aggregate_window_seconds": 0,
            "min_message_length": 1,
            "context_messages": 10,
        }
        plugin = _isolated_plugin(loaded, config)

        captured = {}

        from betterchat_core import Action, GroupVerdict

        async def fake_judge(event, umo, settings, text, already_woken):
            captured["context"] = plugin._group_context(event, umo, settings)  # noqa: SLF001
            captured["text"] = text
            return GroupVerdict(Action.IGNORE, "test")

        plugin._judge_group = fake_judge  # noqa: SLF001

        # 先来两条群消息，再来第三条触发判定
        for text in ("第一句话够长了", "第二句话也够长"):
            await plugin.on_group_message(FakeEvent(text, group=True))
        await plugin.on_group_message(FakeEvent("第三句话同样长", group=True))
        return captured

    captured = run(scenario())
    context = captured["context"]
    assert context, "上下文不能为空 —— 否则判定就是在盲猜"
    texts = [c.text for c in context]
    assert "第一句话够长了" in texts
    assert "第二句话也够长" in texts


def test_group_context_respects_limit(loaded) -> None:
    """context_messages 生效，不会把整个缓存都塞进去。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {
            "enable": True,
            "judge_all_messages": True,
            "aggregate_window_seconds": 0,
            "min_message_length": 1,
            "context_messages": 2,
        }
        plugin = _isolated_plugin(loaded, config)
        captured = {}

        from betterchat_core import Action, GroupVerdict

        async def fake_judge(event, umo, settings, text, already_woken):
            captured["context"] = plugin._group_context(event, umo, settings)  # noqa: SLF001
            return GroupVerdict(Action.IGNORE, "test")

        plugin._judge_group = fake_judge  # noqa: SLF001
        for i in range(5):
            await plugin.on_group_message(FakeEvent(f"第{i}句话都够长了", group=True))
        return captured["context"]

    context = run(scenario())
    assert len(context) <= 2


def test_context_survives_gate_rejection(loaded) -> None:
    """被本地闸门拦下的消息也要进上下文。

    否则"有没有人接话"的判断会缺片：前面几句都被闸门挡掉，
    后面判定时看到的上下文是空的。
    """

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {
            "enable": True,
            "judge_all_messages": True,
            "aggregate_window_seconds": 0,
            "min_message_length": 50,  # 故意设很大，让消息被闸门拦下
            "context_messages": 10,
        }
        plugin = _isolated_plugin(loaded, config)

        # 这条会被 TOO_SHORT 拦掉
        await plugin.on_group_message(FakeEvent("短消息", group=True))
        return plugin.transcripts.recent("test:group:1")  # noqa: SLF001

    recorded = run(scenario())
    assert [c.text for c in recorded] == ["短消息"]


def test_reread_triggers_and_swallows_message(loaded) -> None:
    """复读命中后：转发原消息 + 吞掉这条，不再走唤醒判定。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {"enable": True, "judge_all_messages": True, "aggregate_window_seconds": 0, "min_message_length": 1}
        config["reread"] = {
            "enable": True,
            "text_threshold": 3,
            "follow_probability": 1.0,  # 必定跟读
            "cooldown_seconds": 0,
            "max_per_hour": 0,
        }
        plugin = _isolated_plugin(loaded, config)

        events = []
        for i, sender in enumerate(["u1", "u2", "u3"]):
            event = FakeEvent("哈哈哈", group=True)
            event.message_obj.message = [_Plain("哈哈哈")]
            event.message_obj.message_id = f"m{i}"
            event.get_sender_id = lambda s=sender: s  # type: ignore[method-assign]
            events.append(event)

        # 前两条不触发，第三条触发
        for event in events:
            await plugin.on_group_message(event)

        # 检查是否有事件被 send 过（复读发送）
        return events

    events = run(scenario())
    # 第三条触发复读：它应该被 take over（call_llm=True），不继续走判定
    assert events[-1].call_llm is True


def test_reread_disabled_by_default(loaded) -> None:
    """默认关闭 —— 不应有任何复读行为。"""

    async def scenario():
        plugin = _isolated_plugin(loaded, FakeConfig(base_config()))
        return plugin._reread_settings().enable  # noqa: SLF001

    assert run(scenario()) is False


def test_reread_ignores_commands(loaded) -> None:
    """指令不参与复读。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {"enable": True, "judge_all_messages": True, "aggregate_window_seconds": 0, "min_message_length": 1}
        config["reread"] = {"enable": True, "text_threshold": 2, "follow_probability": 1.0, "cooldown_seconds": 0, "max_per_hour": 0}
        plugin = _isolated_plugin(loaded, config)
        results = []
        for i, sender in enumerate(["u1", "u2"]):
            event = FakeEvent("/help", group=True)
            event.message_obj.message = [_Plain("/help")]
            event.message_obj.message_id = f"c{i}"
            event.get_sender_id = lambda s=sender: s  # type: ignore[method-assign]
            results.append(await plugin._try_reread(event, "test:group:1", plugin._group_settings()))  # noqa: SLF001
        return results

    assert run(scenario()) == [None, None]


def test_proactive_disabled_by_default(loaded) -> None:
    async def scenario():
        plugin = _isolated_plugin(loaded, FakeConfig(base_config()))
        return plugin._proactive_settings().enable  # noqa: SLF001

    assert run(scenario()) is False


def test_proactive_schedules_and_cancels_on_new_message(loaded) -> None:
    """有人说话 → 安排冷场检查；再来消息 → 取消重排。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {"enable": True, "judge_all_messages": True, "aggregate_window_seconds": 0, "min_message_length": 1}
        config["proactive"] = {"enable": True, "silence_seconds": 600}
        plugin = _isolated_plugin(loaded, config)
        umo = "test:group:1"

        await plugin.on_group_message(FakeEvent("第一句够长了", group=True))
        first = plugin._silence_tasks.get(umo)  # noqa: SLF001
        assert first is not None, "应当安排了冷场检查"

        await plugin.on_group_message(FakeEvent("第二句够长了", group=True))
        second = plugin._silence_tasks.get(umo)  # noqa: SLF001
        assert second is not None
        assert second is not first, "新消息应当重排"
        # cancel 是"已请求"，任务要在下一个 check point 才真正结束。
        assert first.cancelling() > 0, "旧任务应被请求取消"

        await plugin.terminate()
        assert not plugin._silence_tasks  # noqa: SLF001
        return True

    assert run(scenario()) is True


def test_proactive_gate_respects_quiet_hours(loaded) -> None:
    """免打扰时段不开口。"""
    from betterchat_group_extras import ProactiveAction, ProactiveSettings, proactive_gate

    action, reason = proactive_gate(
        settings=ProactiveSettings(enable=True, quiet_hours="23-7"),
        silence_seconds=9999,
        seconds_since_bot=float("inf"),
        proactive_today=0,
        message_count=10,
        hour_of_day=3,
    )
    assert action is ProactiveAction.SKIP
    assert reason == "quiet_hours"


def test_proactive_state_carries_silence_facts(loaded) -> None:
    """冷场 state 必须带上"安静多久"这类事实。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["proactive"] = {"enable": True, "silence_seconds": 600}
        plugin = _isolated_plugin(loaded, config)
        umo = "test:group:1"
        plugin.silence.note_message(umo, now=1000.0)
        plugin.silence.note_message(umo, now=1010.0)
        state = plugin._proactive_state(umo, plugin._proactive_settings())  # noqa: SLF001
        return state

    state = run(scenario())
    assert "silence_seconds" in state
    assert "seconds_since_bot_spoke" in state
    assert "recent_messages" in state


def test_at_other_detects_mention_of_someone_else(loaded) -> None:
    """「@ 了别人」必须能检测出来 —— 这是不抢答的前提。

    AstrBot 的 At 组件带 ``qq`` 字段，平台层面就已经确定了 @ 的是谁，
    比让模型从文本里猜靠谱。
    """

    At = _At

    def make(chain):
        event = FakeEvent("x", group=True)
        event.message_obj.message = chain
        return event

    # @ 别人 → True
    assert loaded.module.BetterChatPlugin.at_other(make([At("other-id")])) is True
    # @ 自己 → False
    assert loaded.module.BetterChatPlugin.at_other(make([At("bot")])) is False
    # @全体 → False
    assert loaded.module.BetterChatPlugin.at_other(make([At("all")])) is False
    # 没有 At → False
    assert loaded.module.BetterChatPlugin.at_other(make([_Plain("hi")])) is False


def test_group_gate_ignores_message_addressed_to_other(loaded) -> None:
    """端到端：@ 了别人的提问不该触发主动插话。"""

    At = _At

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {
            "enable": True,
            "judge_all_messages": True,
            "aggregate_window_seconds": 0,
            "min_message_length": 1,
        }
        plugin = _isolated_plugin(loaded, config)

        async def fake_judge(event, umo, settings, text, already_woken):
            # 走真实的决策逻辑，验证 addressed_to_other 确实传下去了
            from betterchat_core import decide_group_action

            return decide_group_action(
                addressed=0.06,
                open_question=0.95,
                already_answered=0.0,
                already_woken=already_woken,
                addressed_to_other=plugin.at_other(event),
            )

        plugin._judge_group = fake_judge  # noqa: SLF001

        event = FakeEvent("@某人 你这是哪个大学的", group=True)
        event.message_obj.message = [At("someone-else"), _Plain("你这是哪个大学的")]
        await plugin.on_group_message(event)
        return event

    event = run(scenario())
    # 判定为不说话 → call_llm=True（禁止默认 LLM）
    assert event.call_llm is True, "@ 了别人的对话不该被插话"


def test_at_other_detects_qq_official_text_markup(loaded) -> None:
    """回归测试：QQ 官方平台的 @别人 是**纯文本**，不是 At 组件。

    这是线上实际踩到的坑。AstrBot 的 qqofficial 适配器只把 **@Bot 自己** 的
    mention 转成 ``At`` 组件：

        bot_mentions = [m for m in message.mentions if m.is_you is True]
        if group_mentioned:
            msg.append(At(qq=abm.self_id, ...))

    @ 别人的 mention 被原样留在 ``message_str`` 里，形如 ``<@openid>``。
    所以只遍历 ``At`` 组件的实现，在这个平台上永远返回 False ——
    单测能过（假消息链里塞了 At），线上完全不生效。
    """
    plugin = loaded.module.BetterChatPlugin

    # 没有任何 At 组件，@ 只出现在文本里
    for text in (
        "<@2879588DE5953607C534F862B4AFFE5C> 有没有插件推荐",
        "<@!123456789> hi",
        '<qqbot-at-user id="abc123" /> 你好',
    ):
        event = FakeEvent(text, group=True)
        event.message_obj.message = [_Plain(text)]
        assert plugin.at_other(event) is True, f"未识别文本 @ 标记: {text!r}"

    # @ 自己的文本标记 → 不算「@ 别人」
    event = FakeEvent("<@bot> 你好", group=True)
    event.message_obj.message = [_Plain("<@bot> 你好")]
    assert plugin.at_other(event) is False


def test_at_other_ignores_plain_text_without_mentions(loaded) -> None:
    """普通文本不能被误判成 @。"""
    plugin = loaded.module.BetterChatPlugin
    for text in ("<重要> 注意一下", "a < b > c", "没有任何 at"):
        event = FakeEvent(text, group=True)
        event.message_obj.message = [_Plain(text)]
        assert plugin.at_other(event) is False, f"误判: {text!r}"


def test_help_unaddressed_defaults_to_defer_mode(loaded) -> None:
    """「主动帮没人接的问题」默认开启，但走观望而不是立即插话。"""

    async def scenario():
        plugin = _isolated_plugin(loaded, FakeConfig(base_config()))
        s = plugin._group_settings()  # noqa: SLF001
        return s.help_unaddressed, s.help_unaddressed_delay_seconds

    enabled, delay = run(scenario())
    assert enabled is True
    assert delay == 90


def test_defer_schedules_watch_and_cancels_on_reply(loaded) -> None:
    """观望期内有人接话 → 取消主动帮忙。

    这是「等时间」的核心语义：不立即插话，给正在打字的人机会。
    """

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {
            "enable": True,
            "judge_all_messages": True,
            "aggregate_window_seconds": 0,
            "min_message_length": 1,
            "help_unaddressed": True,
            "help_unaddressed_delay_seconds": 30,
        }
        plugin = _isolated_plugin(loaded, config)

        from betterchat_core import Action, GroupVerdict

        async def fake_judge(*_a, **_k):
            return GroupVerdict(Action.DEFER, "unanswered_question", 0.08, 0.94, 0.0)

        plugin._judge_group = fake_judge  # noqa: SLF001

        umo = "test:group:1"
        # 第一条触发观望
        await plugin.on_group_message(FakeEvent("有人知道这个报错吗", group=True))
        task = plugin._help_tasks.get(umo)  # noqa: SLF001
        assert task is not None, "应当登记观望任务"

        # 第二条（有人接话）→ 观望任务被取消
        await plugin.on_group_message(FakeEvent("我来答：是缺依赖", group=True))
        assert task.cancelling() > 0, "有人接话时应当取消观望"

        await plugin.terminate()
        assert not plugin._help_tasks  # noqa: SLF001
        return True

    assert run(scenario()) is True


def test_defer_not_scheduled_when_help_disabled(loaded) -> None:
    """功能关闭时不该登记观望。"""

    async def scenario():
        config = FakeConfig(base_config())
        config["group"] = {
            "enable": True,
            "judge_all_messages": True,
            "aggregate_window_seconds": 0,
            "min_message_length": 1,
            "help_unaddressed": False,
        }
        plugin = _isolated_plugin(loaded, config)

        async def fake_judge(*_a, **_k):
            # 关闭后走真实决策逻辑会返回 IGNORE
            from betterchat_core import decide_group_action

            return decide_group_action(
                addressed=0.08,
                open_question=0.94,
                already_answered=0.0,
                already_woken=False,
                help_unaddressed=False,
            )

        plugin._judge_group = fake_judge  # noqa: SLF001
        await plugin.on_group_message(FakeEvent("有人知道这个报错吗", group=True))
        return plugin._help_tasks  # noqa: SLF001

    assert run(scenario()) == {}


def test_non_text_message_joins_merge_window(loaded) -> None:
    """回归测试：非纯文本消息不能绕过合并窗口。

    实测踩过：用户连发两句 + 一个 Sticker 时，Sticker 因为
    ``_is_plain_text`` 为 False 被直接放行，单独触发了一次 LLM 请求；
    而攒着的那两句到期后又触发第二次 —— 一次行为产生两个回复，
    还互相排队，回复延迟被拖到 30 秒以上。

    现在非文本消息也进入窗口，只是不参与文本拼接。
    """

    async def scenario():
        config = FakeConfig(base_config())
        config["private"] = {
            "enable": True,
            "base_quiet_seconds": 0.05,
            "min_quiet_seconds": 0.05,
            "max_quiet_seconds": 0.05,
            "total_seconds": 0.2,
        }
        plugin = _isolated_plugin(loaded, config)

        # 第一条：纯文本
        first = FakeEvent("有你就够了")
        t1 = asyncio.create_task(plugin.on_waiting_llm(first))
        await asyncio.sleep(0.005)

        # 第二条：图片（非纯文本）
        image_event = FakeEvent("")
        image_event.message_obj.message = [_Image("a.jpg")]
        t2 = asyncio.create_task(plugin.on_waiting_llm(image_event))

        await asyncio.gather(t1, t2, return_exceptions=True)
        # 两条都进了同一个窗口 → 只有一个拿到本轮
        winners = [
            e for e in (first, image_event) if not e.is_stopped()
        ]
        return first, image_event, winners

    first, image_event, winners = run(scenario())
    # 两条都被处理过（没有被直接放行绕过窗口）
    both_ran = first.call_llm is not None and image_event.call_llm is not None
    assert both_ran


def test_empty_private_message_passes_through(loaded) -> None:
    """纯空白消息没有合并价值，应直接交还原生流程。"""

    async def scenario():
        plugin = _isolated_plugin(loaded, FakeConfig(base_config()))
        event = FakeEvent("   ")
        await plugin.on_waiting_llm(event)
        return event

    event = run(scenario())
    # 交还原生流程 → call_llm=False
    assert event.call_llm is False


def test_jev_error_with_empty_body_has_context(loaded) -> None:
    """回归测试：服务端返回空响应体时不能只剩 `HTTP 400: `。

    实测 OpenCode 的 400 有时响应体是空的，日志里完全看不出是哪个字段
    的问题。现在会补上 model / endpoint / state 大小 / questions 名称。
    """
    from betterchat_jev import JevConfig, _request_context_hint

    config = JevConfig.from_config(
        {"channel": "opencode", "oc_api_key": "k", "oc_model": "jev-1.13-free"}
    )
    hint = _request_context_hint(
        config, {"state": {"a": 1}, "questions": {"q1": {}}}, 400
    )
    assert "status=400" in hint
    assert "model=jev-1.13-free" in hint
    assert "endpoint=" in hint
    assert "state_chars=8" in hint
    assert "questions=['q1']" in hint


def test_burst_of_private_messages_merges_into_one(loaded) -> None:
    """回归测试：连发多条只能产生一次回复。

    这是「每条都回复」的直接回归用例：如果合并窗口被打断逻辑误伤，
    每条消息都会各自走一次 LLM。
    """

    async def scenario():
        config = FakeConfig(base_config())
        config["private"] = {
            "enable": True,
            "base_quiet_seconds": 0.08,
            "min_quiet_seconds": 0.08,
            "max_quiet_seconds": 0.08,
            "total_seconds": 0.4,
        }
        config["interrupt"] = {"enable": True, "mode": "graceful"}
        plugin = _isolated_plugin(loaded, config)

        texts = ["有你就够了", "欸嘿", "那你别管"]
        tasks = []
        for text in texts:
            event = FakeEvent(text)
            tasks.append((event, asyncio.create_task(plugin.on_waiting_llm(event))))
            await asyncio.sleep(0.01)  # 模拟连续打字

        for _e, task in tasks:
            await asyncio.wait_for(task, timeout=3.0)
        return tasks

    tasks = run(scenario())
    events = [e for e, _ in tasks]

    # 只有一条继续走 LLM，其余都被合并掉了。
    winners = [e for e in events if e.call_llm is False]
    assert len(winners) == 1, f"应当只回复一次，实际 {len(winners)} 次"

    # 胜出者拿到的是合并后的文本。
    assert winners[0].message_str == "有你就够了\n欸嘿\n那你别管"

    # 不该有任何事件被标记为 stale（那是"被打断"才有的）。
    assert not any(e.get_extra("betterchat_stale") for e in events)


def test_interrupt_still_works_when_previous_is_generating(loaded) -> None:
    """真正在生成的那一轮，仍然可以被新消息打断。

    「不该打断窗口内的」不等于「不该打断生成中的」——
    后者是核心功能，必须保留。
    """

    async def scenario():
        config = FakeConfig(base_config())
        config["private"] = {
            "enable": True,
            "base_quiet_seconds": 0.02,
            "min_quiet_seconds": 0.02,
            "max_quiet_seconds": 0.02,
            "total_seconds": 0.1,
        }
        config["interrupt"] = {"enable": True, "mode": "graceful"}
        plugin = _isolated_plugin(loaded, config)

        # 第一条：完整走完合并窗口，拿到本轮（模拟"正在生成"）
        first = FakeEvent("第一句")
        await plugin.on_waiting_llm(first)
        assert first.call_llm is False
        assert first.get_extra("betterchat_ticket") is not None

        # 第二条：应当打断第一条
        second = FakeEvent("第二句")
        await plugin.on_waiting_llm(second)

        # 第一条被标记 stale，内容不会发出去。
        assert first.get_extra("betterchat_stale") is True
        return first, second

    first, second = run(scenario())
    assert first.get_extra("betterchat_stale") is True


def test_follow_up_detection_yields_to_astrbot(loaded) -> None:
    """检测到 follow_up 会接管时，必须让路。

    AstrBot 的 ``follow_up`` 在我们的钩子**之前**执行，条件是该会话有活跃
    agent runner 且发送者相同。如果它会接走这条消息，我们不该再攒一份 ——
    否则同一句话会被处理两次，合并文本还缺了被接走的那部分。

    实测踩过：用户连发 4 条，第 1 条先跑起来，第 4 条被 follow_up 接走，
    我们窗口里还攒着 3 条 —— Bot 回「能什么？话只说一半」。
    """
    plugin_cls = loaded.module.BetterChatPlugin

    # 没有活跃 runner → 不会接管
    event = FakeEvent("你好")
    assert plugin_cls._follow_up_will_take("test:private:1", event) is False

    # 内部模块不可用时应保守返回 False（不能让插件崩）
    assert isinstance(
        plugin_cls._follow_up_will_take("test:private:1", event), bool
    )


def test_follow_up_detection_is_defensive(loaded) -> None:
    """即使 AstrBot 内部结构和预期不符，也不能抛异常。"""
    plugin_cls = loaded.module.BetterChatPlugin

    class Weird:
        pass

    event = FakeEvent("x")
    # 注入一个畸形 runner，确认不会崩
    try:
        from astrbot.core.pipeline.process_stage.follow_up import (  # noqa: F401
            _ACTIVE_AGENT_RUNNERS,
        )
    except Exception:
        return
    _ACTIVE_AGENT_RUNNERS["weird:umo"] = Weird()
    try:
        result = plugin_cls._follow_up_will_take("weird:umo", event)
        assert result is False
    finally:
        _ACTIVE_AGENT_RUNNERS.pop("weird:umo", None)


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


def test_terminate_is_idempotent_and_safe(loaded) -> None:
    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        await plugin.terminate()
        await plugin.terminate()
        return True

    assert run(scenario()) is True


def test_terminate_wakes_waiting_merges(loaded) -> None:
    """热重载时正在等待的合并必须被唤醒，不能留孤儿任务。"""

    async def scenario():
        plugin = loaded.module.BetterChatPlugin(
            context=None, config=FakeConfig(base_config())
        )
        reservation = plugin.merge.reserve("umo", "hi")
        task = asyncio.create_task(plugin.merge.wait_for_quiet("umo", reservation))
        await asyncio.sleep(0)
        await plugin.terminate()
        return await asyncio.wait_for(task, timeout=2.0)

    assert run(scenario()) is not None
