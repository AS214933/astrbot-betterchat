"""Jev 响应校验的测试。

这里的每一条拒绝规则都对应一个真实风险：校验一旦放松，
"服务端返回了奇怪的东西"就会变成"Bot 在群里乱说话"。
"""

from __future__ import annotations

import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from betterchat_jev import JevError, validate_noul_answers  # noqa: E402

EXPECTED = {"a"}


def ok(value: float) -> dict:
    return {"a": {"type": "noul", "noul": value}}


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #


def test_accepts_valid_answer() -> None:
    result = validate_noul_answers(ok(0.75), EXPECTED)
    assert result["a"].value == 0.75
    assert result["a"].raw == {"type": "noul", "noul": 0.75}


@pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 1.0])
def test_accepts_boundary_values(value: float) -> None:
    assert validate_noul_answers(ok(value), EXPECTED)["a"].value == float(value)


def test_accepts_missing_type_field() -> None:
    """有些网关会省略 type，此时按 noul 处理即可。"""
    result = validate_noul_answers({"a": {"noul": 0.4}}, EXPECTED)
    assert result["a"].value == 0.4


# --------------------------------------------------------------------------- #
# 键集合
# --------------------------------------------------------------------------- #


def test_rejects_missing_key() -> None:
    with pytest.raises(JevError, match="键不匹配"):
        validate_noul_answers({}, EXPECTED)


def test_rejects_extra_key() -> None:
    payload = ok(0.5)
    payload["unexpected"] = {"type": "noul", "noul": 0.5}
    with pytest.raises(JevError, match="键不匹配"):
        validate_noul_answers(payload, EXPECTED)


def test_rejects_non_mapping_answers() -> None:
    with pytest.raises(JevError, match="不是对象"):
        validate_noul_answers(["a"], EXPECTED)


def test_rejects_answer_that_is_not_mapping() -> None:
    with pytest.raises(JevError, match="答案不是对象"):
        validate_noul_answers({"a": 0.5}, EXPECTED)


# --------------------------------------------------------------------------- #
# 类型与数值
# --------------------------------------------------------------------------- #


def test_rejects_bool_masquerading_as_number() -> None:
    """``True`` 是 ``int`` 的子类，不排除就会被当成概率 1.0。"""
    with pytest.raises(JevError, match="不是数字"):
        validate_noul_answers({"a": {"type": "noul", "noul": True}}, EXPECTED)


def test_rejects_string_number() -> None:
    with pytest.raises(JevError, match="不是数字"):
        validate_noul_answers({"a": {"type": "noul", "noul": "0.9"}}, EXPECTED)


def test_rejects_none() -> None:
    with pytest.raises(JevError, match="不是数字"):
        validate_noul_answers({"a": {"type": "noul", "noul": None}}, EXPECTED)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite(value: float) -> None:
    with pytest.raises(JevError):
        validate_noul_answers({"a": {"type": "noul", "noul": value}}, EXPECTED)


@pytest.mark.parametrize("value", [-0.01, 1.01, 2, -5])
def test_rejects_out_of_range(value: float) -> None:
    with pytest.raises(JevError, match=r"超出 \[0,1\]"):
        validate_noul_answers({"a": {"type": "noul", "noul": value}}, EXPECTED)


def test_rejects_wrong_answer_type() -> None:
    with pytest.raises(JevError, match="不是 noul"):
        validate_noul_answers(
            {"a": {"type": "choice", "choice": "x", "confidence": 1.0, "probabilities": {"x": 1.0}}},
            EXPECTED,
        )


# --------------------------------------------------------------------------- #
# 多问题
# --------------------------------------------------------------------------- #


def test_validates_multiple_questions_independently() -> None:
    payload = {
        "x": {"type": "noul", "noul": 0.1},
        "y": {"type": "noul", "noul": 0.9},
    }
    result = validate_noul_answers(payload, {"x", "y"})
    assert result["x"].value == 0.1
    assert result["y"].value == 0.9


def test_one_bad_answer_rejects_whole_response() -> None:
    payload = {
        "x": {"type": "noul", "noul": 0.1},
        "y": {"type": "noul", "noul": 5.0},
    }
    with pytest.raises(JevError):
        validate_noul_answers(payload, {"x", "y"})


def test_nan_is_not_finite_by_definition() -> None:
    """守卫性断言：确认 math.isfinite 的行为符合我们的假设。"""
    assert math.isfinite(float("nan")) is False
