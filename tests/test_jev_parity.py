"""Jev 渠道归一化测试。

核心断言：**同一组问题在两个渠道下，调用方拿到的答案必须完全一致**。
这是"内置官方和 Cloudflare 两个渠道"能被信任的前提 —— 否则换渠道就会
悄悄改变 Bot 的脾气。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from typing import Any

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from betterchat_jev import (  # noqa: E402
    JevClient,
    JevConfig,
    JevError,
    NoulQuestion,
    build_group_questions,
)


OFFICIAL_CONFIG = JevConfig(
    channel="official",
    api_key="sk-test",
    api_base="https://api.typesafe.ai",
    model="jev-latest",
    timeout_seconds=2.0,
)

CF_CONFIG = JevConfig(
    channel="cloudflare",
    cf_account_id="acc123",
    cf_api_token="cf-token",
    cf_model="typesafe/jev",
    timeout_seconds=2.0,
)

QUESTIONS = {
    "should_reply": NoulQuestion(
        instructions="Is a reply needed?",
        criteria={"true": "yes", "false": "no"},
    )
}


def run(coro):
    return asyncio.run(coro)


class CapturedPost:
    """替换 ``JevClient._post``，捕获请求并返回预设响应。"""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.payload: dict | None = None
        self.url: str | None = None
        self.client: JevClient | None = None

    async def __call__(self, payload: dict, timeout: float) -> Any:
        self.payload = payload
        self.url = self.client.config.endpoint()
        return self.response


def install_fake_post(client: JevClient, response: Any) -> CapturedPost:
    captured = CapturedPost(response)
    captured.client = client

    async def fake_post(payload, timeout):  # noqa: ANN001
        return await captured(payload, timeout)

    client._post = fake_post  # type: ignore[method-assign]  # noqa: SLF001
    return captured


# --------------------------------------------------------------------------- #
# 请求形状
# --------------------------------------------------------------------------- #


def test_official_request_carries_model_in_body() -> None:
    async def scenario():
        client = JevClient(OFFICIAL_CONFIG)
        captured = install_fake_post(
            client, {"answers": {"should_reply": {"type": "noul", "noul": 0.9}}}
        )
        await client.decide(state={"msg": "hi"}, questions=QUESTIONS)
        return captured

    captured = run(scenario())
    assert captured.payload is not None
    assert captured.payload["model"] == "jev-latest"
    assert captured.payload["state"] == {"msg": "hi"}
    assert captured.payload["questions"]["should_reply"]["type"] == "noul"
    assert captured.payload["questions"]["should_reply"]["criteria"] == {
        "true": "yes",
        "false": "no",
    }
    assert captured.url == "https://api.typesafe.ai/v1/systemone"


def test_cloudflare_request_omits_model_and_uses_run_path() -> None:
    async def scenario():
        client = JevClient(CF_CONFIG)
        captured = install_fake_post(
            client,
            {
                "result": {
                    "model": "jev-1.13.0",
                    "answers": {"should_reply": {"type": "noul", "noul": 0.9}},
                },
                "success": True,
                "errors": [],
                "messages": [],
            },
        )
        await client.decide(state={"msg": "hi"}, questions=QUESTIONS)
        return captured

    captured = run(scenario())
    assert captured.payload is not None
    # Cloudflare 的模型在 URL 里，body 里不能有 model。
    assert "model" not in captured.payload
    assert captured.payload["questions"]["should_reply"]["type"] == "noul"
    assert (
        captured.url
        == "https://api.cloudflare.com/client/v4/accounts/acc123/ai/run/typesafe/jev"
    )


def test_api_base_with_full_endpoint_is_not_double_appended() -> None:
    config = JevConfig(
        channel="official",
        api_key="k",
        api_base="https://gateway.example.com/v1/systemone",
    )
    assert config.endpoint() == "https://gateway.example.com/v1/systemone"


# --------------------------------------------------------------------------- #
# 跨渠道归一化
# --------------------------------------------------------------------------- #


def test_both_channels_produce_identical_answers() -> None:
    """两个渠道的响应形状不同，但归一化后必须一模一样。"""

    async def scenario():
        official = JevClient(OFFICIAL_CONFIG)
        install_fake_post(
            official,
            {"answers": {"should_reply": {"type": "noul", "noul": 0.82}}},
        )
        cloudflare = JevClient(CF_CONFIG)
        install_fake_post(
            cloudflare,
            {
                "result": {
                    "answers": {"should_reply": {"type": "noul", "noul": 0.82}}
                },
                "success": True,
                "errors": [],
            },
        )
        return (
            await official.decide(state="x", questions=QUESTIONS),
            await cloudflare.decide(state="x", questions=QUESTIONS),
        )

    from_official, from_cf = run(scenario())
    assert from_official["should_reply"].value == 0.82
    assert from_cf["should_reply"].value == 0.82
    assert from_official["should_reply"].value == from_cf["should_reply"].value


def test_cloudflare_failure_envelope_becomes_jev_error() -> None:
    async def scenario():
        client = JevClient(CF_CONFIG)
        install_fake_post(
            client,
            {"result": None, "success": False, "errors": [{"code": 3036}]},
        )
        return await client.decide(state="x", questions=QUESTIONS)

    with pytest.raises(JevError) as excinfo:
        run(scenario())
    assert "Cloudflare" in str(excinfo.value)


def test_official_detail_error_becomes_jev_error() -> None:
    async def scenario():
        client = JevClient(OFFICIAL_CONFIG)
        install_fake_post(client, {"detail": "invalid api key"})
        return await client.decide(state="x", questions=QUESTIONS)

    with pytest.raises(JevError) as excinfo:
        run(scenario())
    assert "invalid api key" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 可用性与熔断
# --------------------------------------------------------------------------- #


def test_missing_credentials_disable_the_client() -> None:
    assert JevConfig(channel="official", api_key="").ready_missing() is not None
    assert (
        JevConfig(channel="cloudflare", cf_account_id="a").ready_missing() is not None
    )
    assert JevConfig(channel="official", api_key="k").ready_missing() is None
    assert (
        JevConfig(
            channel="cloudflare", cf_account_id="a", cf_api_token="t"
        ).ready_missing()
        is None
    )


def test_missing_credentials_raise_without_network() -> None:
    async def scenario():
        client = JevClient(JevConfig(channel="official", api_key=""))
        return await client.decide(state="x", questions=QUESTIONS)

    with pytest.raises(JevError) as excinfo:
        run(scenario())
    assert "api_key" in str(excinfo.value)


def test_breaker_opens_after_repeated_failures_and_blocks_requests() -> None:
    config = JevConfig(
        channel="official",
        api_key="k",
        breaker_failures=2,
        breaker_open_seconds=30,
    )

    async def scenario():
        client = JevClient(config)
        calls = {"n": 0}

        async def failing_post(payload, timeout):  # noqa: ANN001
            calls["n"] += 1
            raise RuntimeError("boom")

        client._post = failing_post  # type: ignore[method-assign]  # noqa: SLF001

        for _ in range(2):
            with pytest.raises(JevError):
                await client.decide(state="x", questions=QUESTIONS)

        assert client.breaker_open is True

        # 熔断期内即使 _post 正常也不该再发请求。
        async def ok_post(payload, timeout):  # noqa: ANN001
            calls["n"] += 1
            return {"answers": {"should_reply": {"type": "noul", "noul": 0.9}}}

        client._post = ok_post  # type: ignore[method-assign]  # noqa: SLF001
        with pytest.raises(JevError) as excinfo:
            await client.decide(state="x", questions=QUESTIONS)
        assert "熔断" in str(excinfo.value)
        return calls["n"]

    assert run(scenario()) == 2


def test_success_resets_breaker_counter() -> None:
    config = JevConfig(channel="official", api_key="k", breaker_failures=2)

    async def scenario():
        client = JevClient(config)
        fail = {"on": True}

        async def flaky_post(payload, timeout):  # noqa: ANN001
            if fail["on"]:
                raise RuntimeError("boom")
            return {"answers": {"should_reply": {"type": "noul", "noul": 0.5}}}

        client._post = flaky_post  # type: ignore[method-assign]  # noqa: SLF001

        with pytest.raises(JevError):
            await client.decide(state="x", questions=QUESTIONS)
        fail["on"] = False
        await client.decide(state="x", questions=QUESTIONS)
        assert client.breaker_open is False
        # 计数器已归零：再失败一次不足以熔断。
        fail["on"] = True
        with pytest.raises(JevError):
            await client.decide(state="x", questions=QUESTIONS)
        return client.breaker_open

    assert run(scenario()) is False


# --------------------------------------------------------------------------- #
# 问题模板
# --------------------------------------------------------------------------- #


def test_group_questions_are_three_nouls() -> None:
    questions = build_group_questions()
    assert set(questions) == {
        "addressed_to_bot",
        "open_question",
        "already_answered_by_bot",
    }
    for question in questions.values():
        payload = question.to_payload()
        assert payload["type"] == "noul"
        # 只允许线上支持的题型：Cloudflare 那边同义字段叫 boolean。
        assert payload["type"] in {"noul", "choice", "score"}
        assert payload["instructions"]
        assert payload["criteria"]["true"] and payload["criteria"]["false"]


def test_question_payload_omits_absent_criteria() -> None:
    payload = NoulQuestion(instructions="x").to_payload()
    assert "criteria" not in payload
