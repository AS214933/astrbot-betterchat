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

import asyncio
import re
from typing import Any

try:  # 包内导入（AstrBot 正常加载方式）
    from .betterchat_core import (
        Action,
        ChatLine,
        GroupAggregator,
        GroupSettings,
        GroupVerdict,
        MergeSettings,
        MergeWindow,
        ReservationState,
        build_gate_state,
        decide_group_action,
        gate_reason,
        merge_group_lines,
        resolve_fail_policy,
    )
    from .betterchat_jev import (
        JevClient,
        JevConfig,
        JevError,
        NoulQuestion,
        build_group_questions,
    )
    from .betterchat_group_extras import (
        ProactiveAction,
        ProactiveSettings,
        RereadDetector,
        RereadSettings,
        RereadVerdict,
        SilenceTracker,
        build_proactive_questions,
        make_fingerprint,
        proactive_gate,
        reread_gate,
    )
    from .betterchat_state import RateLimiter, RuntimeState, TranscriptCache
except ImportError:  # 直接以路径加载时的兜底
    from betterchat_core import (
        Action,
        ChatLine,
        GroupAggregator,
        GroupSettings,
        GroupVerdict,
        MergeSettings,
        MergeWindow,
        ReservationState,
        build_gate_state,
        decide_group_action,
        gate_reason,
        merge_group_lines,
        resolve_fail_policy,
    )
    from betterchat_jev import (
        JevClient,
        JevConfig,
        JevError,
        NoulQuestion,
        build_group_questions,
    )
    from betterchat_group_extras import (
        ProactiveAction,
        ProactiveSettings,
        RereadDetector,
        RereadSettings,
        RereadVerdict,
        SilenceTracker,
        build_proactive_questions,
        make_fingerprint,
        proactive_gate,
        reread_gate,
    )
    from betterchat_state import RateLimiter, RuntimeState, TranscriptCache

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

