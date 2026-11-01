"""限频与运行态的测试。

重点覆盖一个容易漏掉的坑：限频计数**要跨重启保留**，所以必须用墙上
时钟；而运行态里的间隔计算要用单调时钟。两者混用的话，落盘的时间戳
在下次启动后完全对不上，限频会静默失效。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from betterchat_state import RateLimiter, RuntimeState  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# RateLimiter
# --------------------------------------------------------------------------- #


def test_counts_within_sliding_window(tmp_path: pathlib.Path) -> None:
    limiter = RateLimiter(tmp_path / "state.json")
    now = 1_000_000.0

    async def scenario():
        for offset in (0, 10, 20):
            await limiter.record("g1", now=now + offset)
        return limiter.count("g1", now=now + 30)

    assert run(scenario()) == 3


def test_old_entries_fall_out_of_window(tmp_path: pathlib.Path) -> None:
    limiter = RateLimiter(tmp_path / "state.json")
    now = 1_000_000.0

    async def scenario():
        await limiter.record("g1", now=now)
        await limiter.record("g1", now=now + 10)
        # 两小时后再看：全都过期了。
        return limiter.count("g1", now=now + 7200)

    assert run(scenario()) == 0


def test_recording_prunes_expired_entries(tmp_path: pathlib.Path) -> None:
    limiter = RateLimiter(tmp_path / "state.json")
    now = 1_000_000.0

    async def scenario():
        await limiter.record("g1", now=now)
        await limiter.record("g1", now=now + 7200)
        return limiter.count("g1", now=now + 7200)

    # 老的被裁掉，只剩刚记的这条。
    assert run(scenario()) == 1


def test_counts_are_per_key(tmp_path: pathlib.Path) -> None:
    limiter = RateLimiter(tmp_path / "state.json")
    now = 1_000_000.0

    async def scenario():
        await limiter.record("g1", now=now)
        await limiter.record("g2", now=now)
        await limiter.record("g2", now=now)
        return limiter.count("g1", now=now), limiter.count("g2", now=now)

    assert run(scenario()) == (1, 2)


def test_persists_across_instances(tmp_path: pathlib.Path) -> None:
    """**这是重启后限频仍然有效的保证。**"""
    path = tmp_path / "state.json"
    now = time.time()

    async def write():
        await RateLimiter(path).record("g1", now=now)

    run(write())
    assert path.exists()

    # 新实例（模拟重启）能读到之前的计数。
    assert RateLimiter(path).count("g1", now=now + 10) == 1


def test_persisted_timestamps_use_wall_clock(tmp_path: pathlib.Path) -> None:
    """落盘的时间戳必须接近 ``time.time()``，不能是单调时钟。"""
    path = tmp_path / "state.json"

    async def write():
        await RateLimiter(path).record("g1")

    run(write())
    payload = json.loads(path.read_text(encoding="utf-8"))
    stamp = payload["g1"][0]
    # 单调时钟的数值通常远小于 Unix 时间戳；这里允许 1 小时误差。
    assert abs(stamp - time.time()) < 3600
    assert stamp > 1_000_000_000  # 明显是 Unix 时间戳量级


def test_corrupt_state_file_is_ignored(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    limiter = RateLimiter(path)
    # 坏文件只影响精度，不能让插件起不来。
    assert limiter.count("g1") == 0


def test_malformed_entries_are_filtered(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "state.json"
    now = time.time()
    path.write_text(
        json.dumps({"g1": [now, "bad", True, None, now - 5]}), encoding="utf-8"
    )
    limiter = RateLimiter(path)
    # 只保留两个合法数字。
    assert limiter.count("g1", now=now) == 2


def test_reset_clears_one_key_or_all(tmp_path: pathlib.Path) -> None:
    limiter = RateLimiter(tmp_path / "state.json")
    now = time.time()

    async def scenario():
        await limiter.record("g1", now=now)
        await limiter.record("g2", now=now)
        await limiter.reset("g1")
        after_one = (limiter.count("g1", now=now), limiter.count("g2", now=now))
        await limiter.reset(None)
        after_all = limiter.count("g2", now=now)
        return after_one, after_all

    assert run(scenario()) == ((0, 1), 0)


def test_last_reply_at_returns_latest(tmp_path: pathlib.Path) -> None:
    limiter = RateLimiter(tmp_path / "state.json")
    now = 1_000_000.0

    async def scenario():
        await limiter.record("g1", now=now)
        await limiter.record("g1", now=now + 50)
        return limiter.last_reply_at("g1")

    assert run(scenario()) == now + 50


def test_unknown_key_reports_zero(tmp_path: pathlib.Path) -> None:
    limiter = RateLimiter(tmp_path / "state.json")
    assert limiter.count("nope") == 0
    assert limiter.last_reply_at("nope") == 0.0


# --------------------------------------------------------------------------- #
# RuntimeState
# --------------------------------------------------------------------------- #


def test_seconds_since_uses_monotonic_clock() -> None:
    state = RuntimeState()
    now = time.monotonic()
    state.note_bot_message("g1", now=now)
    assert state.seconds_since_bot_spoke("g1", now=now + 30) == 30


def test_never_spoken_returns_infinity() -> None:
    state = RuntimeState()
    assert state.seconds_since_bot_spoke("g1") == float("inf")
    assert state.seconds_since_last_reply("g1") == float("inf")


def test_note_reply_updates_both_markers() -> None:
    state = RuntimeState()
    now = time.monotonic()
    state.note_reply("g1", now=now)
    assert state.seconds_since_last_reply("g1", now=now) == 0
    assert state.seconds_since_bot_spoke("g1", now=now) == 0


def test_spawn_tracks_task_and_close_cancels_it() -> None:
    async def scenario():
        state = RuntimeState()
        started = asyncio.Event()

        async def worker():
            started.set()
            await asyncio.sleep(60)

        task = state.spawn(worker())
        assert task is not None
        await started.wait()
        assert task in state.tasks
        await state.close()
        assert state.closed is True
        assert task.cancelled() or task.done()
        return len(state.tasks)

    assert run(scenario()) == 0


def test_spawn_after_close_does_not_create_task() -> None:
    async def scenario():
        state = RuntimeState()
        await state.close()

        async def worker():
            await asyncio.sleep(0)

        return state.spawn(worker())

    # 已关闭时不应再拉起任务（否则会有 never awaited 警告）。
    assert run(scenario()) is None


def test_close_clears_in_flight() -> None:
    async def scenario():
        state = RuntimeState()
        state.in_flight["g1"] = object()
        await state.close()
        return len(state.in_flight)

    assert run(scenario()) == 0


def test_close_is_idempotent() -> None:
    async def scenario():
        state = RuntimeState()
        await state.close()
        await state.close()
        return state.closed

    assert run(scenario()) is True


def test_prune_drops_inactive_sessions() -> None:
    state = RuntimeState()
    now = time.monotonic()
    state.session("old")
    state.note_reply("old", now=now)
    state.session("active")
    state.note_reply("active", now=now + 9999)
    state.prune(now=now + 10_000, horizon=3600)
    assert "old" not in state.sessions
    assert "active" in state.sessions


def test_prune_keeps_never_active_sessions() -> None:
    """刚创建、还没产生任何活动的会话不能被剪掉。"""
    state = RuntimeState()
    state.session("brand-new")
    state.prune(now=time.monotonic() + 10_000, horizon=3600)
    assert "brand-new" in state.sessions
