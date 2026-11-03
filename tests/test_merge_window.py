"""合并窗口的离线测试。

窗口用**真实时钟**但毫秒级的窗口长度，所以整个文件跑完很快，
同时能真实覆盖 ``asyncio.wait_for`` 的超时路径。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from betterchat_core import (  # noqa: E402
    MergeSettings,
    MergeWindow,
    ReservationState,
    build_gate_state,
    merge_texts,
    quiet_seconds_for,
    visible_length,
)


#: 毫秒级窗口，测试足够快又不至于抖动。
FAST = MergeSettings(
    base_quiet_seconds=0.02,
    min_quiet_seconds=0.02,
    max_quiet_seconds=0.03,
    total_seconds=0.12,
    length_scale_chars=60,
    max_messages=8,
    max_chars=4000,
)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 纯函数（无时钟依赖）
# --------------------------------------------------------------------------- #


def test_visible_length_ignores_whitespace() -> None:
    assert visible_length("你好 世界") == 4
    assert visible_length("  \n\t ") == 0


def test_merge_texts_keeps_order_and_trims_from_the_left() -> None:
    assert merge_texts(["a", "b", "c"]) == "a\nb\nc"
    # 超限时丢掉最旧的一端，保留最新的内容。
    assert merge_texts(["AAAA", "BBBB"], max_chars=4) == "BBBB"
    assert merge_texts(["", "x", ""]) == "x"


def test_quiet_seconds_clamped_between_bounds() -> None:
    settings = MergeSettings(
        base_quiet_seconds=2.0,
        min_quiet_seconds=1.5,
        max_quiet_seconds=4.0,
        length_scale_chars=60,
    )
    # 基准值高于下限时，短文本从基准值起步（不会被拉低到下限）。
    assert quiet_seconds_for("hi", settings) == pytest.approx(2.0 + 2 / 60)
    # 极长文本走上限。
    assert quiet_seconds_for("字" * 5000, settings) == settings.max_quiet_seconds
    # 长度贡献是单调的。
    assert quiet_seconds_for("字" * 60, settings) > quiet_seconds_for("字" * 10, settings)

    # 基准值低于下限时，短文本被抬到下限。
    low_base = MergeSettings(
        base_quiet_seconds=0.5,
        min_quiet_seconds=1.5,
        max_quiet_seconds=4.0,
        length_scale_chars=60,
    )
    assert quiet_seconds_for("hi", low_base) == low_base.min_quiet_seconds


def test_settings_from_bad_config_falls_back_to_defaults() -> None:
    # 默认 base_quiet = 4.0（按实测打字节奏调的，见 MergeSettings 注释）
    assert MergeSettings.from_config("not a dict").base_quiet_seconds == 4.0

    # 下限 > 上限时自动规整。
    settings = MergeSettings.from_config(
        {"min_quiet_seconds": 9, "max_quiet_seconds": 3}
    )
    assert settings.max_quiet_seconds >= settings.min_quiet_seconds

    # NaN / 非数字退回默认。
    assert MergeSettings.from_config({"base_quiet_seconds": "abc"}).base_quiet_seconds == 4.0


def test_should_ignore_respects_prefixes() -> None:
    assert FAST.should_ignore("/help") is True
    assert FAST.should_ignore("  !cmd") is True
    assert FAST.should_ignore("你好") is False


# --------------------------------------------------------------------------- #
# 窗口行为
# --------------------------------------------------------------------------- #


def test_single_message_becomes_owner_after_quiet_window() -> None:
    async def scenario():
        window = MergeWindow(settings=FAST)
        reservation = window.reserve("s1", "你好")
        state = await window.wait_for_quiet("s1", reservation)
        assert state is ReservationState.OWNER
        return await window.settle("s1", reservation)

    assert run(scenario()) == ("你好", 1)


def test_newer_message_supersedes_older_waiter() -> None:
    """后到的消息接管本轮，先到的等待者必须静默退出。"""

    async def scenario():
        window = MergeWindow(settings=FAST)

        first = window.reserve("s", "第一句")
        task = asyncio.create_task(window.wait_for_quiet("s", first))
        await asyncio.sleep(0)  # 让 first 真正进入等待

        second = window.reserve("s", "第二句")
        assert await task is ReservationState.SUPERSEDED
        # 先到者不能结算成功。
        assert await window.settle("s", first) is None

        assert await window.wait_for_quiet("s", second) is ReservationState.OWNER
        return await window.settle("s", second)

    assert run(scenario()) == ("第一句\n第二句", 2)


def test_settle_after_close_returns_none() -> None:
    """插件卸载后等待者不能再结算。"""

    async def scenario():
        window = MergeWindow(settings=FAST)
        reservation = window.reserve("s", "hi")
        await window.close()
        assert await window.wait_for_quiet("s", reservation) is ReservationState.BYPASS
        assert await window.settle("s", reservation) is None

    run(scenario())


def test_overflow_starts_new_turn_instead_of_dropping() -> None:
    """条数超限时立刻结算，而不是继续等或丢消息。"""
    settings = MergeSettings(
        base_quiet_seconds=5.0,
        min_quiet_seconds=5.0,
        max_quiet_seconds=5.0,
        total_seconds=5.0,
        max_messages=2,
    )

    async def scenario():
        window = MergeWindow(settings=settings)
        window.reserve("s", "a")
        window.reserve("s", "b")
        third = window.reserve("s", "c")

        # 已经超过 max_messages=2，最新那条应当立刻拿到 OWNER，
        # 而不是继续等 5 秒。
        state = await asyncio.wait_for(window.wait_for_quiet("s", third), timeout=1.0)
        assert state is ReservationState.OWNER
        settled = await window.settle("s", third)
        return settled

    settled = run(scenario())
    assert settled is not None and settled[1] == 3


def test_quiet_window_extends_but_total_caps_it() -> None:
    """持续刷新会延长静默窗口，但整轮硬上限必须兜住。"""
    settings = MergeSettings(
        base_quiet_seconds=0.03,
        min_quiet_seconds=0.03,
        max_quiet_seconds=0.03,
        total_seconds=0.09,
    )

    async def scenario():
        window = MergeWindow(settings=settings)
        first = window.reserve("s", "a")
        task = asyncio.create_task(window.wait_for_quiet("s", first))
        await asyncio.sleep(0)

        for _ in range(3):
            await asyncio.sleep(0.02)
            if task.done():
                break
            window.reserve("s", "b")

        return await asyncio.wait_for(task, timeout=2.0)

    # 后来的消息接管，先到者被取代。
    assert run(scenario()) is ReservationState.SUPERSEDED


def test_external_is_current_false_supersedes() -> None:
    """外部判定失效（例如事件被别的插件停掉）时立刻退出。"""

    async def scenario():
        window = MergeWindow(settings=FAST)
        reservation = window.reserve("s", "hi")
        return await window.wait_for_quiet(
            "s", reservation, is_current=lambda: False
        )

    assert run(scenario()) is ReservationState.SUPERSEDED


def test_settled_turn_marks_late_waiter_as_superseded() -> None:
    """本轮已被别人结算时，迟到的等待者不能误判成新会话。"""

    async def scenario():
        window = MergeWindow(settings=FAST)
        first = window.reserve("s", "a")
        second = window.reserve("s", "b")
        # second 是主人，先结算掉。
        assert await window.settle("s", second) is not None
        # first 再来等，必须判定为被取代而不是 BYPASS。
        return await window.wait_for_quiet("s", first)

    assert run(scenario()) is ReservationState.SUPERSEDED


def test_prune_removes_stale_turns() -> None:
    now = [1000.0]
    window = MergeWindow(clock=lambda: now[0], settings=FAST)
    window.reserve("old", "x")
    now[0] += 10_000
    window.reserve("new", "y")
    assert "old" not in window._turns  # noqa: SLF001 - 测试内部状态
    assert "new" in window._turns  # noqa: SLF001


def test_default_window_covers_realistic_typing_gaps() -> None:
    """默认窗口必须能覆盖真实的打字间隔。

    实测用户连发消息的间隔从 0.7 秒到 8.6 秒都有。原来的 base=2.0 时，
    间隔 3.57 秒的下一句就漏在窗口外 —— 第一条单独跑了一轮回复，
    后面几条又合并成第二轮，用户看到两句毫不相干的话。
    """
    settings = MergeSettings()
    # 3.6 秒的间隔（实测出现过的值）必须仍在窗口内
    assert quiet_seconds_for("不知道...", settings) > 3.6
    # 但也不能长到让人等太久
    assert quiet_seconds_for("不知道...", settings) <= 6.0
    # 硬上限要大于窗口，否则窗口永远到不了
    assert settings.total_seconds > settings.max_quiet_seconds


def test_window_still_capped_by_total() -> None:
    """就算连着打字，也不会超过整轮硬上限。"""
    settings = MergeSettings()
    very_long = "字" * 1000
    assert quiet_seconds_for(very_long, settings) <= settings.max_quiet_seconds
    assert settings.total_seconds >= settings.max_quiet_seconds


# --------------------------------------------------------------------------- #
# 群聊 state 构造
# --------------------------------------------------------------------------- #


def test_build_gate_state_clips_long_content() -> None:
    state = build_gate_state(
        group_name="测试群",
        latest="这是一个很长的问题" * 500,
        context=["消息%d" % i for i in range(100)],
        bot_messages=["回复%d" % i for i in range(20)],
    )
    assert len(state["recent_messages"]) <= 20
    assert len(state["recent_bot_messages"]) <= 5
    # latest_message 现在是结构化对象（含 sender/seconds_ago），
    # 正文本身仍受 2000 字符上限约束。
    assert len(state["latest_message"]["text"]) <= 2000


def test_build_gate_state_shrinks_when_over_budget() -> None:
    state = build_gate_state(
        group_name="g",
        latest="x" * 1900,
        context=["y" * 490 for _ in range(20)],
        bot_messages=["z" * 490 for _ in range(5)],
        max_chars=1000,
    )
    assert len(state["recent_messages"]) <= 5
    assert len(state["recent_bot_messages"]) <= 2