#: 文本形式的 @ 标记。
#:
#: QQ 官方平台（qq_official）的消息适配器**只把 @Bot 自己**的 mention 转成
#: ``At`` 组件，@ 别人的 mention 会被原样留在 ``message_str`` 里，形如
#: ``<@2879588DE5953607C534F862B4AFFE5C>``。所以光看 ``At`` 组件在那边
#: 永远检测不到"@ 了别人"。这里把三种已知写法都覆盖：
#:
#: * ``<@openid>``            —— QQ 官方群聊
#: * ``<@!123456>``           —— Discord，``!`` 表示昵称
#: * ``<qqbot-at-user id="…"/>`` —— QQ 官方的另一种标记
_AT_MARKUP_RE = re.compile(
    r"<qqbot-at-user\s+id=\"([^\"]+)\"\s*/>"   # 组 1
    r"|<@!?([^>\s]+)>",                        # 组 2
)


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
        self.group_agg = GroupAggregator()
        self.state = RuntimeState()
        self.limiter = RateLimiter(_plugin_data_dir() / "state.json")
        # 群聊上下文缓存。AstrBot 的 event 只带当前这一条消息，
        # 要"最近几条群消息"只能自己记（同类插件也都是这么做的）。
        self.transcripts = TranscriptCache(maxlen=30)
        self.jev = JevClient(self._jev_config())

        # Jev 失败日志的去重状态（避免群聊里逐条刷屏）。
        self._last_jev_error: str | None = None
        self._last_jev_error_at: float = 0.0
        self._jev_error_cooldown: float = 300.0
        #: 聚合时收集到的其他消息，送去判定后即清空。
        self._pending_group_context: dict[str, list] = {}

        # ---- 自动复读 / 防冷场 ----
        self.transcripts = self.transcripts  # 复用上面的上下文缓存
        self._reread_detectors: dict[str, RereadDetector] = {}
        self.silence = SilenceTracker()
        #: 每群一个"冷场检查"定时任务，新消息来了就取消。
        self._silence_tasks: dict[str, asyncio.Task] = {}
        #: 每群一个"观望待帮"的任务：有问题没人接时先等着，有人接话就取消。
        self._help_tasks: dict[str, asyncio.Task] = {}

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

    def _reread_settings(self) -> RereadSettings:
        return RereadSettings.from_config(self._section("reread"))

    def _proactive_settings(self) -> ProactiveSettings:
        return ProactiveSettings.from_config(self._section("proactive"))

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
            f"model={jev.active_model()} "
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

    def _is_generating(self, event: AstrMessageEvent) -> bool:
        """这条事件是否**已经开始生成回复**（而不是还在合并窗口里等）。

        关键区别：还在窗口里等待的事件绝不能被打断 —— 否则用户连发两条时，
        第二条会把第一条打断，合并永远不可能发生。

        判据用我们自己维护的标记 ``EXTRA_TICKET``：它只在本事件**成功结算**
        （拿到本轮、写回合并文本）时设置。没有这个标记 = 还在等，或者
        根本没进过窗口。
        """
        if event.get_extra(EXTRA_TICKET) is not None:
            return True
        # 兜底：已经有产出结果，说明确实在生成。
        try:
            result = event.get_result()
            if result is not None and getattr(result, "chain", None):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    @staticmethod
    def _follow_up_will_take(umo: str, event: AstrMessageEvent) -> bool:
        """AstrBot 的 ``follow_up`` 机制会不会接走这条消息。

        为什么必须检测：``follow_up`` 在我们的钩子**之前**执行
        （``internal.py`` 里 ``try_capture_follow_up`` 在
        ``OnWaitingLLMRequestEvent`` 之前）。它的条件是「该会话有活跃
        agent runner 且发送者相同」。

        如果它会接走这条消息，我们就**不该**把它攒进合并窗口：
        - 它的内容会作为 follow-up 塞进正在跑的那一轮
        - 我们攒着的副本到期后又会触发第二轮，两边抢消息

        实测发生过：用户连发 4 条，第 1 条先走 LLM 起了 runner，
        第 4 条被 follow_up 接走，而我们窗口里还攒着 3 条 ——
        结果合并出来的文本缺了最后一句，Bot 回「话只说一半」。
        """
        try:
            from astrbot.core.pipeline.process_stage.follow_up import (
                _ACTIVE_AGENT_RUNNERS,
            )
        except Exception:  # noqa: BLE001 - 内部模块不可用时保守返回 False
            return False
        try:
            runner = _ACTIVE_AGENT_RUNNERS.get(umo)
            if runner is None:
                return False
            runner_event = getattr(
                getattr(runner.run_context, "context", None), "event", None
            )
            if runner_event is None:
                return False
            return str(runner_event.get_sender_id()) == str(event.get_sender_id())
        except Exception:  # noqa: BLE001
            return False

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
            text = (event.message_str or "").strip()
            if text and settings.should_ignore(text):
                self._register_in_flight(umo, event)
                return
            # 判断该「打断重来」还是「让 follow_up 并进当前回复」。
            #
            # 顺序很重要，这里踩过坑：早期把 _follow_up_will_take 的检查
            # 放在 _merge_turn **之前**，结果只要当前有活跃 runner，新消息
            # 就永远走 follow_up 让路 —— **打断功能彻底失效**。
            #
            # 现在的规则：
            #   * 打断开启时 → 先让 _merge_turn 处理（它会决定要不要打断）
            #   * 打断关闭时 → 让路给 follow_up，别和它抢消息
            if not self._interrupt_enabled() and self._follow_up_will_take(
                umo, event
            ):
                logger.debug("[BetterChat] 打断已关闭，交给 follow_up 处理")
                self._register_in_flight(umo, event)
                return
            # 非纯文本（图片 / 表情 / 文件）也进入合并窗口，只是不参与
            # 文本拼接 —— 它这一轮必须**陪着**前面攒下的文字一起走。
            #
            # 曾经这里直接 return 放行，导致：用户连发两句 + 一个表情时，
            # 表情会绕过窗口单独触发一次 LLM 请求，而攒着的两句到期后又
            # 触发第二次 —— 一次行为产生两个回复，还互相排队拖慢。
            if not text and self._is_plain_text(event):
                # 空消息（纯空白）没什么可合并的，交还原生流程。
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

        # 判断上一轮处于哪个阶段，这决定了本次该做什么：
        #
        #   * 还在**合并窗口**里（没开始生成）→ 加入它，一起合并
        #   * 已经**在生成**            → 按配置打断它，重新开始
        #
        # 早期版本只看了 in_flight 有没有值，于是把"还在窗口里等"的上一轮
        # 也当成"正在生成"去打断。结果用户连发两条时，第二条会把第一条
        # 打断，然后自己成为新的 in_flight，第三条再打断它…… 连锁下去
        # 就变成了"每条消息各自回复一次"，合并形同虚设。
        previous = self.state.in_flight.get(umo)
        self._register_in_flight(umo, event)

        run = self.state.session(umo)
        previous_ticket = run.latest_ticket
        run.latest_ticket = reservation.ticket

        if (
            self._interrupt_enabled()
            and previous is not None
            and previous is not event
            and previous_ticket
            and previous_ticket != reservation.ticket
            and self._is_generating(previous)
        ):
            self._interrupt_in_flight(umo, event, previous=previous)
            # 打断场景必须**立即响应**，不能再去等合并窗口 ——
            # 用户刚说完一条新的，期待的就是「马上重来」，
            # 再等 4 秒等于白打断。
            logger.info("[BetterChat] 打断上一轮，立即开始新回复")
            merged, count = text, 1
            turn = self.merge._turns.pop(umo, None)  # noqa: SLF001
            if turn is not None:
                # 丢弃本次登记的窗口，避免它到期后又触发一轮。
                self.merge._settled[umo] = reservation.ticket  # noqa: SLF001
            if merged:
                self._replace_text(event, merged)
            event.set_extra(EXTRA_TICKET, reservation.ticket)
            self._let_astrbot_handle(event)
            return True

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
            # 合并结果为空而本事件是图片/表情时，**不要**用空串覆盖它 ——
            # 那样会把消息内容清掉。让原生流程照常处理这条非文本消息，
            # 前面攒的文字已经作为会话历史进入了上下文。
            if merged:
                self._replace_text(event, merged)
                logger.info(f"[BetterChat] 合并 {count} 条消息（{len(merged)} 字符）")
            elif not text:
                logger.debug(
                    "[BetterChat] 本轮只有非文本消息，保留原消息内容"
                )
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

        text = (event.message_str or "").strip()

        # 先把这条消息记进上下文缓存，再去判定。
        # 必须在闸门之前 —— 被本地闸门拦下的消息同样是群聊的一部分，
        # 少了它们，后面"有没有人接话"的判断就会缺片。
        if not self._is_self_message(event):
            self._remember_group_message(umo, event, text)

        if self._is_self_message(event):
            return

        # ---- 复读 / 冷场计时（都在判定之前） ----

        # 有人说话了 → 记录沉默计时，并取消待执行的冷场发言 / 观望待帮。
        self.silence.note_message(umo)
        self._cancel_silence_task(umo)
        self._cancel_help_task(umo)

        # 复读检测：命中则直接跟读并吞掉这条消息，不再走后续判定。
        reread_outcome = await self._try_reread(event, umo, settings)
        if reread_outcome is not None:
            return

        # 安排下一次冷场检查（有人说话就把计时重置）。
        self._schedule_silence_check(umo, event)

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

        # ---- 判定前聚合 ----
        # 被 @ / 引用的消息不聚合：用户明确叫了 Bot，必须立刻响应。
        # 只有「未唤醒的主动插话」才值得攒一下 —— 群里连刷三句只花一次调用，
        # 而且 open_question 看的是合并后的完整语义，不是被打断的半句。
        window = 0.0 if already_woken else settings.aggregate_window_seconds
        if window > 0:
            state = await self._aggregate_group(event, umo, settings, window, text)
            if state is None:
                return
            text, merged_count = state
            if merged_count > 1:
                logger.info(
                    f"[BetterChat] 聚合 {merged_count} 条群消息为一次判定"
                )

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

        if verdict.action is Action.DEFER:
            # 有个问题还没人接 —— 不立即插话（可能有人正在打字），
            # 先观望一段时间；期间有人接话就取消。
            self._take_over_turn(event)
            event.is_at_or_wake_command = False
            self._schedule_help(umo, settings, text)
            logger.info(
                f"[BetterChat] 观望中（{verdict.reason}）"
                f" addressed={verdict.addressed} question={verdict.open_question}"
                f" 等待 {settings.help_unaddressed_delay_seconds}s"
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

    async def _aggregate_group(
        self,
        event: AstrMessageEvent,
        umo: str,
        settings: GroupSettings,
        window: float,
        text: str,
    ) -> tuple[str, int] | None:
        """决定本轮判定用哪段文本。

        Returns:
            ``(用于判定的文本, 合并条数)``；``None`` 表示**本事件不该继续**，
            调用方应当直接返回（它已经被并进另一条消息的判定里）。
        """
        line = ChatLine(
            sender=self._sender_name(event),
            text=text,
            is_self=False,
            at_bot=bool(getattr(event, "is_at_or_wake_command", False)),
            seconds_ago=self._seconds_since_created(event),
        )
        reservation = self.group_agg.reserve(umo, line, window_seconds=window)

        # 所有到达者都进入等待，由最终仍是 owner 的那条收口。
        # （不能因为"我并入了别的窗口"就提前返回 —— 并发下可能出现
        #  先到者被顶掉、后到者都已退出，导致没有人负责判定。）
        outcome = await self.group_agg.wait(umo, reservation)
        if outcome is not ReservationState.OWNER:
            self._take_over_turn(event)
            event.is_at_or_wake_command = False
            return None

        batch = await self.group_agg.settle(umo, reservation)
        if not batch:
            return text, 1

        latest = merge_group_lines(batch)
        # 合并时其他人说的内容作为上下文一并送进去，判定更准。
        self._pending_group_context[umo] = [b for b in batch[:-1] if b.text]
        return latest.text or text, len(batch)

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
                context=self._group_context(event, umo, settings),
                bot_messages=self._recent_bot_messages(event),
                **self._gate_state_extras(event, already_woken),
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
            # @ 了别人 → 不抢答两人之间的对话。
            addressed_to_other=self.at_other(event),
            # 「主动帮没人接的问题」默认关闭 —— 群里两人正常对话时
            # 「有人提问还没人答」太常见，放行等于见缝插针。
            help_unaddressed=settings.help_unaddressed,
            reply_threshold=jev_config.reply_threshold,
            already_answered_threshold=jev_config.already_answered_threshold,
        )

    # ------------------------------------------------------------------ #
    # 事件信息提取
    # ------------------------------------------------------------------ #

    @staticmethod
    def at_other(event: AstrMessageEvent) -> bool:
        """这条消息是否 @ 了**别人**（不是 Bot）。

        必须**双轨检测**，因为不同平台暴露 @ 的方式不一样：

        * **通用平台**（OneBot / Telegram / Discord 等）：@ 会变成 ``At``
          组件，带 ``qq`` 字段。
        * **QQ 官方平台**（qq_official）：适配器**只把 @Bot 自己**的 mention
          转成 ``At`` 组件，**@ 别人的 mention 被原样留在 ``message_str``
          里**，形如 ``<@2879588DE5953607C534F862B4AFFE5C>``。
          来源：``qqofficial_platform_adapter.py`` 的
          ``bot_mentions = [m for m in message.mentions if m.is_you is True]``
          以及随后的 ``if group_mentioned: msg.append(At(...))``。

        只查 ``At`` 组件会在 QQ 官方平台上永远返回 False。这个坑很隐蔽：
        单测能过（构造的假消息链里有 At），线上却完全不生效。
        """
        self_id = str(event.get_self_id() or "")

        # 轨道一：标准 At 组件。
        try:
            for comp in event.get_messages():
                kind = getattr(comp, "type", None)
                if str(getattr(kind, "value", kind)) != "At":
                    continue
                target = str(getattr(comp, "qq", "") or "")
                if not target or target in (self_id, "all"):
                    continue
                return True
        except Exception:  # noqa: BLE001 - 消息链异常时继续走文本检测
            pass

        # 轨道二：文本里的 @ 标记（QQ 官方把 @别人 留成了纯文本）。
        text = (event.message_str or "").strip()
        if not text:
            return False
        for match in _AT_MARKUP_RE.finditer(text):
            target = match.group(1) or match.group(2) or ""
            if target and target not in (self_id, "all"):
                return True
        return False

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

    def _gate_state_extras(
        self, event: AstrMessageEvent, already_woken: bool
    ) -> dict[str, object]:
        """给 ``build_gate_state`` 补上"事实类"上下文。

        这些**不是**提示词，是 Jev 做判断必需的事实：

        * ``bot_name`` —— 不告诉它 Bot 叫什么，``addressed_to_bot``
          就只能瞎猜"@后面的那串是不是指我"。
        * ``latest_sender`` —— 区分"谁在说话"，对判断是不是在对 Bot 说
          很有帮助。
        * ``bot_mentioned`` —— 平台层面已经确定的事实（被 @ / 被引用 /
          唤醒词），比让模型从文本里推断可靠得多。
        """
        return {
            "bot_name": self._bot_name(event),
            "latest_sender": self._sender_name(event),
            "bot_mentioned": bool(already_woken),
            # 「@ 了别人」也是平台层面的事实，必须一起给 Jev。
            "addressed_to_other": self.at_other(event),
            # 平台给了创建时间就带上"多久以前"：Jev 能据此区分
            # "刚刚正在聊" 和 "5 分钟前没人接的老问题"。
            "latest_seconds_ago": self._seconds_since_created(event),
        }

    @staticmethod
    def _seconds_since_created(event: AstrMessageEvent) -> float | None:
        """消息距今多少秒；平台没给时间就返回 None（不编造 0）。"""
        import time

        for attr in ("created_at", "timestamp"):
            try:
                value = getattr(event.message_obj, attr, None)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, (int, float)) and value > 0:
                return max(0.0, time.time() - float(value))
        return None

    def _bot_name(self, event: AstrMessageEvent) -> str:
        """取 Bot 的显示名。取不到就退回 ID，总比没有强。"""
        for getter in ("get_self_name", "get_bot_name"):
            try:
                value = getattr(event, getter, None)
                if callable(value):
                    name = value()
                    if isinstance(name, str) and name.strip():
                        return name.strip()
            except Exception:  # noqa: BLE001
                continue
        try:
            return str(event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        try:
            name = event.get_sender_name()
            if isinstance(name, str) and name.strip():
                return name.strip()
        except Exception:  # noqa: BLE001
            pass
        try:
            return str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return ""

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

    def _group_context(
        self, event: AstrMessageEvent, umo: str, settings: GroupSettings
    ) -> list:
        """本次判定要带的上下文。

        优先用聚合窗口里收集到的消息（它们是刚刚连续发生的，比历史
        新鲜得多），取完后立即清空，避免下一条判定重复带上。
        """
        merged = self._pending_group_context.pop(umo, [])
        history = self._recent_group_messages(event, settings.context_messages)
        if not merged:
            return history
        # 聚合消息在前（更近），历史在后。
        return list(merged) + list(history)[-max(0, settings.context_messages - len(merged)):]

    def _recent_group_messages(self, event: AstrMessageEvent, limit: int) -> list:
        """取最近的群消息作为判定上下文（结构化）。

        为什么不是一串裸字符串：判断"该不该接话"时，**谁说的**、**是不是
        Bot 自己说的**、**多久以前**都直接影响结论 —— 一条 5 分钟前没人接
        的问题可以主动帮，刚刚别人正聊得热的话题就不该硬插。这也正是
        ``astrbot_plugin_reply_gate`` 的做法，值得照搬。

        AstrBot 没有插件可直接读的"群历史列表"API，这里尝试几个已知的
        存放位置；任何一个取不到就退化为空列表 —— 判定仍然可用，只是
        上下文少一些，不会影响功能。
        """
        if limit <= 0:
            return []
        # 从自己维护的缓存里取（AstrBot 的 event 只有当前这一条消息，
        # 没有"最近 N 条"这种东西）。
        return self.transcripts.recent(event.unified_msg_origin, limit)

    # ------------------------------------------------------------------ #
    # 自动复读
    # ------------------------------------------------------------------ #

    def _reread_detector(self, umo: str) -> RereadDetector:
        det = self._reread_detectors.get(umo)
        if det is None:
            det = RereadDetector()
            self._reread_detectors[umo] = det
        return det

    async def _try_reread(
        self, event: AstrMessageEvent, umo: str, settings: GroupSettings
    ) -> RereadVerdict | None:
        """复读检测与跟读。

        Returns:
            ``None`` 表示不是复读，调用方继续正常流程；
            返回任何 :class:`RereadVerdict` 都表示**已处理完毕，调用方应直接返回**
            （即使这次没跟读，也要把消息吞掉 —— 已经确认是复读场景了，
            再让它触发一次 LLM 回复会很怪）。
        """
        import random

        cfg = self._reread_settings()
        if not cfg.enable:
            return None

        text = (event.message_str or "").strip()
        # 指令不参与复读。
        if text and any(text.lstrip().startswith(p) for p in cfg.ignore_prefixes):
            return None

        chain = self._message_chain(event)
        fingerprint_pair = make_fingerprint(chain)
        if fingerprint_pair is None:
            return None
        kind, fingerprint = fingerprint_pair

        detector = self._reread_detector(umo)
        if not detector.observe(
            kind, fingerprint, str(event.get_sender_id()), threshold=cfg.threshold_for(kind)
        ):
            return None

        # 确认是复读，再决定这次要不要真的跟。
        recent = detector.recent_follows()
        seconds_since = (
            self._clock() - recent[-1] if recent else float("inf")
        )
        verdict, reason = reread_gate(
            settings=cfg,
            recent_follows=recent,
            seconds_since_last_follow=seconds_since,
            roll=random.random(),
        )

        if verdict is RereadVerdict.TRIGGERED:
            try:
                # 复读的灵魂就是"原样再发一遍"，所以直接转发原消息链，
                # **不经过 LLM** —— 让模型生成反而会破坏它。
                await event.send(event.make_result().chain_result(list(chain)))
                detector.mark_followed(fingerprint)
                self.silence.note_bot(umo)
                logger.info(f"[BetterChat] 自动复读 {kind}（{reason}）")
            except Exception:  # noqa: BLE001 - 发送失败不能影响消息流
                logger.warning("[BetterChat] 复读发送失败", exc_info=True)
            # 无论成功失败都吞掉这条消息，不再走唤醒判定。
            self._take_over_turn(event)
            return verdict

        logger.debug(f"[BetterChat] 检测到复读但本次不跟（{reason}）")
        self._take_over_turn(event)
        return verdict

    @staticmethod
    def _clock() -> float:
        """单调时钟。抽成方法便于测试注入。"""
        import time

        return time.monotonic()

    @staticmethod
    def _message_chain(event: AstrMessageEvent) -> list:
        chain = getattr(getattr(event, "message_obj", None), "message", None)
        return list(chain) if isinstance(chain, list) else []

    # ------------------------------------------------------------------ #
    # 防冷场
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # 观望待帮：有问题但暂时没人接
    # ------------------------------------------------------------------ #

    def _cancel_help_task(self, umo: str) -> None:
        task = self._help_tasks.pop(umo, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_help(
        self, umo: str, settings: GroupSettings, question: str
    ) -> None:
        """登记一次「观望」，到期再看要不要帮。

        为什么不是立即帮：群里两个人对话时「有人提问、还没人回答」是
        最常见的状态 —— 对方可能正在打字。立即插话就是抢答，
        等一个观望期才像真人。
        """
        delay = settings.help_unaddressed_delay_seconds
        self._cancel_help_task(umo)
        if delay <= 0:
            # 不观望：交给下面的检查直接放行。
            delay = 0.0
        task = self.state.spawn(self._help_check(umo, settings, question, delay))
        if task is not None:
            self._help_tasks[umo] = task

    async def _help_check(
        self,
        umo: str,
        settings: GroupSettings,
        question: str,
        delay: float,
    ) -> None:
        """观望期满后，确认确实没人接，再主动帮一次。"""
        import datetime

        try:
            if delay > 0:
                await asyncio.sleep(delay)

            # 观望期内有人说话 → 说明话题还在继续，不插嘴。
            silence = self.silence.silence_seconds(umo)
            if silence < max(delay, 1.0):
                logger.debug(
                    f"[BetterChat] 观望取消：有人接话了（安静 {silence:.0f}s）"
                )
                return

            # 走和其他主动发言一样的本地闸门。
            today = datetime.date.today().isoformat()
            state = self.silence.group(umo)
            action, reason = proactive_gate(
                settings=self._proactive_settings(),
                silence_seconds=silence,
                seconds_since_bot=self.silence.seconds_since_bot(umo),
                proactive_today=self.silence.proactive_count(umo, day_stamp=today),
                message_count=state.message_count,
                hour_of_day=datetime.datetime.now().hour,
            )
            if action is not ProactiveAction.SPEAK:
                logger.debug(f"[BetterChat] 观望到期但闸门拦截（{reason}）")
                return

            logger.info("[BetterChat] 观望期满，主动帮忙回答问题")
            await self._speak_proactively(
                umo,
                event=None,
                prompt=(
                    f"（群里有人提了个问题，等了 {delay:.0f} 秒都没人回答："
                    f"{question[:200]}　你可以自然地帮着答一句，"
                    "就像群友随口接话那种。直接说答案就好，"
                    "不要复述这条说明，也不要解释你在做什么。"
                    "如果确实答不上来，就只回一个句号。）"
                ),
            )
            self.silence.bump_proactive(umo, day_stamp=today)

        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 观望失败绝不能影响聊天
            logger.warning("[BetterChat] 观望待帮异常", exc_info=True)

    # ------------------------------------------------------------------ #
    # 防冷场
    # ------------------------------------------------------------------ #

    def _cancel_silence_task(self, umo: str) -> None:
        task = self._silence_tasks.pop(umo, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_silence_check(
        self, umo: str, event: AstrMessageEvent
    ) -> None:
        """有人说话后，安排一次"到时候看看是不是冷场了"的检查。

        刻意用「每条消息重置一个延迟任务」而不是后台轮询：
        群正常聊天时，旧任务不停被取消，几乎没有开销。
        """
        cfg = self._proactive_settings()
        if not cfg.enable:
            return
        self._cancel_silence_task(umo)
        task = self.state.spawn(self._silence_check(umo, event, cfg))
        if task is not None:
            self._silence_tasks[umo] = task

    async def _silence_check(
        self,
        umo: str,
        event: AstrMessageEvent,
        cfg: ProactiveSettings,
    ) -> None:
        """等到沉默阈值后，判断要不要主动开口。"""
        import datetime
        import random

        try:
            # 稍微多等一点随机量，避免整点整齐开口（显得像定时任务）。
            jitter = random.uniform(0, min(60.0, cfg.silence_seconds * 0.1))
            await asyncio.sleep(cfg.silence_seconds + jitter)

            state = self.silence.group(umo)
            today = datetime.date.today().isoformat()
            action, reason = proactive_gate(
                settings=cfg,
                silence_seconds=self.silence.silence_seconds(umo),
                seconds_since_bot=self.silence.seconds_since_bot(umo),
                proactive_today=self.silence.proactive_count(umo, day_stamp=today),
                message_count=state.message_count,
                hour_of_day=datetime.datetime.now().hour,
            )
            if action is not ProactiveAction.SPEAK:
                logger.debug(f"[BetterChat] 冷场检查跳过（{reason}）")
                return

            # 交给 Jev 判：现在开口合适吗。
            answers = await self.jev.decide(
                state=self._proactive_state(umo, cfg),
                questions=build_proactive_questions(NoulQuestion),
            )
            should_speak = answers["should_speak"].value
            has_topic = answers["has_topic"].value
            mood_fit = answers["mood_fit"].value

            # 组合策略（实测调过）：
            #
            # 只看 should_speak 太严 —— 实测「有人提了个没人接的问题」这种
            # 明显该开口的场景，should_speak 只给 0.56，而 has_topic 高达
            # 0.88。三个信号要一起看：
            #
            #   气氛不合适（mood_fit 低）  → 一律不说（吵架/严肃话题）
            #   有话可接 且 气氛允许        → 说
            #   纯 should_speak 很高        → 说（兜底）
            #
            # 阈值偏保守：宁可少说，也别在群里刷存在感。
            speak = False
            if mood_fit >= 0.5:
                if has_topic >= 0.6 and should_speak >= 0.4:
                    speak = True
                elif should_speak >= 0.8:
                    speak = True

            if not speak:
                logger.debug(
                    f"[BetterChat] 冷场不开口（should_speak={should_speak:.2f}"
                    f" has_topic={has_topic:.2f} mood_fit={mood_fit:.2f}）"
                )
                return

            logger.info(
                f"[BetterChat] 冷场主动开口（should_speak={should_speak:.2f}"
                f" has_topic={has_topic:.2f} mood_fit={mood_fit:.2f}）"
            )
            await self._speak_proactively(umo, event)
            self.silence.bump_proactive(umo, day_stamp=today)

        except asyncio.CancelledError:
            raise
        except JevError as exc:
            # Jev 不可用是**预期内的**降级场景（服务端故障 / 熔断 / 超时），
            # 不该打完整堆栈 —— 那会把日志刷爆，而且看着像插件崩了。
            self._log_jev_failure(exc)
        except Exception:  # noqa: BLE001 - 冷场检查失败绝不能影响聊天
            logger.warning("[BetterChat] 冷场检查异常", exc_info=True)

    def _proactive_state(self, umo: str, cfg: ProactiveSettings) -> dict:
        """冷场判定用的 state。只放事实，规则在 instructions 里。"""
        history = self.transcripts.recent(umo, cfg.context_messages)
        return build_gate_state(
            group_name="",
            latest="",
            context=history,
            bot_messages=[],
            bot_name=self._bot_name_hint(),
            latest_sender="",
            bot_mentioned=False,
        ) | {
            "silence_seconds": round(self.silence.silence_seconds(umo), 1),
            "seconds_since_bot_spoke": round(self.silence.seconds_since_bot(umo), 1),
        }

    def _bot_name_hint(self) -> str:
        try:
            cfg = self.context.get_config(umo=None)
            name = cfg.get("platform_settings", {}).get("nickname")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _speak_proactively(
        self,
        umo: str,
        event: AstrMessageEvent | None = None,
        *,
        prompt: str | None = None,
    ) -> None:
        """主动开口：注入一个官方 CronMessageEvent，让主 LLM 正常生成。

        为什么不用 ``provider.text_chat`` 直接调模型：那样会绕过整个管线，
        人设、长期记忆、工具、发送钩子全部失效，发出来的话会很不像这个 Bot。
        走官方事件队列则完全等价于"有人在说话"，只不过说话者是定时任务。

        Args:
            umo: 会话标识。
            event: 触发本次发言的事件；冷场/观望场景没有对应事件，传 ``None``。
            prompt: 自定义提示。不传则用「冷场暖场」的默认措辞。
        """
        try:
            from astrbot.core.cron.events import CronMessageEvent
            from astrbot.core.platform.message_session import MessageSession
        except ImportError:
            logger.warning("[BetterChat] 当前 AstrBot 版本没有 CronMessageEvent，冷场功能不可用")
            return

        try:
            session = MessageSession.from_str(umo)
        except Exception:  # noqa: BLE001
            logger.warning(f"[BetterChat] 无法解析会话标识：{umo}")
            return

        # 这段 prompt 会被当成"用户消息"送进主 LLM，所以措辞要小心：
        #
        # * 不要写成"要求：1. 2. 3."这种清单 —— 实测模型会把清单本身
        #   当成"我该怎么做"的说明，然后原样吐进回复里。
        # * 明确说"不要复述这段说明"，堵住自我解释。
        # * 给一个真正的"退出选项"（只回句号），否则模型没什么可说时
        #   会硬憋一句废话。
        if prompt is None:
            prompt = (
                "（群聊已经安静了一段时间，你可以自在地说一句话暖场，"
                "就像群友随口说的那种。直接说你想到的话就好，"
                "不要复述这条说明，也不要解释你在做什么。"
                "如果实在没什么想说的，就只回一个句号。）"
            )
        try:
            synthetic = CronMessageEvent(
                context=self.context,
                session=session,
                message=prompt,
                sender_name="BetterChat",
                message_type=session.message_type,
                extras={"_betterchat_proactive": True},
            )
        except Exception:  # noqa: BLE001 - 构造失败不能崩
            logger.warning("[BetterChat] 构造冷场事件失败", exc_info=True)
            return

        try:
            await self.context.get_event_queue().put(synthetic)
        except Exception:  # noqa: BLE001
            logger.warning("[BetterChat] 冷场事件入队失败", exc_info=True)

    def _remember_group_message(
        self, umo: str, event: AstrMessageEvent, text: str
    ) -> None:
        """把一条群消息记进上下文缓存。

        同一条消息可能被多个 handler 看到，用 ``message_id`` 去重。
        """
        if not text:
            return
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        self.transcripts.record(
            umo,
            ChatLine(
                sender=self._sender_name(event),
                text=text[:500],
                is_self=self._is_self_message(event),
                at_bot=bool(getattr(event, "is_at_or_wake_command", False)),
                seconds_ago=0.0,
                message_id=message_id,
            ),
        )

    def _to_chat_line(self, item: object) -> object:
        """把历史消息（对象或字符串）转成 :class:`ChatLine`。"""

        def pick(*names: str) -> str:
            for name in names:
                try:
                    value = getattr(item, name, None)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""

        if isinstance(item, str):
            return ChatLine(sender="", text=item)
        if isinstance(item, dict):
            return ChatLine(
                sender=str(item.get("sender") or item.get("sender_name") or ""),
                text=str(item.get("text") or item.get("content") or ""),
                is_self=bool(item.get("is_self") or item.get("is_bot")),
                at_bot=bool(item.get("at_bot")),
            )
        return ChatLine(
            sender=pick("sender_name", "sender", "nickname"),
            text=pick("text", "content", "message_str"),
            is_self=bool(getattr(item, "is_self", False)),
            at_bot=bool(getattr(item, "at_bot", False)),
        )

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
        lines.append(f"  模型：{jev.active_model()}")
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
                    **self._gate_state_extras(event, already_woken),
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
        for umo in list(self._silence_tasks):
            self._cancel_silence_task(umo)
        for umo in list(self._help_tasks):
            self._cancel_help_task(umo)
        try:
            await self.group_agg.close()
        except Exception:  # noqa: BLE001
            logger.debug("[BetterChat] 关闭群聊聚合器失败", exc_info=True)
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
