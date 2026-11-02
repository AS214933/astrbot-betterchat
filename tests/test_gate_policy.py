"""群聊闸门与决策策略的测试。

两条主线：
* :func:`gate_reason` —— 本地短路，必须先于任何 HTTP 请求生效。
* :func:`decide_group_action` —— 概率到动作的映射，是 Bot 脾气的定义处。
"""

from __future__ import annotations

import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from betterchat_core import (  # noqa: E402
    Action,
    GateReason,
    GroupSettings,
    decide_group_action,
    gate_reason,
    resolve_fail_policy,
)

INF = math.inf


def gate(**overrides):
    """用一套"一切正常"的默认参数调用闸门，只覆盖关心的字段。"""
    params = dict(
        settings=GroupSettings(),
        text="这是一个正常的群聊问题",
        is_self=False,
        is_command=False,
        already_woken=False,
        recently_replied=0,
        seconds_since_last_reply=INF,
        seconds_since_bot_spoke=INF,
        breaker_open=False,
    )
    params.update(overrides)
    return gate_reason(**params)


# --------------------------------------------------------------------------- #
# 闸门：短路顺序
# --------------------------------------------------------------------------- #


def test_disabled_plugin_never_judges() -> None:
    decision = gate(settings=GroupSettings(enable=False))
    assert decision.reason is GateReason.DISABLED
    assert decision.should_judge is False


def test_self_message_is_skipped() -> None:
    decision = gate(is_self=True)
    assert decision.reason is GateReason.SELF
    assert decision.should_judge is False


def test_command_is_skipped() -> None:
    decision = gate(is_command=True)
    assert decision.reason is GateReason.COMMAND
    assert decision.should_judge is False


def test_short_message_is_skipped() -> None:
    decision = gate(text="哈哈", settings=GroupSettings(min_message_length=6))
    assert decision.reason is GateReason.TOO_SHORT
    assert decision.should_judge is False


def test_whitespace_does_not_count_toward_length() -> None:
    decision = gate(text="  哈   哈  ", settings=GroupSettings(min_message_length=6))
    assert decision.reason is GateReason.TOO_SHORT


def test_breaker_open_blocks_active_insertion() -> None:
    decision = gate(breaker_open=True)
    assert decision.reason is GateReason.BREAKER_OPEN
    assert decision.should_judge is False


def test_hourly_cap_blocks_active_insertion() -> None:
    decision = gate(
        recently_replied=8, settings=GroupSettings(max_replies_per_hour=8)
    )
    assert decision.reason is GateReason.RATE_LIMITED
    assert decision.should_judge is False


def test_min_interval_blocks_active_insertion() -> None:
    decision = gate(
        seconds_since_last_reply=10,
        settings=GroupSettings(min_interval_seconds=120),
    )
    assert decision.reason is GateReason.RATE_LIMITED


def test_bot_cooldown_blocks_active_insertion() -> None:
    decision = gate(
        seconds_since_bot_spoke=5,
        settings=GroupSettings(bot_cooldown_seconds=90),
    )
    assert decision.reason is GateReason.RATE_LIMITED


def test_rate_limits_do_not_apply_to_woken_messages() -> None:
    """被 @ 的消息不受主动插话限频约束 —— 用户叫了就该应。"""
    decision = gate(
        already_woken=True,
        recently_replied=999,
        seconds_since_last_reply=0,
        seconds_since_bot_spoke=0,
        breaker_open=True,
        settings=GroupSettings(max_replies_per_hour=1),
    )
    assert decision.reason is GateReason.PASS
    assert decision.should_judge is True


def test_woken_but_short_message_is_not_judged() -> None:
    decision = gate(
        already_woken=True,
        text="在吗",
        settings=GroupSettings(min_message_length=6),
    )
    assert decision.reason is GateReason.TOO_SHORT
    # 不被判定，但仍应由调用方放行原生流程。
    assert decision.already_woken is True


def test_judge_all_messages_false_skips_unwoken() -> None:
    decision = gate(settings=GroupSettings(judge_all_messages=False))
    assert decision.reason is GateReason.DISABLED
    assert decision.should_judge is False


def test_judge_all_messages_false_still_judges_woken() -> None:
    decision = gate(
        already_woken=True, settings=GroupSettings(judge_all_messages=False)
    )
    assert decision.should_judge is True


def test_zero_limits_mean_unlimited() -> None:
    decision = gate(
        recently_replied=10_000,
        seconds_since_last_reply=0,
        seconds_since_bot_spoke=0,
        settings=GroupSettings(
            max_replies_per_hour=0,
            min_interval_seconds=0,
            bot_cooldown_seconds=0,
        ),
    )
    assert decision.reason is GateReason.PASS


def test_settings_from_bad_config_falls_back() -> None:
    assert GroupSettings.from_config(None).enable is True
    # 非法的失败策略退回保守值。
    assert GroupSettings.from_config({"fail_policy": "banana"}).fail_policy == "closed"
    # 负值被夹到 0。
    settings = GroupSettings.from_config({"min_message_length": -5})
    assert settings.min_message_length == 0


