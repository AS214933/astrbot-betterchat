"""自动复读的测试。

复读检测的核心契约：
* 指纹相同 + 连续 N 条 + **发送者两两不同** → 才算复读
* 同一个人连刷 → 是刷屏不是复读，窗口清空
* 同一内容只跟读一次（防跟读风暴）
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from betterchat_group_extras import (  # noqa: E402
    RereadDetector,
    RereadSettings,
    RereadVerdict,
    make_fingerprint,
    reread_gate,
)


class Seg:
    """模拟消息组件。"""

    def __init__(self, kind: str, **kw) -> None:
        self.type = kind
        self.__dict__.update(kw)


# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #


def test_text_fingerprint_strips_whitespace() -> None:
    assert make_fingerprint([Seg("Plain", text="  哈哈哈  ")]) == ("text", "text:哈哈哈")


def test_empty_text_has_no_fingerprint() -> None:
    assert make_fingerprint([Seg("Plain", text="   ")]) is None


def test_image_and_face_fingerprints() -> None:
    assert make_fingerprint([Seg("Image", file="a.jpg")]) == ("image", "image:a.jpg")
    assert make_fingerprint([Seg("Face", id="178")]) == ("face", "face:178")


def test_multi_segment_is_not_fingerprinted() -> None:
    """多段消息没法定义"是不是同一句话"，不参与复读。"""
    assert make_fingerprint([Seg("Plain", text="hi"), Seg("Image", file="a.jpg")]) is None
    assert make_fingerprint([]) is None


def test_unknown_component_type_returns_none() -> None:
    assert make_fingerprint([Seg("Video", file="v.mp4")]) is None


# --------------------------------------------------------------------------- #
# 检测核心
# --------------------------------------------------------------------------- #


def feed(detector: RereadDetector, kind: str, fp: str, sender: str, n: int, threshold: int) -> list[bool]:
    return [detector.observe(kind, fp, f"{sender}{i}", threshold=threshold) for i in range(n)]


def test_four_different_senders_triggers() -> None:
    det = RereadDetector()
    results = [
        det.observe("text", "text:哈哈", sender, threshold=4)
        for sender in ("a", "b", "c", "d")
    ]
    # 前三条不够，第四条触发
    assert results == [False, False, False, True]


def test_same_sender_spam_does_not_trigger() -> None:
    """一个人连刷 5 条不是复读 —— 是刷屏。"""
    det = RereadDetector()
    results = [det.observe("text", "text:哈哈", "a", threshold=4) for _ in range(5)]
    assert not any(results)


def test_same_sender_resets_window() -> None:
    """同一人连发会清空窗口，之前的累计作废。"""
    det = RereadDetector()
    det.observe("text", "text:X", "a", threshold=3)
    det.observe("text", "text:X", "b", threshold=3)
    # a 又来连发 → 窗口清空，只剩这条 a
    assert det.observe("text", "text:X", "a", threshold=3) is False
    # b 再来 → 窗口 [a, b]，只有两个不同的人，还差一个
    assert det.observe("text", "text:X", "b", threshold=3) is False
    # 第三人 → [a, b, c] 互不相同，触发
    assert det.observe("text", "text:X", "c", threshold=3) is True


def test_interleaved_duplicate_sender_does_not_trigger() -> None:
    """回归测试：尾部 N 条里发送者必须**两两不同**。

    曾经用 `len(set(senders)) == threshold` 判断，在 `[b, a, b]` 这种
    交替重复时会误判成复读 —— 集合大小够，但显然不是三个人在复读。
    改为比较 `len(set(senders)) == len(senders)`（去重后长度不变）。
    """
    det = RereadDetector()
    results = [det.observe("text", "text:X", s, threshold=3) for s in ("b", "a", "b")]
    assert not any(results), "同一人交替出现不算复读"


def test_three_distinct_senders_after_duplicate_triggers() -> None:
    """窗口里出现过重复人，但只要**尾部三条**互不相同就算复读。"""
    det = RereadDetector()
    # 窗口逐步变成 [b, a, b, c]；尾部三条 [a, b, c] 互不相同
    seen = [det.observe("text", "text:X", s, threshold=3) for s in ("b", "a", "b", "c")]
    assert seen[-1] is True


def test_mixed_fingerprints_do_not_trigger() -> None:
    """内容不同就不是复读。"""
    det = RereadDetector()
    assert det.observe("text", "text:A", "a", threshold=3) is False
    assert det.observe("text", "text:B", "b", threshold=3) is False
    assert det.observe("text", "text:C", "c", threshold=3) is False


def test_kinds_are_counted_separately() -> None:
    """文字和表情各自计数，不混在一起。"""
    det = RereadDetector()
    det.observe("text", "text:X", "a", threshold=2)
    det.observe("face", "face:1", "b", threshold=2)
    # 各自都只有 1 条，不该触发
    assert det.observe("text", "text:X", "c", threshold=2) is True
    det2 = RereadDetector()
    det2.observe("text", "text:X", "a", threshold=3)
    det2.observe("face", "face:1", "b", threshold=3)
    assert det2.observe("image", "image:i", "c", threshold=3) is False


def test_followed_fingerprint_is_not_repeated() -> None:
    """同一内容跟过一次后，不再重复跟 —— 防止跟读风暴。"""
    det = RereadDetector()
    for sender in ("a", "b", "c"):
        det.observe("text", "text:草", sender, threshold=3)
    det.mark_followed("text:草")
    # 群里继续复读同样内容，不应再触发
    results = [det.observe("text", "text:草", f"z{i}", threshold=3) for i in range(5)]
    assert not any(results)


def test_reset_clears_state() -> None:
    det = RereadDetector()
    det.observe("text", "text:X", "a", threshold=2)
    det.mark_followed("text:X")
    det.reset()
    # 重置后同样的内容可以重新触发
    assert det.observe("text", "text:X", "a", threshold=2) is False
    assert det.observe("text", "text:X", "b", threshold=2) is True


def test_threshold_has_floor_of_two() -> None:
    """阈值最小 2 —— 1 条消息不可能叫复读。"""
    det = RereadDetector()
    assert det.observe("text", "text:X", "a", threshold=1) is False
    assert det.observe("text", "text:X", "b", threshold=1) is True


# --------------------------------------------------------------------------- #
# 闸门
# --------------------------------------------------------------------------- #


def settings(**kw) -> RereadSettings:
    base = dict(
        enable=True,
        follow_probability=0.8,
        cooldown_seconds=300,
        max_per_hour=6,
    )
    base.update(kw)
    return RereadSettings(**base)


def test_gate_disabled() -> None:
    verdict, reason = reread_gate(
        settings=settings(enable=False),
        recent_follows=[],
        seconds_since_last_follow=9999,
        roll=0.0,
    )
    assert verdict is RereadVerdict.NOT_REREAD
    assert reason == "disabled"


def test_gate_cooldown_blocks_follow() -> None:
    verdict, reason = reread_gate(
        settings=settings(cooldown_seconds=300),
        recent_follows=[],
        seconds_since_last_follow=10,
        roll=0.0,
    )
    assert verdict is RereadVerdict.DETECTED_NOT_FOLLOWED
    assert reason == "cooldown"


def test_gate_hourly_limit() -> None:
    import time

    now = time.monotonic()
    verdict, reason = reread_gate(
        settings=settings(cooldown_seconds=0, max_per_hour=2),
        recent_follows=[now - 10, now - 20],
        seconds_since_last_follow=10,
        roll=0.0,
    )
    assert verdict is RereadVerdict.DETECTED_NOT_FOLLOWED
    assert reason == "hourly_limit"


def test_old_follows_fall_out_of_window() -> None:
    import time

    now = time.monotonic()
    verdict, reason = reread_gate(
        settings=settings(cooldown_seconds=0, max_per_hour=2),
        recent_follows=[now - 7200, now - 7200],  # 两小时前，已出窗口
        seconds_since_last_follow=7200,
        roll=0.0,
    )
    assert verdict is RereadVerdict.TRIGGERED


def test_gate_probability() -> None:
    ok, _ = reread_gate(
        settings=settings(follow_probability=0.8),
        recent_follows=[],
        seconds_since_last_follow=9999,
        roll=0.79,
    )
    assert ok is RereadVerdict.TRIGGERED

    no, reason = reread_gate(
        settings=settings(follow_probability=0.8),
        recent_follows=[],
        seconds_since_last_follow=9999,
        roll=0.81,
    )
    assert no is RereadVerdict.DETECTED_NOT_FOLLOWED
    assert reason == "probability"


def test_settings_parse_and_clamp() -> None:
    s = RereadSettings.from_config(
        {
            "enable": True,
            "text_threshold": 5,
            "follow_probability": 1.5,  # 夹到 1.0
            "cooldown_seconds": -10,  # 夹到 0
        }
    )
    assert s.enable is True
    assert s.text_threshold == 5
    assert s.follow_probability == 1.0
    assert s.cooldown_seconds == 0


def test_settings_default_thresholds() -> None:
    s = RereadSettings()
    assert (s.text_threshold, s.image_threshold, s.face_threshold) == (4, 3, 2)
    assert s.threshold_for("text") == 4
    assert s.threshold_for("image") == 3
    assert s.threshold_for("face") == 2
    assert s.threshold_for("unknown") == 4


def test_bad_config_falls_back() -> None:
    s = RereadSettings.from_config("not a dict")
    assert s.enable is False
    assert s.text_threshold == 4
    assert RereadSettings.from_config({"text_threshold": "abc"}).text_threshold == 4
