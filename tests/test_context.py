"""群聊上下文的测试。

背景：AstrBot 的 ``AstrBotMessage`` 只带**当前这一条**消息，没有
"最近 N 条群消息"这种东西。所以上下文必须插件自己记 ——
``reply_gate``、``czyczy23/astrbot_plugin_jev`` 也都是这么做的。

这里重点是抓一个曾经真实存在的缺陷：早期实现去读
``event.message_obj.group_messages`` 等**根本不存在的属性**，
结果上下文永远是空列表，判定等于在盲猜，而且不报任何错。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from betterchat_core import ChatLine  # noqa: E402
from betterchat_state import TranscriptCache  # noqa: E402


# --------------------------------------------------------------------------- #
# 缓存本身
# --------------------------------------------------------------------------- #


def test_records_and_returns_in_order() -> None:
    cache = TranscriptCache(maxlen=10)
    for i in range(3):
        cache.record("g", ChatLine(sender="u", text=f"第{i}句"))
    texts = [c.text for c in cache.recent("g")]
    assert texts == ["第0句", "第1句", "第2句"]


def test_limit_returns_newest() -> None:
    cache = TranscriptCache(maxlen=10)
    for i in range(6):
        cache.record("g", ChatLine(sender="u", text=f"第{i}句"))
    assert [c.text for c in cache.recent("g", 2)] == ["第4句", "第5句"]


def test_maxlen_drops_oldest() -> None:
    cache = TranscriptCache(maxlen=3)
    for i in range(6):
        cache.record("g", ChatLine(sender="u", text=f"第{i}句"))
    assert [c.text for c in cache.recent("g")] == ["第3句", "第4句", "第5句"]


def test_dedupes_same_message_id() -> None:
    """同一条消息被多个 handler 看到时不能重复记录。"""
    cache = TranscriptCache()
    line = ChatLine(sender="u", text="只该出现一次", message_id="m1")
    cache.record("g", line)
    cache.record("g", line)
    cache.record("g", line)
    assert len(cache.recent("g")) == 1


def test_different_message_ids_both_kept() -> None:
    cache = TranscriptCache()
    cache.record("g", ChatLine(sender="u", text="a", message_id="m1"))
    cache.record("g", ChatLine(sender="u", text="b", message_id="m2"))
    assert len(cache.recent("g")) == 2


def test_sessions_are_isolated() -> None:
    cache = TranscriptCache()
    cache.record("g1", ChatLine(sender="u", text="群1"))
    cache.record("g2", ChatLine(sender="u", text="群2"))
    assert [c.text for c in cache.recent("g1")] == ["群1"]
    assert [c.text for c in cache.recent("g2")] == ["群2"]


def test_unknown_session_returns_empty() -> None:
    assert TranscriptCache().recent("nope") == []


def test_clear_one_or_all() -> None:
    cache = TranscriptCache()
    cache.record("g1", ChatLine(sender="u", text="a"))
    cache.record("g2", ChatLine(sender="u", text="b"))
    cache.clear("g1")
    assert cache.recent("g1") == []
    assert cache.recent("g2") != []
    cache.clear()
    assert cache.recent("g2") == []


def test_prune_keeps_bounded() -> None:
    cache = TranscriptCache()
    for i in range(60):
        cache.record(f"g{i}", ChatLine(sender="u", text="x"))
    cache.prune(keep=10)
    remaining = sum(1 for i in range(60) if cache.recent(f"g{i}"))
    assert remaining == 10


def test_maxlen_has_floor() -> None:
    """maxlen 至少为 2，避免配置成 1 后完全失去上下文。"""
    assert TranscriptCache(maxlen=1).maxlen == 2
    assert TranscriptCache(maxlen=0).maxlen == 2


# --------------------------------------------------------------------------- #
# ChatLine 结构
# --------------------------------------------------------------------------- #


def test_chat_line_defaults() -> None:
    line = ChatLine(sender="u", text="x")
    assert line.is_self is False
    assert line.at_bot is False
    assert line.message_id == ""


def test_chat_line_keeps_flags_through_cache() -> None:
    cache = TranscriptCache()
    cache.record(
        "g",
        ChatLine(sender="bot", text="我是 Bot", is_self=True, at_bot=False),
    )
    stored = cache.recent("g")[0]
    assert stored.is_self is True
    assert stored.sender == "bot"
