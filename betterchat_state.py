"""BetterChat 的运行期状态。

职责很窄，但要可靠：

* 每会话（UMO）的运行态：谁在等 LLM、谁是最新、最近一次 bot 发言时间。
* 群聊限频计数（每小时回复数、最近回复时间），持久化到
  ``data/plugin_data/astrbot_betterchat/state.json``。
* 插件卸载时的**彻底**清理。

为什么清理要这么认真：AstrBot 在管理面板保存插件配置后会调用
``plugin_manager.reload()``，也就是先 ``terminate()`` 再重建实例。
如果这里留下未取消的 task 或没关的连接，用户每改一次配置就会漏一点，
改几次之后行为就开始错乱。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["RuntimeState", "RateLimiter", "TranscriptCache"]


def _wall_clock() -> float:
    """墙上时钟，用于**需要持久化**的时间戳。

    限频计数要跨重启保留，所以必须用 ``time.time()``（单调时钟的起点
    每次进程启动都不同，落盘后就再也对不上了）。
    """
    return time.time()


def _mono_clock() -> float:
    """单调时钟，用于进程内的间隔计算。

    不受系统时间调整影响，适合「多久没说话」这类判断。
    """
    return time.monotonic()


# --------------------------------------------------------------------------- #
# 运行期状态
# --------------------------------------------------------------------------- #


@dataclass
class SessionRun:
    """单个会话的运行态。"""

    latest_ticket: int = 0
    last_bot_message_at: float = 0.0
    last_reply_at: float = 0.0


def _latest_activity(run: SessionRun) -> float:
    """取一个会话最近一次活动的时刻。

    两个时间戳都可能是 ``0.0``（从未发生过），此时**不能**把它当成
    「很久以前」直接剪掉 —— 刚创建的会话就是这样。返回 0 表示"未知"，
    调用方（``prune``）应当据此保留会话。
    """
    stamps = [ts for ts in (run.last_reply_at, run.last_bot_message_at) if ts > 0]
    return max(stamps) if stamps else 0.0


@dataclass
class RuntimeState:
    """插件全局运行态。

    这里只存"非持久、进程内"的东西；需要跨重载保留的（限频计数）在
    :class:`RateLimiter` 里。
    """

    sessions: dict[str, SessionRun] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)
    in_flight: dict[str, Any] = field(default_factory=dict)
    """每个会话当前正在请求 LLM 的事件对象（用于打断）。"""
    closed: bool = False

    def session(self, umo: str) -> SessionRun:
        run = self.sessions.get(umo)
        if run is None:
            run = SessionRun()
            self.sessions[umo] = run
        return run

    def note_bot_message(self, umo: str, *, now: float | None = None) -> None:
        self.session(umo).last_bot_message_at = now or _mono_clock()

    def note_reply(self, umo: str, *, now: float | None = None) -> None:
        moment = now or _mono_clock()
        run = self.session(umo)
        run.last_reply_at = moment
        run.last_bot_message_at = moment

    def seconds_since_last_reply(self, umo: str, *, now: float | None = None) -> float:
        last = self.session(umo).last_reply_at
        if last <= 0:
            return math.inf
        return (now or _mono_clock()) - last

    def seconds_since_bot_spoke(self, umo: str, *, now: float | None = None) -> float:
        last = self.session(umo).last_bot_message_at
        if last <= 0:
            return math.inf
        return (now or _mono_clock()) - last

    # -- 后台任务 --------------------------------------------------------- #

    def spawn(self, coro) -> asyncio.Task | None:  # noqa: ANN001
        """启动一个受管后台任务。

        返回的 task 会被登记，:meth:`close` 时统一取消。插件已关闭时
        直接丢弃，避免在卸载过程中又拉起新任务。
        """
        if self.closed:
            # 调用方已经把协程造出来了，必须消费掉，否则会有
            # "coroutine was never awaited" 警告。
            coro.close()
            return None
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def cancel_task(self, task: asyncio.Task | None) -> None:
        """取消并等待一个任务结束，吞掉所有异常。"""
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - 清理阶段不关心具体失败
            pass

    async def close(self) -> None:
        """取消全部后台任务，标记关闭。可重复调用。"""
        self.closed = True
        pending = [t for t in self.tasks if not t.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 清理阶段不关心具体失败
                pass
        self.tasks.clear()
        self.sessions.clear()
        self.in_flight.clear()

    # -- 剪枝 ------------------------------------------------------------- #

    def prune(self, *, now: float | None = None, horizon: float = 3600.0) -> None:
        """清掉长时间没有活动的会话，避免 dict 无限增长。"""
        if not self.sessions:
            return
        moment = now or _mono_clock()
        stale = []
        for key, run in self.sessions.items():
            last = _latest_activity(run)
            # 从未活动过的会话（last == 0）不在此处剪枝 —— 它可能刚刚
            # 被创建，剪掉会让下一个请求重新创建一个空记录，得不偿失。
            if last > 0 and moment - last > horizon:
                stale.append(key)
        for key in stale:
            self.sessions.pop(key, None)


class TranscriptCache:
    """每个会话的最近消息缓存。

    为什么必须自己存：AstrBot 的 ``AstrBotMessage`` 只有**当前这一条**消息
    （``message`` / ``message_str`` / ``sender`` / ``timestamp``），并没有
    "最近 N 条群消息"这种东西。插件要上下文只能自己记。

    横向对比，``astrbot_plugin_reply_gate``、``czyczy23/astrbot_plugin_jev``
    也都是这么做的（前者用 ``deque(maxlen=12)``，后者用
    ``pending_messages_cache``）。唯一不同的是 ``jev_router``，它去读
    AstrBot 的**对话历史**（conversations 表）—— 但那是"和 LLM 的对话记录"，
    不是"群里的聊天流"，对判断"要不要插话"帮助有限。

    刻意选了内存缓存而不是读数据库：
    * 零 IO，判定路径上没有额外延迟
    * 不依赖 AstrBot 内部表结构（升级不易碎）
    * 群聊上下文本来就只需要"最近几条"
    """

    def __init__(self, *, maxlen: int = 20) -> None:
        self.maxlen = max(2, int(maxlen))
        self._cache: dict[str, deque[Any]] = {}

    def record(self, key: str, line: Any) -> None:
        """记一条消息。

        同一个 ``message_id`` 不重复记 —— 群消息可能被多个 handler 看到
        （AstrBot 的 handler 是链式执行的），不去重的话上下文里会出现重复条目，
        反而干扰判定。
        """
        if line is None:
            return
        bucket = self._cache.get(key)
        if bucket is None:
            bucket = deque(maxlen=self.maxlen)
            self._cache[key] = bucket
        message_id = getattr(line, "message_id", "")
        if message_id and bucket:
            if getattr(bucket[-1], "message_id", "") == message_id:
                return
        bucket.append(line)

    def recent(self, key: str, limit: int = 0) -> list[Any]:
        """取最近 ``limit`` 条（``0`` 或负数表示不限，但不超过 maxlen）。"""
        bucket = self._cache.get(key)
        if not bucket:
            return []
        items = list(bucket)
        if limit and limit > 0:
            return items[-limit:]
        return items

    def clear(self, key: str | None = None) -> None:
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    def prune(self, *, keep: int = 50) -> None:
        """会话太多时丢掉最久没更新的，防止内存无限增长。"""
        if len(self._cache) <= keep:
            return
        for key in list(self._cache)[: len(self._cache) - keep]:
            self._cache.pop(key, None)


# --------------------------------------------------------------------------- #
# 限频（持久化）
# --------------------------------------------------------------------------- #


@dataclass
class _Bucket:
    """一个会话的回复历史。只用「最近一小时」的计数。"""

    replies: list[float] = field(default_factory=list)

    def count_within(self, now: float, window: float) -> int:
        cutoff = now - window
        return sum(1 for ts in self.replies if ts >= cutoff)


class RateLimiter:
    """按小时滑窗的回复计数，落盘保存。

    用**滑动窗口**而不是"整点清零"，因为整点清零会让 59 分和 01 分
    各刷一次，实际等于双倍额度。

    持久化的意义：AstrBot 重启后限频不会被清零，否则"重启 = 刷新额度"
    会变成绕过限制的捷径。
    """

    WINDOW_SECONDS = 3600.0

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    # -- 读写 ------------------------------------------------------------- #

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 状态文件坏掉只影响限频精度，绝不能让插件起不来。
            return
        if not isinstance(raw, dict):
            return
        for key, values in raw.items():
            if not isinstance(values, list):
                continue
            stamps = []
            for value in values:
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)) and math.isfinite(value):
                    stamps.append(float(value))
            self._buckets[str(key)] = _Bucket(replies=sorted(stamps))

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {key: bucket.replies for key, bucket in self._buckets.items()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            # 磁盘写不进去不影响运行，最多重启后额度重置。
            pass

    # -- 查询 ------------------------------------------------------------- #

    def count(self, key: str, *, now: float | None = None) -> int:
        """最近一小时内的回复次数。"""
        self._ensure_loaded()
        bucket = self._buckets.get(key)
        if bucket is None:
            return 0
        return bucket.count_within(now or _wall_clock(), self.WINDOW_SECONDS)

    def last_reply_at(self, key: str) -> float:
        self._ensure_loaded()
        bucket = self._buckets.get(key)
        if not bucket or not bucket.replies:
            return 0.0
        return bucket.replies[-1]

    # -- 修改 ------------------------------------------------------------- #

    async def record(self, key: str, *, now: float | None = None) -> None:
        """记一次回复，并把过期的记录裁掉。"""
        moment = now or _wall_clock()
        async with self._lock:
            self._ensure_loaded()
            bucket = self._buckets.setdefault(key, _Bucket())
            bucket.replies = [
                ts for ts in bucket.replies if ts >= moment - self.WINDOW_SECONDS
            ]
            bucket.replies.append(moment)
            self._prune_buckets(moment)
            self._save()

    def _prune_buckets(self, now: float) -> None:
        """丢掉已经彻底过期的会话，防止状态文件无限增长。"""
        cutoff = now - self.WINDOW_SECONDS
        for key in [
            k for k, b in self._buckets.items() if not b.replies or b.replies[-1] < cutoff
        ]:
            self._buckets.pop(key, None)

    async def reset(self, key: str | None = None) -> None:
        """清空某个会话（或全部）的计数，供 ``/betterchat reset`` 使用。"""
        async with self._lock:
            self._ensure_loaded()
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)
            self._save()
