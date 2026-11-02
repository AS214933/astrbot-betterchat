"""防冷场的测试。

核心契约：
* 沉默够久 + 各种保护闸门都过了，才算"可以开口"
* 从没来过消息的群不开口（避免刚拉进群就自言自语）
* 免打扰时段（支持跨午夜）
* 每日/冷却/历史活跃度限制
"""

from __future__ import annotations

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from betterchat_group_extras import (  # noqa: E402
    ProactiveAction,
    ProactiveSettings,
    SilenceTracker,
    in_quiet_hours,
    proactive_gate,
)

INF = math.inf


def settings(**kw) -> ProactiveSettings:
    base = dict(
        enable=True,
        silence_seconds=1200,
        require_prior_activity=True,
        quiet_hours="23-7",
        max_per_day=3,
        bot_cooldown_seconds=1800,
        min_messages_before=3,
    )
    base.update(kw)
    return ProactiveSettings(**base)


def gate(**kw):
    params = dict(
        settings=settings(),
        silence_seconds=1500.0,
        seconds_since_bot=INF,
        proactive_today=0,
        message_count=10,
        hour_of_day=14,  # 下午，不在免打扰时段
    )
    params.update(kw)
    return proactive_gate(**params)


# --------------------------------------------------------------------------- #
# 免打扰时段
# --------------------------------------------------------------------------- #


def test_quiet_hours_overnight() -> None:
    hours = [h for h in range(24) if in_quiet_hours("23-7", h)]
    assert hours == [0, 1, 2, 3, 4, 5, 6, 23]


def test_quiet_hours_same_day() -> None:
    hours = [h for h in range(24) if in_quiet_hours("14-18", h)]
    assert hours == [14, 15, 16, 17]


def test_quiet_hours_end_exclusive() -> None:
    assert in_quiet_hours("14-18", 17) is True
    assert in_quiet_hours("14-18", 18) is False


def test_quiet_hours_empty_means_no_restriction() -> None:
    assert all(not in_quiet_hours("", h) for h in range(24))


def test_quiet_hours_invalid_input_is_permissive() -> None:
    """配置写坏了宁可不管制，也不要整天不说话。"""
    for bad in ("abc", "8-8", "-", "25-30"):
        assert not any(in_quiet_hours(bad, h) for h in range(24)), bad


# --------------------------------------------------------------------------- #
# 闸门
# --------------------------------------------------------------------------- #


def test_disabled_never_speaks() -> None:
    action, reason = gate(settings=settings(enable=False))
    assert action is ProactiveAction.SKIP
    assert reason == "disabled"


def test_no_history_never_speaks() -> None:
    """群里从没来过消息 → 不知道氛围，不开口。"""
    action, reason = gate(silence_seconds=INF, message_count=0)
    assert action is ProactiveAction.SKIP
    assert reason == "no_history"


def test_quiet_hours_blocks() -> None:
    action, reason = gate(hour_of_day=3)
    assert action is ProactiveAction.SKIP
    assert reason == "quiet_hours"


def test_not_silent_enough() -> None:
    action, reason = gate(silence_seconds=600.0)
    assert action is ProactiveAction.SKIP
    assert reason == "not_silent_enough"


def test_require_prior_activity() -> None:
    action, reason = gate(message_count=1, settings=settings(min_messages_before=3))
    assert action is ProactiveAction.SKIP
    assert reason == "not_enough_history"


def test_prior_activity_can_be_disabled() -> None:
    action, _ = gate(
        message_count=1,
        settings=settings(require_prior_activity=False, min_messages_before=3),
    )
    assert action is ProactiveAction.SPEAK


def test_bot_cooldown_blocks() -> None:
    action, reason = gate(seconds_since_bot=100.0)
    assert action is ProactiveAction.SKIP
    assert reason == "bot_cooldown"


def test_daily_limit() -> None:
    action, reason = gate(proactive_today=3, settings=settings(max_per_day=3))
    assert action is ProactiveAction.SKIP
    assert reason == "daily_limit"


def test_speaks_when_everything_passes() -> None:
    action, reason = gate()
    assert action is ProactiveAction.SPEAK
    assert reason == "ok"


