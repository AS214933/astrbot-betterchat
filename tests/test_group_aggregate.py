"""群聊判定前聚合的测试。

聚合的目的：群里连刷三句只花一次 Jev 调用，而不是三次。
这是本项目相对同类插件的额外优化 —— reply_gate / jev_router / czyczy23
都是每条消息判一次。

这里重点验证两件事：
1. **合并正确**：连续消息只产生一次调用，且文本是合并后的。
2. **不丢消息**：被并进来的事件必须安全退出，不能触发默认 LLM。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from betterchat_core import (  # noqa: E402
    ChatLine,
    GroupAggregator,
    GroupSettings,
    ReservationState,
    merge_group_lines,
)


def run(coro):
    return asyncio.run(coro)


def line(sender: str, text: str, **kw) -> ChatLine:
    return ChatLine(sender=sender, text=text, **kw)


# --------------------------------------------------------------------------- #
# 合并规则
# --------------------------------------------------------------------------- #


def test_same_sender_lines_are_joined() -> None:
    merged = merge_group_lines(
        [line("小明", "那个"), line("小明", "我想问一下"), line("小明", "关于退款")]
    )
    assert merged.sender == "小明"
    assert merged.text == "那个\n我想问一下\n关于退款"


def test_different_senders_are_not_fused() -> None:
    """不同人说的话不能硬拼成一句 —— 会让 Jev 误解语义。"""
    merged = merge_group_lines([line("小明", "AAA"), line("小红", "BBB")])
    assert merged.sender == "小红"
    assert merged.text == "BBB"  # 保留最后一条作为主体


def test_merge_keeps_at_bot_and_is_self_flags() -> None:
    merged = merge_group_lines(
        [line("小明", "a", at_bot=True), line("小明", "b")]
    )
    assert merged.at_bot is True


def test_merge_single_line_is_identity() -> None:
    only = line("a", "x", seconds_ago=3.0)
    assert merge_group_lines([only]) is only


def test_merge_empty_returns_empty_line() -> None:
    merged = merge_group_lines([])
    assert merged.text == ""


# --------------------------------------------------------------------------- #
# 聚合窗口行为
# --------------------------------------------------------------------------- #


def test_burst_is_aggregated_into_one_verdict() -> None:
    """三连发 → 只结算一次，且文本是三句合并的结果。"""

    async def scenario():
        agg = GroupAggregator()
        first = agg.reserve("g", line("u", "那个"), window_seconds=0.05)
        second = agg.reserve("g", line("u", "我想问下"), window_seconds=0.05)
        third = agg.reserve("g", line("u", "退款怎么弄"), window_seconds=0.05)

        # 三条的等待者里，只有最新的能拿到 OWNER。
        states = await asyncio.gather(
            agg.wait("g", first), agg.wait("g", second), agg.wait("g", third)
        )
        owners = [s for s in states if s is ReservationState.OWNER]
        assert len(owners) == 1, f"只应有一次结算，实际 {states}"

        # 由最新的那条收口。
        assert states[2] is ReservationState.OWNER
        batch = await agg.settle("g", third)
        assert batch is not None
        assert [b.text for b in batch] == ["那个", "我想问下", "退款怎么弄"]
        return batch

    batch = run(scenario())
    assert merge_group_lines(batch).text == "那个\n我想问下\n退款怎么弄"


def test_window_extends_on_new_message() -> None:
    """窗口期内又来消息 → 窗口顺延，不会提前结算。"""

    async def scenario():
        agg = GroupAggregator()
        first = agg.reserve("g", line("u", "a"), window_seconds=0.08)
        task = asyncio.create_task(agg.wait("g", first))
        await asyncio.sleep(0.03)
        # 此时第一条还没等到窗口关闭，再来一条把窗口推后。
        agg.reserve("g", line("u", "b"), window_seconds=0.08)
        state = await asyncio.wait_for(task, timeout=2.0)
        # 第一条被接管，不是它收口。
        return state

    assert run(scenario()) is ReservationState.SUPERSEDED


def test_zero_window_settles_immediately() -> None:
    """窗口为 0 时退化成"每条各判一次"（旧行为）。"""

    async def scenario():
        agg = GroupAggregator()
        first = agg.reserve("g", line("u", "x"), window_seconds=0.0)
        state = await agg.wait("g", first)
        assert state is ReservationState.OWNER
        return await agg.settle("g", first)

    batch = run(scenario())
    assert batch is not None and len(batch) == 1


def test_close_wakes_waiters() -> None:
    """热重载时必须唤醒等待者，不能留孤儿任务。"""

    async def scenario():
        agg = GroupAggregator()
        first = agg.reserve("g", line("u", "x"), window_seconds=60.0)
        task = asyncio.create_task(agg.wait("g", first))
        await asyncio.sleep(0)
        await agg.close()
        return await asyncio.wait_for(task, timeout=2.0)

    assert run(scenario()) is ReservationState.BYPASS


def test_settle_twice_returns_none() -> None:
    """同一批不能被结算两次（否则会重复调用 Jev）。"""

    async def scenario():
        agg = GroupAggregator()
        first = agg.reserve("g", line("u", "x"), window_seconds=0.0)
        await agg.wait("g", first)
        assert await agg.settle("g", first) is not None
        return await agg.settle("g", first)

    assert run(scenario()) is None


def test_separate_groups_are_independent() -> None:
    """两个群同时聚合，互不干扰。"""

    async def scenario():
        agg = GroupAggregator()
        a = agg.reserve("g1", line("u", "群1"), window_seconds=0.0)
        b = agg.reserve("g2", line("u", "群2"), window_seconds=0.0)
        sa = await agg.wait("g1", a)
        sb = await agg.wait("g2", b)
        return sa, sb, await agg.settle("g1", a), await agg.settle("g2", b)

    sa, sb, ba, bb = run(scenario())
    assert sa is ReservationState.OWNER and sb is ReservationState.OWNER
    assert ba[0].text == "群1" and bb[0].text == "群2"


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #


def test_aggregate_window_default_is_one_and_half_seconds() -> None:
    assert GroupSettings().aggregate_window_seconds == 1.5


def test_aggregate_window_parsed_and_clamped() -> None:
    assert GroupSettings.from_config({"aggregate_window_seconds": 2.5}).aggregate_window_seconds == 2.5
    assert GroupSettings.from_config({"aggregate_window_seconds": 0}).aggregate_window_seconds == 0.0
    # 负数夹到 0，过大夹到 10。
    assert GroupSettings.from_config({"aggregate_window_seconds": -3}).aggregate_window_seconds == 0.0
    assert GroupSettings.from_config({"aggregate_window_seconds": 999}).aggregate_window_seconds == 10.0
    # 非法值退回默认。
    assert GroupSettings.from_config({"aggregate_window_seconds": "abc"}).aggregate_window_seconds == 1.5


def test_concurrent_burst_exactly_one_owner() -> None:
    """回归测试：并发到达时必须有且只有一个 owner。

    曾经这里有个 bug：``reserve`` 返回"是否并入已有窗口"，调用方据此
    决定"我并入了我先走"。但并发下第 0 条会被第 1 条顶成 SUPERSEDED，
    而第 1、2 条又都判定自己"已并入"直接返回 —— 结果**没有任何一条**
    负责结算，这次判定直接丢失（表现是群里刷屏时 Bot 突然不说话）。

    现在所有到达者都进 wait，由最终仍是 owner 的那条收口。
    """

    async def scenario():
        agg = GroupAggregator()
        owners = []
        settled_texts = []

        async def handle(i: int):
            res = agg.reserve("g", line("u", f"第{i}句"), window_seconds=0.05)
            state = await agg.wait("g", res)
            if state is ReservationState.OWNER:
                owners.append(i)
                batch = await agg.settle("g", res)
                if batch:
                    settled_texts.append(merge_group_lines(batch).text)

        await asyncio.gather(*[handle(i) for i in range(5)])
        return owners, settled_texts, "g" in agg._turns  # noqa: SLF001

    owners, settled_texts, leftover = run(scenario())
    assert len(owners) == 1, f"必须恰好一个 owner，实际 {owners}"
    assert leftover is False, "窗口应已清理"
    assert len(settled_texts) == 1
    # 五句都应该在合并结果里。
    assert settled_texts[0].count(chr(10)) == 4
