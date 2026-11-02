"""BetterChat 纯逻辑层。

这个模块**不导入 astrbot**，也不做任何网络/IO，只包含可离线单测的决策逻辑：

* :class:`MergeWindow` —— 私聊「等用户说完」的合并窗口计时与归属仲裁。
* :func:`gate_reason` —— 群聊本地闸门（零延迟短路，先于任何 Jev 请求）。
* :func:`decide_group_action` —— 把 Jev 的三个概率换算成三态动作。
* :func:`quiet_seconds_for` —— 静默窗口的自适应计算。

设计取舍：时间全部走可注入的 ``clock``（默认 :func:`time.monotonic`），
等待全部走 ``asyncio.Event`` 而不是轮询，这样窗口逻辑可以在测试里
用假时钟瞬间跑完，不会真的 sleep。
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Sequence

__all__ = [
    "Action",
    "ChatLine",
    "GroupAggregator",
    "GateDecision",
    "GateReason",
    "GroupSettings",
    "GroupVerdict",
    "MergeSettings",
    "MergeWindow",
    "Reservation",
    "ReservationState",
    "build_gate_state",
    "decide_group_action",
    "gate_reason",
    "merge_group_lines",
    "merge_texts",
    "quiet_seconds_for",
    "resolve_fail_policy",
    "visible_length",
]


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #


def visible_length(text: str) -> int:
    """返回文本的「可见长度」：忽略所有空白字符。

    用户打字时的停顿往往伴随空格与换行，用可见长度衡量「话说得多不多」
    比用 ``len()`` 稳定。
    """
    return sum(1 for ch in text if not ch.isspace())


def merge_texts(parts: Sequence[str], *, max_chars: int = 0) -> str:
    """把一轮里的多条消息按到达顺序合并成一段文本。

    Args:
        parts: 已按到达顺序排好的文本片段。
        max_chars: 字数上限；``0`` 表示不限制。超出时从**最旧**的一端开始丢弃，
            保留最新的内容 —— 用户刚说的话比很早之前说的更重要。
    """
    merged = "\n".join(p for p in parts if p)
    if max_chars > 0 and len(merged) > max_chars:
        merged = merged[-max_chars:]
    return merged


def _as_float(value: object, default: float) -> float:
    """把配置值安全地转成 float，非法值退回默认。

    面板配置可能被手改成字符串或 ``NaN``，这里统一兜住，
    避免一个坏配置把整条消息管线拖垮。
    """
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _as_int(value: object, default: int) -> int:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result


# --------------------------------------------------------------------------- #
# 私聊合并
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MergeSettings:
    """私聊合并的可调参数，对应面板里的「私聊合并」分组。"""

    enable: bool = True
    base_quiet_seconds: float = 2.0
    min_quiet_seconds: float = 1.5
    max_quiet_seconds: float = 4.0
    total_seconds: float = 8.0
    length_scale_chars: int = 60
    max_messages: int = 8
    max_chars: int = 4000
    ignore_prefixes: tuple[str, ...] = ("/", "!")

    @classmethod
    def from_config(cls, raw: object) -> MergeSettings:
        """从插件配置里解析，任何缺失/非法字段都退回默认值。"""
        section = raw if isinstance(raw, dict) else {}

        min_quiet = _as_float(section.get("min_quiet_seconds", 1.5), 1.5)
        max_quiet = _as_float(section.get("max_quiet_seconds", 4.0), 4.0)
        # 下限永远不能超过上限，否则 clamp 会失效；这里做一次规整。
        if max_quiet < min_quiet:
            max_quiet = min_quiet

        prefixes = section.get("ignore_prefixes", ["/", "!"])
        if not isinstance(prefixes, (list, tuple)):
            prefixes = ["/", "!"]
        prefixes = tuple(str(p) for p in prefixes if isinstance(p, str) and p)

        return cls(
            enable=bool(section.get("enable", True)),
            base_quiet_seconds=_as_float(section.get("base_quiet_seconds", 2.0), 2.0),
            min_quiet_seconds=min_quiet,
            max_quiet_seconds=max_quiet,
            total_seconds=max(0.1, _as_float(section.get("total_seconds", 8.0), 8.0)),
            length_scale_chars=max(0, _as_int(section.get("length_scale_chars", 60), 60)),
            max_messages=max(0, _as_int(section.get("max_messages", 8), 8)),
            max_chars=max(0, _as_int(section.get("max_chars", 4000), 4000)),
            ignore_prefixes=prefixes,
        )

    def should_ignore(self, text: str) -> bool:
        """指令类消息不参与合并，直接交给 AstrBot 原生处理。"""
        stripped = text.lstrip()
        return any(stripped.startswith(p) for p in self.ignore_prefixes)


def quiet_seconds_for(text: str, settings: MergeSettings) -> float:
    """计算「这段话之后需要安静多久」才算说完。

    思路：话越多，用户越可能还在组织语言，窗口相应延长；但被
    ``min_quiet_seconds`` / ``max_quiet_seconds`` 夹住，避免极端值。

    Args:
        text: 这一轮**累计**的文本（不是单条）。
        settings: 合并参数。
    """
    base = settings.base_quiet_seconds
    if settings.length_scale_chars <= 0:
        return min(settings.max_quiet_seconds, max(settings.min_quiet_seconds, base))
    bonus = visible_length(text) / float(settings.length_scale_chars)
    return min(settings.max_quiet_seconds, max(settings.min_quiet_seconds, base + bonus))


class ReservationState(str, Enum):
    """一次「登记」最终得到的结局。"""

    OWNER = "owner"
    """我仍是这一轮的主人 —— 合并文本后放行原生 LLM。"""

    SUPERSEDED = "superseded"
    """更新的消息接管了本轮 —— 我应当静默退出，什么都不做。"""

    BYPASS = "bypass"
    """策略不适用（关闭 / 指令 / 非纯文本 / 插件已卸载）—— 立刻放行原生流程。"""


@dataclass(frozen=True)
class Reservation:
    """一次消息到达的登记记录。``ticket`` 单调递增，越大越新。"""

    ticket: int
    received_at: float
    text: str


@dataclass
class _PendingTurn:
    """一个会话当前正在攒的那一轮。"""

    created_at: float
    owner_ticket: int = 0
    latest_at: float = 0.0
    parts: list[str] = field(default_factory=list)
    count: int = 0
    version: int = 0
    """每次有更新消息进来 +1；等待者靠它判断是否被超越。"""
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class MergeWindow:
    """私聊合并窗口。

    使用方式是「登记 → 等待 → 结算」：

    1. :meth:`reserve` **同步**登记当前消息（内部不含 ``await``，因此相对于
       事件循环是原子的，登记顺序等于到达顺序）。
    2. :meth:`wait_for_quiet` 等待静默窗口或整轮硬上限到期；期间若有更新的
       消息进来会刷新窗口，并让本等待者重新判断归属。
    3. :meth:`settle` 原子地判断「我是否仍是主人」，是则取走合并文本。

    调用方拿到 :attr:`ReservationState.OWNER` 后，才把合并文本写回事件并放行。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        settings: MergeSettings | None = None,
    ) -> None:
        self._clock = clock
        self.settings = settings or MergeSettings()
        self._turns: dict[str, _PendingTurn] = {}
        self._settled: dict[str, int] = {}
        """每个会话最近一次被结算的 ticket，用于让「慢半拍」的等待者
        正确判定为 SUPERSEDED 而不是误判成新会话。"""
        self._next_ticket = 0
        self._lock = asyncio.Lock()
        self._closed = False

    # -- 生命周期 --------------------------------------------------------- #

    def configure(self, settings: MergeSettings) -> None:
        """热更新参数（面板保存后调用）。不影响已经在等待的轮次。"""
        self.settings = settings

    async def close(self) -> None:
        """插件卸载/热重载时调用：唤醒所有等待者，避免孤儿任务。"""
        self._closed = True
        for turn in list(self._turns.values()):
            turn.wake.set()
        self._turns.clear()

    def _prune(self) -> None:
        """清掉明显过期的记录，防止 dict 无限增长。"""
        if not self._turns:
            return
        now = self._clock()
        horizon = max(self.settings.total_seconds * 4, 60.0)
        for key in [k for k, v in self._turns.items() if now - v.latest_at > horizon]:
            self._turns.pop(key, None)
            self._settled.pop(key, None)

    # -- 登记 ------------------------------------------------------------- #

    def reserve(self, session_key: str, text: str) -> Reservation:
        """同步登记一条消息，返回它在本轮中的位置。

        关键：**这个函数内部不能有 ``await``**。AstrBot 的事件循环里
        多个事件会并发进入，只有同步登记才能保证 ticket 顺序等于到达顺序。
        """
        self._prune()
        self._next_ticket += 1
        now = self._clock()
        reservation = Reservation(ticket=self._next_ticket, received_at=now, text=text)

        turn = self._turns.get(session_key)
        if turn is None:
            turn = _PendingTurn(created_at=now)
            self._turns[session_key] = turn

        turn.owner_ticket = reservation.ticket
        turn.latest_at = now
        turn.parts.append(text)
        turn.count += 1
        turn.version += 1
        # 唤醒可能在等的旧等待者，让它们重新判断归属。
        turn.wake.set()
        turn.wake = asyncio.Event()
        return reservation

    # -- 窗口计算 --------------------------------------------------------- #

    def _deadline(self, turn: _PendingTurn) -> float:
        """本轮应当结算的时刻 = min(静默到期, 整轮硬上限)。"""
        quiet = quiet_seconds_for("\n".join(turn.parts), self.settings)
        return min(turn.latest_at + quiet, turn.created_at + self.settings.total_seconds)

    def _overflowed(self, turn: _PendingTurn) -> bool:
        """本轮是否已经攒爆（条数或字数超限）。超过就立刻结算，不再等。"""
        if self.settings.max_messages > 0 and turn.count > self.settings.max_messages:
            return True
        if self.settings.max_chars > 0:
            if len("\n".join(turn.parts)) > self.settings.max_chars:
                return True
        return False

    # -- 等待 ------------------------------------------------------------- #

    async def wait_for_quiet(
        self,
        session_key: str,
        reservation: Reservation,
        *,
        is_current: Callable[[], bool] | None = None,
    ) -> ReservationState:
        """等到可以结算为止。

        Args:
            session_key: 会话标识。
            reservation: :meth:`reserve` 的返回值。
            is_current: 可选外部校验（例如「事件没被别的插件停掉」）。
                返回 False 即视为失效。

        Returns:
            :attr:`ReservationState.OWNER` /
            :attr:`ReservationState.SUPERSEDED` /
            :attr:`ReservationState.BYPASS`。
        """
        while True:
            if self._closed:
                return ReservationState.BYPASS

            if is_current is not None and not is_current():
                return ReservationState.SUPERSEDED

            turn = self._turns.get(session_key)
            if turn is None:
                # 本轮已经被别人结算掉了：我不是主人，静默退出。
                if self._settled.get(session_key, 0) >= reservation.ticket:
                    return ReservationState.SUPERSEDED
                # 从未有过本轮（被 prune 掉）—— 交给原生流程。
                return ReservationState.BYPASS

            # 被更新的消息超越：本轮已不属于我。
            if turn.owner_ticket != reservation.ticket:
                return ReservationState.SUPERSEDED

            if self._overflowed(turn):
                return ReservationState.OWNER

            timeout = self._deadline(turn) - self._clock()
            if timeout <= 0:
                return ReservationState.OWNER

            version = turn.version
            wake = turn.wake
            # 必须在复查 version 之前 clear，否则会丢掉「clear 与 wait 之间」
            # 到达的唤醒信号（经典 lost-wakeup）。
            wake.clear()
            if turn.version != version:
                continue

            try:
                await asyncio.wait_for(wake.wait(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                # 超时 == 真实时间已经走完了我们请求的全部剩余时间，也就是
                # 到了 deadline。此时仍然要重新确认归属：可能刚好有更新的
                # 消息在超时前一刻登记，那样本轮就不该由我收口。
                latest = self._turns.get(session_key)
                if latest is None:
                    if self._settled.get(session_key, 0) >= reservation.ticket:
                        return ReservationState.SUPERSEDED
                    return ReservationState.BYPASS
                if latest.owner_ticket != reservation.ticket:
                    return ReservationState.SUPERSEDED
                return ReservationState.OWNER
            # 被新消息唤醒：回到循环顶部重算窗口与归属。

    # -- 结算 ------------------------------------------------------------- #

    async def settle(
        self, session_key: str, reservation: Reservation
    ) -> tuple[str, int] | None:
        """原子地关闭本轮并取走合并文本。

        Returns:
            ``(合并文本, 条数)``；若本 reservation 已不是主人则返回 ``None``。
        """
        async with self._lock:
            turn = self._turns.get(session_key)
            if turn is None or turn.owner_ticket != reservation.ticket:
                return None
            self._turns.pop(session_key, None)
            self._settled[session_key] = reservation.ticket
            merged = merge_texts(turn.parts, max_chars=self.settings.max_chars)
            return merged, turn.count


class GroupAggregator:
    """群聊「判定前」的短暂聚合窗口。

    对比过几种做法后选定的形态：

    * 现有三家同类插件（reply_gate / jev_router / czyczy23）**都是每条群消息
      判一次**，没有一家在判定前聚合。所以我们这里比它们更省调用。
    * czyczy23 虽然有「等待窗口」，但那个窗口是在**决定回复之后**才开的，
      用于攒更多消息一次性回复，判定本身早就做完了；而且是 100ms 轮询，
      既费 CPU 精度又差。我们不复用那种做法。

    我们的窗口开在**判定之前**：群里连刷三句只花一次 Jev 调用，而且
    ``open_question`` 看的是合并后的完整语义，不是被打断的半句。

    复用 :class:`MergeWindow` 已经验证过的核心机制（同步 ``reserve`` 保证
    原子性、ticket 仲裁、``asyncio.Event`` 等待而非轮询），区别是攒的是
    :class:`ChatLine` 而不是纯字符串 —— 发送者、是否 Bot 自己、是否 @ Bot
    这些信息在群聊判定里都要保留。
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._turns: dict[str, _GroupTurn] = {}
        self._settled: dict[str, int] = {}
        self._next_ticket = 0
        self._lock = asyncio.Lock()
        self._closed = False

    async def close(self) -> None:
        """唤醒所有等待者，避免热重载时留下孤儿任务。"""
        self._closed = True
        for turn in list(self._turns.values()):
            turn.wake.set()
        self._turns.clear()

    def reserve(
        self, group_key: str, line: ChatLine, *, window_seconds: float
    ) -> Reservation:
        """同步登记一条群消息，返回它在本轮中的位置。

        值 **不能** 返回"是否并入了已有窗口"：并发到达时，先登记的那条
        也可能被后来的顶掉（``SUPERSEDED``），而后来者又都"并入"了自己
        提前返回 —— 结果没有人负责结算，判定直接丢失。所以统一让所有
        到达者都去 :meth:`wait`，由最终仍是 owner 的那条收口。

        关键：**这个函数内部不能有 ``await``**。多事件并发进入时，只有同步
        登记才能保证 ticket 顺序等于到达顺序。
        """
        self._prune()
        self._next_ticket += 1
        now = self._clock()
        reservation = Reservation(
            ticket=self._next_ticket, received_at=now, text=line.text
        )

        turn = self._turns.get(group_key)
        if turn is None:
            turn = _GroupTurn(created_at=now)
            self._turns[group_key] = turn

        turn.owner_ticket = reservation.ticket
        turn.latest_at = now
        turn.window_seconds = max(0.0, window_seconds)
        turn.lines.append(line)
        turn.version += 1
        turn.wake.set()
        turn.wake = asyncio.Event()
        return reservation

    def _prune(self) -> None:
        if not self._turns:
            return
        now = self._clock()
        horizon = 120.0
        for key in [k for k, v in self._turns.items() if now - v.latest_at > horizon]:
            self._turns.pop(key, None)
            self._settled.pop(key, None)

    async def wait(
        self, group_key: str, reservation: Reservation
    ) -> ReservationState:
        """等到窗口关闭。

        只有**最新的那条**消息有资格收口；更早的已经被并进来，
        应当直接退出（它们的文本已经在合并结果里）。
        """
        while True:
            if self._closed:
                return ReservationState.BYPASS
            turn = self._turns.get(group_key)
            if turn is None:
                if self._settled.get(group_key, 0) >= reservation.ticket:
                    return ReservationState.SUPERSEDED
                return ReservationState.BYPASS
            # 窗口被更新的消息接管：我不是最新的，让最新的那条负责。
            if turn.owner_ticket != reservation.ticket:
                return ReservationState.SUPERSEDED
            timeout = turn.latest_at + turn.window_seconds - self._clock()
            if timeout <= 0:
                return ReservationState.OWNER
            version, wake = turn.version, turn.wake
            wake.clear()
            if turn.version != version:
                continue
            try:
                await asyncio.wait_for(wake.wait(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                # 窗口到期。再确认一次归属，然后收口。
                latest = self._turns.get(group_key)
                if latest is None:
                    if self._settled.get(group_key, 0) >= reservation.ticket:
                        return ReservationState.SUPERSEDED
                    return ReservationState.BYPASS
                if latest.owner_ticket != reservation.ticket:
                    return ReservationState.SUPERSEDED
                return ReservationState.OWNER

    async def settle(
        self, group_key: str, reservation: Reservation
    ) -> list[ChatLine] | None:
        """关闭窗口并取走这一批消息（按到达顺序）。"""
        async with self._lock:
            turn = self._turns.get(group_key)
            if turn is None or turn.owner_ticket != reservation.ticket:
                return None
            self._turns.pop(group_key, None)
            self._settled[group_key] = reservation.ticket
            return list(turn.lines)


@dataclass
class _GroupTurn:
    """一群正在聚合中的消息。"""

    created_at: float
    owner_ticket: int = 0
    latest_at: float = 0.0
    window_seconds: float = 0.0
    lines: list[ChatLine] = field(default_factory=list)
    version: int = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event)


def merge_group_lines(lines: Sequence[ChatLine]) -> ChatLine:
    """把一批聚合到的消息合成一条"最新消息"。

    规则刻意保守：

    * 只有**同一个人**连发的才算同一句话，直接换行拼接。
    * 混了不同的人，就不硬拼成一条 —— 那样会让 Jev 以为是一句话，
      反而降低判断质量。此时保留最后一条作为主体，其余作为上下文。
    """
    if not lines:
        return ChatLine(sender="", text="")
    if len(lines) == 1:
        return lines[0]
    senders = {line.sender for line in lines}
    last = lines[-1]
    if len(senders) == 1:
        merged_text = "\n".join(line.text for line in lines if line.text)
        return ChatLine(
            sender=last.sender,
            text=merged_text,
            is_self=any(line.is_self for line in lines),
            at_bot=any(line.at_bot for line in lines),
            seconds_ago=last.seconds_ago,
        )
    return last


# --------------------------------------------------------------------------- #
# 群聊闸门
# --------------------------------------------------------------------------- #


class GateReason(str, Enum):
    """本地闸门的判定原因。"""

    DISABLED = "disabled"
    COMMAND = "command"
    SELF = "self"
    TOO_SHORT = "too_short"
    RATE_LIMITED = "rate_limited"
    BREAKER_OPEN = "breaker_open"
    PASS = "pass"


@dataclass(frozen=True)
class GroupSettings:
    """群聊闸门参数，对应面板里的「群聊 Jev 闸门」分组。"""

    enable: bool = True
    judge_all_messages: bool = True
    min_message_length: int = 6
    max_replies_per_hour: int = 8
    min_interval_seconds: int = 120
    bot_cooldown_seconds: int = 90
    context_messages: int = 10
    help_unaddressed: bool = False
    """没被叫到时，是否主动帮「没人接的问题」。

    默认关闭。这条规则最容易显得 Bot 在多管闲事 —— 群里两个人正常对话时
    「有人提问、还没人回答」是极常见的状态，放行就等于见缝插针。
    """
    aggregate_window_seconds: float = 1.5
    """判定前的静默聚合窗口（秒）。0 表示每条消息各判一次。"""
    fail_policy: str = "closed"

    @classmethod
    def from_config(cls, raw: object) -> GroupSettings:
        section = raw if isinstance(raw, dict) else {}
        policy = str(section.get("fail_policy", "closed")).strip().lower()
        if policy not in ("closed", "open"):
            policy = "closed"
        return cls(
            enable=bool(section.get("enable", True)),
            judge_all_messages=bool(section.get("judge_all_messages", True)),
            min_message_length=max(0, _as_int(section.get("min_message_length", 6), 6)),
            max_replies_per_hour=max(
                0, _as_int(section.get("max_replies_per_hour", 8), 8)
            ),
            min_interval_seconds=max(
                0, _as_int(section.get("min_interval_seconds", 120), 120)
            ),
            bot_cooldown_seconds=max(
                0, _as_int(section.get("bot_cooldown_seconds", 90), 90)
            ),
            context_messages=max(0, _as_int(section.get("context_messages", 10), 10)),
            help_unaddressed=bool(section.get("help_unaddressed", False)),
            aggregate_window_seconds=max(
                0.0, min(10.0, _as_float(section.get("aggregate_window_seconds", 1.5), 1.5))
            ),
            fail_policy=policy,
        )


@dataclass(frozen=True)
class GateDecision:
    """闸门结论。"""

    reason: GateReason
    should_judge: bool
    """True 表示需要通过 Jev 判定才能决定是否回复。"""

    already_woken: bool
    """消息本身已经是「被 @ / 引用 / 唤醒词」状态。"""


def gate_reason(
    *,
    settings: GroupSettings,
    text: str,
    is_self: bool,
    is_command: bool,
    already_woken: bool,
    recently_replied: int = 0,
    seconds_since_last_reply: float = float("inf"),
    seconds_since_bot_spoke: float = float("inf"),
    breaker_open: bool = False,
) -> GateDecision:
    """群聊本地闸门 —— 零延迟、不发请求，按「最省」的顺序短路。

    顺序刻意这样排：先剔除永远不该判定的（关闭 / 自己 / 指令），
    再剔除「不值得花额度」的（太短），最后才是各类限频。
    """
    if not settings.enable:
        return GateDecision(GateReason.DISABLED, False, already_woken)
    if is_self:
        return GateDecision(GateReason.SELF, False, already_woken)
    if is_command:
        return GateDecision(GateReason.COMMAND, False, already_woken)

    # 没被唤醒、又不允许主动插话 —— 直接不判定。
    if not already_woken and not settings.judge_all_messages:
        return GateDecision(GateReason.DISABLED, False, already_woken)

    # 被 @ 了但太短（例如只有「在吗」）—— 不判定，但放行原生流程交给模型。
    if visible_length(text) < settings.min_message_length:
        return GateDecision(GateReason.TOO_SHORT, False, already_woken)

    # 已被唤醒的消息必须判定（用于过滤「叫了但没必要回」），因此限频
    # 只作用于「未被唤醒」的主动插话。
    if not already_woken:
        if breaker_open:
            return GateDecision(GateReason.BREAKER_OPEN, False, already_woken)
        if (
            settings.max_replies_per_hour > 0
            and recently_replied >= settings.max_replies_per_hour
        ):
            return GateDecision(GateReason.RATE_LIMITED, False, already_woken)
        if (
            settings.min_interval_seconds > 0
            and seconds_since_last_reply < settings.min_interval_seconds
        ):
            return GateDecision(GateReason.RATE_LIMITED, False, already_woken)
        if (
            settings.bot_cooldown_seconds > 0
            and seconds_since_bot_spoke < settings.bot_cooldown_seconds
        ):
            return GateDecision(GateReason.RATE_LIMITED, False, already_woken)

    return GateDecision(GateReason.PASS, True, already_woken)


# --------------------------------------------------------------------------- #
# 群聊决策
# --------------------------------------------------------------------------- #


class Action(str, Enum):
    """Jev 判定之后的最终动作。"""

    RESPOND = "respond"
    IGNORE = "ignore"
    UNKNOWN = "unknown"
    """Jev 不可用 / 返回不合法 —— 交给失败策略决定。"""


@dataclass(frozen=True)
class GroupVerdict:
    """一次群聊判定的结果，含诊断信息（供 /betterchat gate 展示）。"""

    action: Action
    reason: str
    addressed: float | None = None
    open_question: float | None = None
    already_answered: float | None = None


def decide_group_action(
    *,
    addressed: float | None,
    open_question: float | None,
    already_answered: float | None,
    already_woken: bool,
    addressed_to_other: bool = False,
    help_unaddressed: bool = False,
    reply_threshold: float = 0.7,
    already_answered_threshold: float = 0.7,
) -> GroupVerdict:
    """把三个概率换算成动作。

    策略写在代码里而不是提示词里，这样调阈值不需要重新请求 Jev。

    规则（按顺序短路）：

    1. 任一概率缺失/非法 → ``UNKNOWN``（走失败策略）。
    2. Bot 刚答过这个话题 → ``IGNORE``，避免重复回复。
    3. 消息已经 @ 了 Bot → ``RESPOND``（被叫到就该出声）。
    4. 消息没有 @ Bot：
       * 明显在问 Bot 且有未回答的问题 → ``RESPOND``（主动帮忙）
       * 其余一律 ``IGNORE``，不确定就闭嘴。
    """
    values = (addressed, open_question, already_answered)
    if any(v is None or not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in values):
        return GroupVerdict(Action.UNKNOWN, "invalid_probability")

    assert addressed is not None and open_question is not None
    assert already_answered is not None

    if already_answered >= already_answered_threshold:
        return GroupVerdict(
            Action.IGNORE,
            "already_answered",
            addressed,
            open_question,
            already_answered,
        )

    if already_woken:
        # 用户明确叫了 Bot，只要不是在重复回答就给回应。
        return GroupVerdict(
            Action.RESPOND, "woken", addressed, open_question, already_answered
        )

    if addressed >= reply_threshold and open_question >= 0.3:
        return GroupVerdict(
            Action.RESPOND,
            "addressed_with_question",
            addressed,
            open_question,
            already_answered,
        )

    if help_unaddressed and open_question >= 0.85 and addressed <= 0.3:
        # 群里有人提了个没人接的问题 —— 主动帮一把。
        #
        # 但**必须排除"在问某个具体的人"的情况**。群里 A 问 B「你这是哪个
        # 大学的」时，open_question 同样很高，可那是两人之间的对话，
        # Bot 插进去就是抢答。这一条是实测踩出来的：state 里没标"@了别人"，
        # Jev 只看到"有人提问"，于是把一对一对话也当成求助。
        if not addressed_to_other:
            return GroupVerdict(
                Action.RESPOND,
                "unanswered_question",
                addressed,
                open_question,
                already_answered,
            )

    return GroupVerdict(
        Action.IGNORE, "not_addressed", addressed, open_question, already_answered
    )


def resolve_fail_policy(policy: str, *, already_woken: bool) -> Action:
    """Jev 不可用时怎么办。

    * 已经被唤醒的消息：永远放行 —— 用户明确叫了 Bot，
      不能因为判定服务挂了就不理人。
    * 未被唤醒（主动插话）：按配置，默认保守地不回复。
    """
    if already_woken:
        return Action.RESPOND
    return Action.RESPOND if str(policy).lower() == "open" else Action.IGNORE


@dataclass(frozen=True)
class ChatLine:
    """群里的一条消息（结构化）。

    只存"可见的事实"：谁说的、说了什么、是不是 Bot 自己、是不是在叫 Bot、
    多久以前。**不存**内部 ID、权限、原始对象 —— Jev 不需要，也不该看到。

    为什么要有时间戳：``reply_gate`` 的做法值得学。判断"该不该接话"时，
    "这条是 3 秒前还是 4 分钟前的"直接影响结论 —— 一个 5 分钟前没人接的
    问题可以主动帮，但刚刚别人正在聊的话题就不该硬插。
    """

    sender: str
    text: str
    is_self: bool = False
    at_bot: bool = False
    seconds_ago: float | None = None
    message_id: str = ""
    """平台消息 ID，仅用于去重（事件可能被多个 handler 看到）。"""


def _chat_line_payload(line: ChatLine) -> dict[str, object]:
    """把一条消息摊成 Jev 能直接读的对象。

    ``seconds_ago`` 为 ``None`` 时**不输出这个键** —— 与其编一个 0 让模型
    误以为"刚刚发生"，不如干脆不说。
    """
    payload: dict[str, object] = {
        "sender": line.sender,
        "text": line.text,
        "is_self": bool(line.is_self),
        "at_bot": bool(line.at_bot),
    }
    if line.seconds_ago is not None:
        payload["seconds_ago"] = round(max(0.0, float(line.seconds_ago)), 1)
    return payload


def build_gate_state(
    *,
    group_name: str,
    latest: str,
    context: "Sequence[ChatLine] | Sequence[str]",
    bot_messages: Iterable[str],
    bot_name: str = "",
    latest_sender: str = "",
    bot_mentioned: bool = False,
    addressed_to_other: bool = False,
    latest_seconds_ago: float | None = None,
    max_chars: int = 6000,
) -> dict[str, object]:
    """构造送进 Jev 的 ``state``。

    官方文档对 state 的要求很清楚：**只放与当前问题相关的内容**，
    不要塞无关信息（会造成 distractions 和 context rot）；判断规则应当
    写进 ``instructions`` / ``criteria``，而不是塞进 state。

    按这条要求，这里只放"事实"，不放"命令"：

    * ``bot`` —— Bot 的显示名，以及在最新消息里是否被点名。**这是必需的**：
      没有它，Jev 根本不知道"对着 bot 说"指的是谁，``addressed_to_bot``
      只能靠猜。
    * ``latest_*`` —— 触发本次判定的那条消息，含发送者与"多久以前"。
    * ``recent_messages`` —— 结构化的最近消息（谁说的、是不是 Bot、
      是不是在叫 Bot、多久以前）。参考 ``reply_gate`` 的做法：只发可见转录，
      但每条带足够字段，比一堆裸字符串信息量大得多。
    * ``recent_bot_messages`` —— 判断"是不是刚答过"所必需。

    Jev 只吃文本、对英文最稳，但聊天原文保持原样（不改写成英文），
    避免语义在翻译中丢失。这里只负责裁剪与结构化。
    """
    raw_context = list(context or [])
    # 兼容旧调用：传入纯字符串也接受，包成 ChatLine。
    lines: list[ChatLine] = [
        item if isinstance(item, ChatLine) else ChatLine(sender="", text=str(item))
        for item in raw_context
        if (item.text if isinstance(item, ChatLine) else str(item))
    ][-20:]
    bots = [b for b in bot_messages if b][-5:]

    def clip(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[-limit:]

    latest_payload: dict[str, object] = {
        "sender": clip(latest_sender, 80),
        "text": clip(latest, 2000),
    }
    if latest_seconds_ago is not None:
        latest_payload["seconds_ago"] = round(max(0.0, float(latest_seconds_ago)), 1)

    state: dict[str, object] = {
        "group": clip(group_name, 120),
        "bot": {
            "name": clip(bot_name, 80) if bot_name else "",
            "mentioned_in_latest": bool(bot_mentioned),
            # 「这条消息 @ 了别人」是平台层面已经确定的事实，必须显式给出。
            # 少了它，Jev 只会看到「有人在提问」而不知道那是在问别人 ——
            # 结果就是 Bot 抢答两人之间的对话。
            "addressed_to_other": bool(addressed_to_other),
        },
        "latest_message": latest_payload,
        "recent_messages": [
            _chat_line_payload(
                ChatLine(
                    sender=clip(line.sender, 80),
                    text=clip(line.text, 500),
                    is_self=line.is_self,
                    at_bot=line.at_bot,
                    seconds_ago=line.seconds_ago,
                )
            )
            for line in lines
        ],
        "recent_bot_messages": [clip(b, 500) for b in bots],
    }

    # 整体再兜一次总长度：state 超过模型上下文会直接 400。
    def size(value: object) -> int:
        if isinstance(value, list):
            return sum(
                sum(len(str(v)) for v in item.values())
                if isinstance(item, dict)
                else len(str(item))
                for item in value
            )
        if isinstance(value, dict):
            return sum(len(str(v)) for v in value.values())
        return len(str(value))

    if sum(size(v) for v in state.values()) > max_chars:
        kept = lines[-5:]
        state["recent_messages"] = [
            _chat_line_payload(
                ChatLine(
                    sender=clip(line.sender, 40),
                    text=clip(line.text, 200),
                    is_self=line.is_self,
                    at_bot=line.at_bot,
                    seconds_ago=line.seconds_ago,
                )
            )
            for line in kept
        ]
        state["recent_bot_messages"] = [clip(b, 200) for b in bots[-2:]]
        latest_payload["text"] = clip(latest, 1500)
    return state
