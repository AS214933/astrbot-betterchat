"""BetterChat —— AstrBot 插件入口。

两个功能：

* **私聊耐心合并**：用户连续打字时先不回复，等静默窗口结束再把整串消息
  合并成一条输入交给原生 LLM 流程；生成中用户再发消息则立刻中止生成、
  丢弃这条旧回复。
* **群聊 Jev 闸门**：群消息先过本地闸门，再交给 TypeSafe Jev 判断要不要
  回复；判定要回复时即使没被 @ 也会主动插话。

实现要点（都已对照 AstrBot 源码核实）：

* 合并/打断挂在 ``on_waiting_llm_request`` —— 它在拿会话锁**之前**触发，
  能真正卡住本轮，而不是排队。
* 事件始终**走原生管线**，靠改写 ``event.message_str`` 让模型看到合并后的
  文本。这样人设、记忆、工具、会话历史全部保持 AstrBot 原生行为。
* 被打断的旧事件在 ``on_llm_request`` / ``on_decorating_result`` 两级被拦截，
  保证非流式下"一个字都不发"。
"""

from __future__ import annotations

from typing import Any

try:  # 包内导入（AstrBot 正常加载方式）
    from .betterchat_core import (
        Action,
        GroupSettings,
        GroupVerdict,
        MergeSettings,
        MergeWindow,
        ReservationState,
        build_gate_state,
        decide_group_action,
        gate_reason,
        resolve_fail_policy,
    )
    from .betterchat_jev import (
        JevClient,
        JevConfig,
        JevError,
        build_group_questions,
    )
    from .betterchat_state import RateLimiter, RuntimeState
except ImportError:  # 直接以路径加载时的兜底
    from betterchat_core import (
        Action,
        GroupSettings,
        GroupVerdict,
        MergeSettings,
        MergeWindow,
        ReservationState,
        build_gate_state,
        decide_group_action,
        gate_reason,
        resolve_fail_policy,
    )
    from betterchat_jev import JevClient, JevConfig, JevError, build_group_questions
    from betterchat_state import RateLimiter, RuntimeState

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

__all__ = ["BetterChatPlugin"]

#: 存放在事件上的标记，用于跨 handler 传递状态
#: （AstrBot 在每个 handler 之间会 ``clear_result()``，所以不能用 stop 标记）。
EXTRA_TICKET = "betterchat_ticket"
EXTRA_STALE = "betterchat_stale"
EXTRA_GROUP_VERDICT = "betterchat_group_verdict"

#: 合并窗口里要跳过的消息类型 —— 只合并纯文本。
_PLAIN_ONLY = "Plain"


def _plugin_data_dir() -> Any:
    """返回本插件的持久化目录 ``data/plugin_data/astrbot_betterchat``。"""
    from pathlib import Path

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path()) / "astrbot_betterchat"
    except Exception:  # noqa: BLE001 - 路径 API 变了也不能让插件起不来
        base = Path("data") / "plugin_data" / "astrbot_betterchat"
    return base