# --------------------------------------------------------------------------- #
# 决策：概率 → 动作
# --------------------------------------------------------------------------- #


def decide(**overrides):
    params = dict(
        addressed=0.9,
        open_question=0.8,
        already_answered=0.1,
        already_woken=False,
        reply_threshold=0.7,
        already_answered_threshold=0.7,
    )
    params.update(overrides)
    return decide_group_action(**params)


def test_respond_when_addressed_with_question() -> None:
    verdict = decide()
    assert verdict.action is Action.RESPOND
    assert verdict.reason == "addressed_with_question"


def test_woken_always_responds() -> None:
    verdict = decide(already_woken=True, addressed=0.05, open_question=0.05)
    assert verdict.action is Action.RESPOND
    assert verdict.reason == "woken"


def test_ignore_when_already_answered() -> None:
    verdict = decide(already_answered=0.95)
    assert verdict.action is Action.IGNORE
    assert verdict.reason == "already_answered"


def test_already_answered_beats_woken() -> None:
    """被 @ 了但刚答过同一件事 —— 仍然不重复回复。"""
    verdict = decide(already_woken=True, already_answered=0.99)
    assert verdict.action is Action.IGNORE


def test_respond_to_unanswered_question_in_group() -> None:
    """群里有人提了个没人接的问题，主动帮一把。"""
    verdict = decide(addressed=0.1, open_question=0.9)
    assert verdict.action is Action.RESPOND
    assert verdict.reason == "unanswered_question"


def test_ignore_ambiguous_chatter() -> None:
    verdict = decide(addressed=0.4, open_question=0.5)
    assert verdict.action is Action.IGNORE
    assert verdict.reason == "not_addressed"


def test_threshold_controls_aggressiveness() -> None:
    """同一个概率下，调高阈值应当让 Bot 更保守。"""
    strict = decide(addressed=0.72, open_question=0.8, reply_threshold=0.9)
    loose = decide(addressed=0.72, open_question=0.8, reply_threshold=0.7)
    assert loose.action is Action.RESPOND
    assert strict.action is Action.IGNORE


@pytest.mark.parametrize(
    "field",
    ["addressed", "open_question", "already_answered"],
)
@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_probability_yields_unknown(field: str, bad: float | None) -> None:
    verdict = decide(**{field: bad})
    assert verdict.action is Action.UNKNOWN
    assert verdict.reason == "invalid_probability"


def test_verdict_carries_probabilities_for_diagnostics() -> None:
    verdict = decide(addressed=0.8, open_question=0.6, already_answered=0.2)
    assert verdict.addressed == 0.8
    assert verdict.open_question == 0.6
    assert verdict.already_answered == 0.2


# --------------------------------------------------------------------------- #
# 失败策略
# --------------------------------------------------------------------------- #


def test_fail_closed_ignores_unwoken() -> None:
    assert resolve_fail_policy("closed", already_woken=False) is Action.IGNORE


def test_fail_open_responds_unwoken() -> None:
    assert resolve_fail_policy("open", already_woken=False) is Action.RESPOND


@pytest.mark.parametrize("policy", ["closed", "open"])
def test_woken_always_responds_even_on_failure(policy: str) -> None:
    """用户明确叫了 Bot，判定服务挂掉不能变成"不理人"。"""
    assert resolve_fail_policy(policy, already_woken=True) is Action.RESPOND


def test_unknown_policy_falls_back_to_closed() -> None:
    assert resolve_fail_policy("banana", already_woken=False) is Action.IGNORE


# --------------------------------------------------------------------------- #
# @ 了别人 —— 不抢答
# --------------------------------------------------------------------------- #


def test_does_not_answer_when_addressed_to_someone_else() -> None:
    """回归测试：@ 了别人的提问，不该被当成"没人接的问题"抢答。

    实测踩过：群里 A 问 B「你这是哪个大学的」，open_question=0.95、
    addressed=0.06，命中「主动帮一把」规则，Bot 就把两人之间的对话
    当成求助插了进去。
    """
    verdict = decide(
        addressed=0.06,
        open_question=0.95,
        already_answered=0.0,
        addressed_to_other=True,
    )
    assert verdict.action is Action.IGNORE


def test_still_helps_when_no_specific_target() -> None:
    """没 @ 任何人的提问，仍然主动帮。"""
    verdict = decide(
        addressed=0.06,
        open_question=0.95,
        already_answered=0.0,
        addressed_to_other=False,
    )
    assert verdict.action is Action.RESPOND
    assert verdict.reason == "unanswered_question"


def test_addressed_to_other_does_not_block_direct_call() -> None:
    """被 @ 了就算同时 @ 了别人，也照样回 —— 用户明确叫了 Bot。"""
    verdict = decide(
        addressed=0.9,
        open_question=0.9,
        already_answered=0.0,
        already_woken=True,
        addressed_to_other=True,
    )
    assert verdict.action is Action.RESPOND