def test_zero_limits_mean_unlimited() -> None:
    action, _ = gate(
        proactive_today=999,
        seconds_since_bot=0.0,
        settings=settings(max_per_day=0, bot_cooldown_seconds=0),
    )
    assert action is ProactiveAction.SPEAK


def test_checks_are_in_cheap_order() -> None:
    """从没来过消息时不该先去算免打扰 —— 直接 no_history 退出。"""
    action, reason = gate(silence_seconds=INF, hour_of_day=3)
    assert reason == "no_history"


# --------------------------------------------------------------------------- #
# 沉默计时
# --------------------------------------------------------------------------- #


def test_silence_tracker_basics() -> None:
    tracker = SilenceTracker()
    tracker.note_message("g", now=1000.0)
    assert tracker.silence_seconds("g", now=1300.0) == 300.0


def test_silence_unknown_group_is_infinite() -> None:
    tracker = SilenceTracker()
    assert tracker.silence_seconds("nope") == INF
    assert tracker.seconds_since_bot("nope") == INF


def test_bot_message_does_not_reset_silence() -> None:
    """Bot 自己说话不算"群里有人说话"，否则会永不自触发。"""
    tracker = SilenceTracker()
    tracker.note_message("g", now=1000.0)
    tracker.note_bot("g", now=1100.0)
    assert tracker.silence_seconds("g", now=2000.0) == 1000.0
    assert tracker.seconds_since_bot("g", now=2000.0) == 900.0


def test_message_count_accumulates() -> None:
    tracker = SilenceTracker()
    for i in range(5):
        tracker.note_message("g", now=float(i))
    assert tracker.group("g").message_count == 5


def test_proactive_counter_resets_on_new_day() -> None:
    tracker = SilenceTracker()
    assert tracker.bump_proactive("g", day_stamp="2026-11-01") == 1
    assert tracker.bump_proactive("g", day_stamp="2026-11-01") == 2
    assert tracker.proactive_count("g", day_stamp="2026-11-01") == 2
    # 换天自动归零
    assert tracker.proactive_count("g", day_stamp="2026-11-02") == 0
    assert tracker.bump_proactive("g", day_stamp="2026-11-02") == 1


def test_groups_are_isolated() -> None:
    tracker = SilenceTracker()
    tracker.note_message("g1", now=1000.0)
    assert tracker.silence_seconds("g2") == INF


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #


def test_defaults() -> None:
    s = ProactiveSettings()
    assert s.enable is False
    assert s.silence_seconds == 1200  # 20 分钟
    assert s.quiet_hours == "23-7"
    assert s.max_per_day == 3
    assert s.require_prior_activity is True


def test_parse_and_clamp() -> None:
    s = ProactiveSettings.from_config(
        {
            "enable": True,
            "silence_seconds": 10,  # 夹到最小值 60
            "max_per_day": -5,  # 夹到 0
            "quiet_hours": "22-8",
        }
    )
    assert s.enable is True
    assert s.silence_seconds == 60
    assert s.max_per_day == 0
    assert s.quiet_hours == "22-8"


def test_bad_config_falls_back() -> None:
    s = ProactiveSettings.from_config("nope")
    assert s.enable is False
    assert s.silence_seconds == 1200
    assert ProactiveSettings.from_config({"silence_seconds": "abc"}).silence_seconds == 1200


# --------------------------------------------------------------------------- #
# Jev 问题
# --------------------------------------------------------------------------- #


def test_proactive_questions_use_injected_class() -> None:
    """问题构建通过注入的类完成，本模块不 import 业务代码。"""

    class FakeNoul:
        def __init__(self, instructions, criteria=None):
            self.instructions = instructions
            self.criteria = criteria

    from betterchat_group_extras import build_proactive_questions

    questions = build_proactive_questions(FakeNoul)
    assert set(questions) == {"should_speak", "has_topic", "mood_fit"}
    for q in questions.values():
        assert isinstance(q, FakeNoul)
        assert q.instructions
        # 每条都要有明确边界
        assert set(q.criteria) == {"true", "false"}