class BetterChatPlugin(Star):
    """插件主体。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: AstrBotConfig | dict = config or {}

        # 注意：面板每次保存配置都会重建本对象，所以 __init__ 里
        # 绝不能做网络请求或阻塞操作。
        self.merge = MergeWindow(settings=self._merge_settings())
        self.state = RuntimeState()
        self.limiter = RateLimiter(_plugin_data_dir() / "state.json")
        self.jev = JevClient(self._jev_config())

        # Jev 失败日志的去重状态（避免群聊里逐条刷屏）。
        self._last_jev_error: str | None = None
        self._last_jev_error_at: float = 0.0
        self._jev_error_cooldown: float = 300.0

        self._log_startup()

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #

    def _section(self, key: str) -> dict:
        """安全地取一个配置分组。

        ``AstrBotConfig`` 是 dict 子类，且 ``__getattr__`` 对缺失的根键
        返回 None 而不是抛错，所以这里统一用 ``.get``。
        """
        value = self.config.get(key) if isinstance(self.config, dict) else None
        return value if isinstance(value, dict) else {}

    def _enabled(self) -> bool:
        if not isinstance(self.config, dict):
            return False
        return bool(self.config.get("enabled", True))

    def _merge_settings(self) -> MergeSettings:
        return MergeSettings.from_config(self._section("private"))

    def _group_settings(self) -> GroupSettings:
        return GroupSettings.from_config(self._section("group"))

    def _jev_config(self) -> JevConfig:
        return JevConfig.from_config(self._section("jev"))

    def _interrupt_enabled(self) -> bool:
        section = self._section("interrupt")
        return bool(section.get("enable", True)) and str(
            section.get("mode", "graceful")
        ).lower() != "none"

    def _log_startup(self) -> None:
        jev = self.jev.config
        missing = jev.ready_missing()
        if missing:
            logger.warning(
                f"[BetterChat] 群聊 Jev 闸门已禁用（{missing}）。"
                "私聊合并功能不受影响。"
            )
            return

        logger.info(
            f"[BetterChat] Jev 渠道={jev.channel} endpoint={jev.endpoint()} "
            f"model={jev.model if jev.channel == 'official' else jev.cf_model} "
            f"timeout={jev.timeout_seconds}s"
        )
        if jev.channel == "cloudflare":
            # 这是一个很容易踩的坑：Cloudflare 的免费 Neurons 只覆盖自托管
            # 的 @cf/ 模型，第三方模型（typesafe/jev）需要预付额度。
            # 提前说清楚，比等第一条群消息报 402 要好。
            logger.info(
                "[BetterChat] 提示：Cloudflare 渠道的免费 Neurons 额度"
                "只覆盖 @cf/ 自托管模型；typesafe/jev 属于第三方模型，"
                "需要 AI Gateway 预付额度。如遇 402/2021，"
                "请改用「TypeSafe 官方」渠道，即可避免该费用。"
            )

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_plain_text(event: AstrMessageEvent) -> bool:
        """只有纯文本消息才参与合并。

        带图片、文件、语音、合并转发的消息语义复杂，重建消息链容易出错，
        直接放行走原生流程更安全。

        注意：AstrBot 的组件 ``type`` 是 ``ComponentType(str, Enum)``，
        它**同时**继承 ``str`` 和 ``Enum``，所以 ``type(x) is str`` 恒为
        ``False`` —— 必须用等值比较。这个坑曾经让合并功能整体失效：
        每条消息都被判成"非纯文本"而直接放行。
        """
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None)
        if not isinstance(chain, list) or not chain:
            return True
        for comp in chain:
            kind = getattr(comp, "type", None)
            if kind is None:
                continue
            # ComponentType 可以与字符串直接比较（str 混入）。
            if kind == _PLAIN_ONLY:
                continue
            return False
        return True

    @staticmethod
    def _replace_text(event: AstrMessageEvent, text: str) -> None:
        """把合并后的文本就地写回事件。

        刻意**不**重建合成事件：保留最新这条消息原生的 ``message_id``、
        权限与引用关系，才不会破坏回复引用和平台侧行为。
        """
        event.message_str = text
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return
        try:
            import astrbot.api.message_components as Comp

            message_obj.message = [Comp.Plain(text)]
        except Exception:  # noqa: BLE001 - 极端情况下退化为只改 message_str
            logger.debug("[BetterChat] 无法重建消息链，仅改写 message_str", exc_info=True)
        message_obj.message_str = text

    @staticmethod
    def _take_over_turn(event: AstrMessageEvent) -> None:
        """让**本插件**接管本轮的 LLM 调用。

        AstrBot 的放行条件是 ``not event.call_llm``，也就是说
        ``should_call_llm(True)`` 起的是**禁止**作用 —— 方法名和语义是反的
        （源码：``astr_message_event.py`` 的 docstring「是否禁止默认的 LLM
        请求」+ ``process_stage/stage.py`` 的 ``and not event.call_llm``）。
        这里用两个语义明确的小函数包起来，避免在每个调用点重复踩这个坑。
        """
        event.should_call_llm(True)

    @staticmethod
    def _let_astrbot_handle(event: AstrMessageEvent) -> None:
        """把本轮交还给 AstrBot 原生的 LLM 流程。"""
        event.should_call_llm(False)

    def _interrupt_in_flight(
        self,
        umo: str,
        current_event: AstrMessageEvent,
        *,
        previous: AstrMessageEvent | None = None,
    ) -> None:
        """中止本会话里正在进行的生成。"""
        try:
            from astrbot.core.utils.active_event_registry import (
                active_event_registry,
            )
        except ImportError:
            logger.debug("[BetterChat] 当前 AstrBot 版本没有 active_event_registry")
            return
        # 先给旧事件打上 stale 标记，再请求停止。两者缺一不可：
        # 只请求停止的话，AstrBot 会把中止时的那句占位回复正常发出去；
        # 只打标记的话，生成还会继续烧 token。
        target = previous if previous is not None else self.state.in_flight.get(umo)
        if target is not None and target is not current_event:
            try:
                target.set_extra(EXTRA_STALE, True)
            except Exception:  # noqa: BLE001
                pass
        try:
            # 优雅停止：保留已生成的半截内容并正常写入历史，
            # 但配合 on_decorating_result 的拦截不会发给用户。
            count = active_event_registry.request_agent_stop_all(
                umo, exclude=current_event
            )
            if count:
                logger.info(f"[BetterChat] 已请求中止 {count} 个正在进行的生成")
        except Exception:  # noqa: BLE001 - 打断失败不能影响新消息处理
            logger.warning("[BetterChat] 请求中止生成失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # 钩子一：合并 + 打断
    # ------------------------------------------------------------------ #

    @filter.on_waiting_llm_request()
    async def on_waiting_llm(self, event: AstrMessageEvent) -> None:
        """在本轮真正开始请求 LLM 之前介入。"""
        if not self._enabled():
            return

        umo = event.unified_msg_origin
        is_private = event.is_private_chat()

        if is_private:
            settings = self._merge_settings()
            if not settings.enable:
                self._register_in_flight(umo, event)
                return
            if not self._is_plain_text(event):
                self._register_in_flight(umo, event)
                return
            text = (event.message_str or "").strip()
            if not text or settings.should_ignore(text):
                self._register_in_flight(umo, event)
                return
            owns_turn = await self._merge_turn(umo, event, text)
            if owns_turn:
                # 只有真正拿到本轮的事件才算「在跑」，后来者据此判断是否打断。
                self._register_in_flight(umo, event)
        else:
            # 群聊不做合并（要不要回本来就由 Jev 闸门决定），
            # 只在本会话确实有生成在跑时才考虑打断。
            if self._interrupt_enabled():
                self._interrupt_in_flight(umo, event)
            self._register_in_flight(umo, event)

    def _register_in_flight(self, umo: str, event: AstrMessageEvent) -> None:
        """登记本会话当前正在走 LLM 流程的事件。

        必须在**决定了这一轮归谁之后**才登记，否则后来者做打断判断时
        读到的永远是自己。
        """
        self.state.in_flight[umo] = event

    def _spawn_record_reply(self, umo: str) -> None:
        """把这次回复记进限频计数（异步落盘，不阻塞消息流程）。"""
        self.state.spawn(self._record_reply(umo))

    async def _record_reply(self, umo: str) -> None:
        try:
            await self.limiter.record(umo)
        except Exception:  # noqa: BLE001 - 记账失败不能影响回复
            logger.debug("[BetterChat] 限频记账失败", exc_info=True)

    async def _merge_turn(
        self, umo: str, event: AstrMessageEvent, text: str
    ) -> bool:
        """私聊合并的主体流程。

        Returns:
            ``True`` 表示本事件拿到了这一轮（合并文本已写回，应交给原生
            LLM）；``False`` 表示本事件已被更新的一条接管，应静默退出。
        """
        reservation = self.merge.reserve(umo, text)

        # 从这一刻起本事件就是「本会话正在进行的那一轮」，后来的消息据此
        # 判断要不要打断它 —— 此时它还只是在等待合并，并没有真的开始生成。
        # 这恰恰是打断要覆盖的场景：用户在你攒话的时候又补了一句。
        previous = self.state.in_flight.get(umo)
        self._register_in_flight(umo, event)

        run = self.state.session(umo)
        previous_ticket = run.latest_ticket
        run.latest_ticket = reservation.ticket

        # 只有当上一轮确实还挂在那儿（真的在跑）时才打断；
        # 已经跑完的轮次不该被误伤。
        if (
            self._interrupt_enabled()
            and previous is not None
            and previous is not event
            and previous_ticket
            and previous_ticket != reservation.ticket
        ):
            self._interrupt_in_flight(umo, event, previous=previous)

        outcome = await self.merge.wait_for_quiet(
            umo,
            reservation,
            is_current=lambda: not event.is_stopped(),
        )

        if outcome is not ReservationState.OWNER:
            # 被更新的一条接管，或插件被卸载 —— 本事件静默退出。
            # 这里没有 yield 任何结果，``_has_send_oper`` 仍是 False，
            # 所以必须显式禁止默认 LLM，否则 AstrBot 会立刻拿**单条**消息
            # 去请求模型，用户就会看到一句话被回复好几次。
            self._take_over_turn(event)
            return False

        settled = await self.merge.settle(umo, reservation)
        if settled is None:
            self._take_over_turn(event)
            return False

        merged, count = settled
        if count > 1 or merged != text:
            self._replace_text(event, merged)
            logger.info(f"[BetterChat] 合并 {count} 条消息（{len(merged)} 字符）")
        event.set_extra(EXTRA_TICKET, reservation.ticket)
        # 合并完成，交还给 AstrBot 的原生流程（人设/记忆/工具全部保留）。
        self._let_astrbot_handle(event)
        return True

    # ------------------------------------------------------------------ #
    # 钩子二：拦截已失效的事件
    # ------------------------------------------------------------------ #

    @filter.on_llm_request(priority=1000)
    async def on_llm_request(self, event: AstrMessageEvent, _request: Any = None) -> None:
        """被打断的旧事件不许走到模型。"""
        if event.get_extra(EXTRA_STALE, False):
            logger.debug("[BetterChat] 跳过已失效事件的 LLM 请求")
            event.stop_event()

    @filter.on_decorating_result(priority=1000)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """被打断的旧事件不许发出任何内容。

        注意流式输出会整段跳过本钩子（AstrBot 的行为），所以非流式下
        才严格保证"一个字都不发"。
        """
        umo = event.unified_msg_origin
        if self.state.in_flight.get(umo) is event:
            self.state.in_flight.pop(umo, None)

        if not event.get_extra(EXTRA_STALE, False):
            return
        result = event.get_result()
        if result is not None:
            try:
                result.chain = []
            except Exception:  # noqa: BLE001
                pass
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 钩子三：群聊 Jev 闸门
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1000)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """群聊判定入口。

        这个 handler 的 filter 会命中所有群消息，从而让事件通过
        ``WakingCheckStage``（否则未被 @ 的群消息根本不会进入插件）。
        我们只负责"决定要不要让原生 LLM 接手"，不自己发消息。
        """
        if not self._enabled() or event.is_private_chat():
            return

        settings = self._group_settings()
        if not settings.enable:
            return

        umo = event.unified_msg_origin

        if self._is_self_message(event):
            return

        text = (event.message_str or "").strip()
        already_woken = bool(getattr(event, "is_at_or_wake_command", False))

        decision = gate_reason(
            settings=settings,
            text=text,
            is_self=False,
            is_command=self._is_command(text),
            already_woken=already_woken,
            recently_replied=self.limiter.count(umo),
            seconds_since_last_reply=self._seconds_since_limiter_reply(umo),
            seconds_since_bot_spoke=self.state.seconds_since_bot_spoke(umo),
            breaker_open=self.jev.breaker_open,
        )

        if not decision.should_judge:
            if already_woken:
                # 用户叫了 Bot 但本地闸门没过（比如太短）：交还原生流程。
                self._let_astrbot_handle(event)
                return
            # 不需要主动插话：确保不触发默认 LLM，也不让它插话。
            self._take_over_turn(event)
            event.is_at_or_wake_command = False
            return

        verdict = await self._judge_group(event, umo, settings, text, already_woken)
        event.set_extra(EXTRA_GROUP_VERDICT, verdict)

        if verdict.action is Action.RESPOND:
            # 未被唤醒的消息过不了 ProcessStage 的放行条件
            # （``is_at_or_wake_command`` 为假），这里主动置真实现"主动插话"，
            # 并确保不禁止默认 LLM 调用。
            event.is_at_or_wake_command = True
            self._let_astrbot_handle(event)
            # 记账：限频统计必须在这里累加，否则 max_replies_per_hour
            # 永远读到 0，等于没有限频。
            self.state.note_reply(umo)
            self._spawn_record_reply(umo)
            if not already_woken:
                logger.info(
                    f"[BetterChat] 主动插话（{verdict.reason}）"
                    f" addressed={verdict.addressed} question={verdict.open_question}"
                )
            return

        # 判定为不说话：禁止默认 LLM，并撤销主动权。
        self._take_over_turn(event)
        event.is_at_or_wake_command = False
        logger.debug(
            f"[BetterChat] 群聊不回复（{verdict.reason}）"
            f" addressed={verdict.addressed} question={verdict.open_question}"
            f" answered={verdict.already_answered}"
        )

    def _log_jev_failure(self, exc: Exception) -> None:
        """Jev 失败时记日志，但同一条错误在冷却期内只记一次。

        群聊里每条消息都会触发判定，如果服务端一直报同一个错，
        逐条打警告会把日志刷爆，反而淹没了真正有用的信息。
        状态变化时（错误内容不同，或距离上次已过一段时间）才重新提示。
        """
        import time

        message = str(exc)
        now = time.monotonic()
        recently_warned = (
            self._last_jev_error == message
            and now - self._last_jev_error_at < self._jev_error_cooldown
        )
        if recently_warned:
            logger.debug(f"[BetterChat] Jev 判定失败（重复，已折叠）: {message}")
        else:
            logger.warning(f"[BetterChat] Jev 判定失败，降级处理: {message}")
            self._last_jev_error = message
            self._last_jev_error_at = now

    async def _judge_group(
        self,
        event: AstrMessageEvent,
        umo: str,
        settings: GroupSettings,
        text: str,
        already_woken: bool,
    ) -> Any:
        """调用 Jev 并换算成动作，任何失败都降级。"""
        jev_config = self.jev.config
        try:
            state = build_gate_state(
                group_name=self._group_name(event),
                latest=text,
                context=self._recent_group_messages(event, settings.context_messages),
                bot_messages=self._recent_bot_messages(event),
            )
            answers = await self.jev.decide(
                state=state, questions=build_group_questions()
            )
        except JevError as exc:
            self._log_jev_failure(exc)
            # fail_policy 属于群聊闸门配置，不属于 Jev 渠道配置。
            action = resolve_fail_policy(
                settings.fail_policy, already_woken=already_woken
            )
            return GroupVerdict(action, f"jev_error: {exc}")

        return decide_group_action(
            addressed=answers["addressed_to_bot"].value,
            open_question=answers["open_question"].value,
            already_answered=answers["already_answered_by_bot"].value,
            already_woken=already_woken,
            reply_threshold=jev_config.reply_threshold,
            already_answered_threshold=jev_config.already_answered_threshold,
        )

    # ------------------------------------------------------------------ #
    # 事件信息提取
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_self_message(event: AstrMessageEvent) -> bool:
        try:
            return str(event.get_self_id()) == str(event.get_sender_id())
        except Exception:  # noqa: BLE001
            return False

    def _is_command(self, text: str) -> bool:
        if not text:
            return False
        prefixes = list(self._merge_settings().ignore_prefixes)
        try:
            context = getattr(self, "context", None)
            if context is not None:
                for prefix in context.get_config(umo=None).get("wake_prefix", []):
                    if isinstance(prefix, str) and prefix and prefix not in prefixes:
                        prefixes.append(prefix)
        except Exception:  # noqa: BLE001 - 读全局配置失败不影响主流程
            pass
        return any(text.lstrip().startswith(p) for p in prefixes if p)

    def _seconds_since_limiter_reply(self, umo: str) -> float:
        """最近一次主动回复距今的秒数；从未回复过返回 ``inf``。"""
        import math
        import time

        last = self.limiter.last_reply_at(umo)
        if last <= 0:
            return math.inf
        return max(0.0, time.time() - last)

    @staticmethod
    def _group_name(event: AstrMessageEvent) -> str:
        try:
            group = getattr(event, "message_obj", None)
            name = getattr(group, "group_name", None)
            if isinstance(name, str) and name:
                return name
        except Exception:  # noqa: BLE001
            pass
        try:
            return str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _recent_group_messages(self, event: AstrMessageEvent, limit: int) -> list[str]:
        """取最近的群消息作为判定上下文。

        AstrBot 没有插件可直接读的"群历史列表"API，这里尝试几个已知的
        存放位置；任何一个取不到就退化为空列表 —— 判定仍然可用，只是
        上下文少一些，不会影响功能。
        """
        if limit <= 0:
            return []
        for getter in ("group_messages", "history", "recent_messages"):
            try:
                value = getattr(event.message_obj, getter, None)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, list) and value:
                return [str(item) for item in value[-limit:]]
        return []

    def _recent_bot_messages(self, event: AstrMessageEvent) -> list[str]:
        for getter in ("group_bot_messages", "bot_messages"):
            try:
                value = getattr(event.message_obj, getter, None)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, list) and value:
                return [str(item) for item in value[-5:]]
        return []

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #

    @filter.command("betterchat")
    async def betterchat_command(self, event: AstrMessageEvent):
        """BetterChat 诊断指令。

        用法:
            /betterchat status     查看当前配置与运行状态
            /betterchat gate <文本> 干跑一次群聊判定（不回复）
            /betterchat reset       清空本会话的限频计数
        """
        parts = (event.message_str or "").split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else "status"
        arg = parts[2] if len(parts) > 2 else ""

        if sub == "status":
            yield event.plain_result(self._status_text(event))
            return

        if sub == "gate":
            if not arg.strip():
                yield event.plain_result("用法：/betterchat gate <要判定的文本>")
                return
            yield event.plain_result(
                await self._dry_run_gate(event, arg.strip())
            )
            return

        if sub == "reset":
            if not event.is_admin():
                yield event.plain_result("只有管理员可以清空限频计数。")
                return
            await self.limiter.reset(event.unified_msg_origin)
            yield event.plain_result("已清空本会话的限频计数。")
            return

        yield event.plain_result(
            "BetterChat 指令：\n"
            "/betterchat status —— 查看配置与运行状态\n"
            "/betterchat gate <文本> —— 干跑一次群聊判定\n"
            "/betterchat reset —— 清空本会话限频（管理员）"
        )

    def _status_text(self, event: AstrMessageEvent) -> str:
        merge = self._merge_settings()
        group = self._group_settings()
        jev = self.jev.config
        umo = event.unified_msg_origin
        missing = jev.ready_missing()

        lines = ["BetterChat 运行状态", "───────────────"]
        lines.append(f"总开关：{'开' if self._enabled() else '关'}")
        lines.append("")
        lines.append("私聊合并")
        lines.append(f"  启用：{'是' if merge.enable else '否'}")
        lines.append(
            f"  静默窗口：{merge.min_quiet_seconds}~{merge.max_quiet_seconds}s"
            f"（基准 {merge.base_quiet_seconds}s，每 {merge.length_scale_chars} 字 +1s）"
        )
        lines.append(f"  整轮上限：{merge.total_seconds}s")
        lines.append(
            f"  单轮上限：{merge.max_messages or '∞'} 条 / {merge.max_chars or '∞'} 字"
        )
        lines.append(f"  打断：{'启用' if self._interrupt_enabled() else '关闭'}")
        lines.append("")
        lines.append("群聊闸门")
        lines.append(f"  启用：{'是' if group.enable else '否'}")
        lines.append(
            f"  判定范围：{'所有群消息（可主动插话）' if group.judge_all_messages else '仅被 @ / 引用'}"
        )
        lines.append(
            f"  限频：{group.max_replies_per_hour or '∞'} 次/小时，"
            f"间隔 ≥{group.min_interval_seconds}s，冷却 {group.bot_cooldown_seconds}s"
        )
        lines.append(f"  本会话最近一小时已回复：{self.limiter.count(umo)} 次")
        lines.append(f"  失败策略：{group.fail_policy}")
        lines.append("")
        lines.append("Jev")
        lines.append(f"  渠道：{jev.channel}")
        lines.append(f"  端点：{jev.endpoint()}")
        lines.append(f"  模型：{jev.model if jev.channel == 'official' else jev.cf_model}")
        lines.append(f"  Key：{'已配置' if not missing else '未配置（闸门已禁用）'}")
        if missing:
            lines.append(f"  缺失：{missing}")
        lines.append(f"  超时：{jev.timeout_seconds}s，并发 {jev.max_concurrency}")
        breaker = (
            f"熔断中（剩余 {self.jev.breaker_remaining():.0f}s）"
            if self.jev.breaker_open
            else "正常"
        )
        lines.append(f"  熔断状态：{breaker}")
        if self.jev.last_error:
            lines.append(f"  最近错误：{self.jev.last_error}")
        lines.append(f"  阈值：回复 {jev.reply_threshold}，已回答 {jev.already_answered_threshold}")
        return "\n".join(lines)

    async def _dry_run_gate(self, event: AstrMessageEvent, text: str) -> str:
        """干跑：真的调用 Jev，但只打印结果，不改变回复行为。"""
        jev = self.jev.config
        missing = jev.ready_missing()
        if missing:
            return f"Jev 不可用：{missing}"

        settings = self._group_settings()
        already_woken = bool(getattr(event, "is_at_or_wake_command", False))

        try:
            answers = await self.jev.decide(
                state=build_gate_state(
                    group_name=self._group_name(event) or "(干跑)",
                    latest=text,
                    context=self._recent_group_messages(event, settings.context_messages),
                    bot_messages=self._recent_bot_messages(event),
                ),
                questions=build_group_questions(),
            )
        except JevError as exc:
            return f"Jev 判定失败：{exc}"

        addressed = answers["addressed_to_bot"].value
        question = answers["open_question"].value
        answered = answers["already_answered_by_bot"].value

        lines = ["BetterChat 群聊判定（干跑，不会回复）", "───────────────"]
        lines.append(f"文本：{text[:60]}{'…' if len(text) > 60 else ''}")
        lines.append(f"addressed_to_bot       = {addressed:.3f}")
        lines.append(f"open_question          = {question:.3f}")
        lines.append(f"already_answered_by_bot= {answered:.3f}")
        lines.append("")
        lines.append("阈值敏感性：")
        for threshold in (0.5, 0.6, 0.7, 0.8, 0.9):
            verdict = decide_group_action(
                addressed=addressed,
                open_question=question,
                already_answered=answered,
                already_woken=already_woken,
                reply_threshold=threshold,
                already_answered_threshold=jev.already_answered_threshold,
            )
            marker = " ←当前" if abs(threshold - jev.reply_threshold) < 1e-9 else ""
            lines.append(
                f"  ≥{threshold:.1f} → {verdict.action.value}（{verdict.reason}）{marker}"
            )
        lines.append("")
        final = decide_group_action(
            addressed=addressed,
            open_question=question,
            already_answered=answered,
            already_woken=already_woken,
            reply_threshold=jev.reply_threshold,
            already_answered_threshold=jev.already_answered_threshold,
        )
        lines.append(f"当前结论：{final.action.value}（{final.reason}）")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def terminate(self) -> None:
        """插件卸载 / 热重载清理。

        面板每次保存配置都会走这里，所以必须干净：唤醒等待者、取消
        后台任务、关闭 HTTP 连接，且不能抛异常。
        """
        try:
            await self.merge.close()
        except Exception:  # noqa: BLE001
            logger.debug("[BetterChat] 关闭合并窗口失败", exc_info=True)
        try:
            await self.state.close()
        except Exception:  # noqa: BLE001
            logger.debug("[BetterChat] 清理后台任务失败", exc_info=True)
        try:
            await self.jev.close()
        except Exception:  # noqa: BLE001
            logger.debug("[BetterChat] 关闭 Jev 连接失败", exc_info=True)
        logger.info("[BetterChat] 已卸载")
