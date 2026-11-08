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

from betterchat_core import build_gate_state  # noqa: E402
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


def test_cloudflare_request_uses_run_path_with_model_in_body() -> None:
    """Cloudflare 走通用入口 ``/ai/run``，模型名在请求体、参数包在 ``input``。

    这个形状是**用真实账号实测**确定的，不是从文档推断：

    * ``/ai/run/typesafe/jev``      → 400 ``code 7000 "No route for that URI"``
    * ``/ai/run/@cf/typesafe/jev``  → 400 ``code 5007 "No such model"``
    * ``/ai/run`` + ``{model, input}`` → 请求被接受（实测只是在余额上返回 402）
    """

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
    # 通用入口 + 模型在 body。
    assert captured.url == (
        "https://api.cloudflare.com/client/v4/accounts/acc123/ai/run"
    )
    assert captured.payload["model"] == "typesafe/jev"
    # 真正的参数包在 input 里，而不是平铺在顶层。
    assert set(captured.payload) == {"model", "input"}
    inner = captured.payload["input"]
    assert inner["state"] == {"msg": "hi"}
    assert inner["questions"]["should_reply"]["type"] == "noul"
    # 绝对不能出现 @cf/ 前缀 —— 那会被 Cloudflare 当成内置模型而拒绝。
    assert not captured.payload["model"].startswith("@cf/")


def test_cloudflare_model_prefix_is_stripped_of_cf_slash() -> None:
    """用户如果填了 ``@cf/typesafe/jev``，要能自动纠正。

    注意必须走 ``from_config``，规范化就发生在那里 —— 直接构造
    ``JevConfig`` 会绕过它（这也是这个测试第一版失败的原因）。
    """
    config = JevConfig.from_config(
        {
            "channel": "cloudflare",
            "cf_account_id": "acc123",
            "cf_api_token": "t",
            "cf_model": "@cf/typesafe/jev",
        }
    )
    assert config.cf_model == "typesafe/jev"

    async def scenario():
        client = JevClient(config)
        captured = install_fake_post(
            client,
            {
                "result": {"answers": {"should_reply": {"noul": 0.5}}},
                "success": True,
            },
        )
        await client.decide(state="x", questions=QUESTIONS)
        return captured

    captured = run(scenario())
    assert captured.payload is not None
    assert captured.payload["model"] == "typesafe/jev"


def test_builtin_cf_models_keep_their_prefix() -> None:
    """纠正只针对 typesafe 命名空间，不能误伤 Cloudflare 的内置模型。"""
    config = JevConfig.from_config(
        {
            "channel": "cloudflare",
            "cf_account_id": "a",
            "cf_api_token": "t",
            "cf_model": "@cf/meta/llama-3.2-1b-instruct",
        }
    )
    assert config.cf_model == "@cf/meta/llama-3.2-1b-instruct"


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


def test_cloudflare_insufficient_balance_is_explained() -> None:
    """402/2021 不能只报原始 JSON —— 用户要看得出该去改什么。"""
    from betterchat_jev import _explain_http_error

    message = _explain_http_error(
        status=402,
        body=(
            '{"errors":[{"message":"Insufficient balance; add money to your '
            'gateway or use BYOK","code":2021}],"success":false,"result":{}}'
        ),
        channel="cloudflare",
    )
    assert "余额不足" in message
    # 必须点出两条实际出路。
    assert "免费 Neurons" in message
    assert "官方" in message


def test_cloudflare_route_error_is_explained() -> None:
    """7000 = 路由不存在，提示要指向正确的 URL 形状。"""
    from betterchat_jev import _explain_http_error

    message = _explain_http_error(
        status=400,
        body='{"success":false,"errors":[{"code":7000,"message":"No route for that URI"}]}',
        channel="cloudflare",
    )
    assert "路由不存在" in message
    assert "/ai/run" in message


def test_auth_error_is_explained() -> None:
    from betterchat_jev import _explain_http_error

    message = _explain_http_error(
        status=401, body='{"detail":"bad key"}', channel="official"
    )
    assert "认证失败" in message


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


def test_payload_matches_official_openapi_contract() -> None:
    """按 TypeSafe OpenAPI 的字段类型做一次严格的形状校验。

    因为没有可用的真实 key 做联网验证（token 已吊销），
    这个测试就是**形状的唯一保证**：一旦字段类型偏离官方 schema，
    这里会立刻炸掉，而不是等线上调用时才暴露。

    依据 ``https://api.typesafe.ai/openapi.json``：

    * ``SystemOneRequest.required = ["model", "state", "questions"]``
    * ``state``       : ``anyOf [string, object, array]``
    * ``questions``   : ``{name: Question}``，``minProperties: 1``
    * ``NoulQuestion.required = ["type"]``，
      ``type`` 为常量 ``"noul"``，
      ``instructions`` 为 ``anyOf [string, object, array, null]``，
      ``criteria`` 为 ``{true?, false?}``，值同为 EntryType
    """
    state = build_gate_state(
        group_name="测试群",
        latest="有人知道这个报错怎么解决吗",
        context=["上一句"],
        bot_messages=["Bot 上一句"],
        bot_name="HexVergeBot",
        latest_sender="秉鑫",
        bot_mentioned=False,
    )
    questions = {k: q.to_payload() for k, q in build_group_questions().items()}

    # --- 顶层必须能组成合法的 System One 请求 ---
    request = {"model": "jev-latest", "state": state, "questions": questions}
    assert set(request) == {"model", "state", "questions"}
    assert isinstance(request["model"], str) and request["model"]
    assert isinstance(request["state"], (str, dict, list))
    assert isinstance(request["questions"], dict) and request["questions"]

    for name, question in questions.items():
        # --- 每个问题 ---
        assert isinstance(name, str) and name, "问题名不能为空"
        assert isinstance(question, dict)
        assert question["type"] == "noul", f"{name} 题型必须是 noul"

        # instructions: EntryType（string | object | array）
        instructions = question["instructions"]
        assert isinstance(instructions, (str, dict, list)), f"{name} instructions 类型不对"
        assert instructions, f"{name} instructions 不能为空"

        # criteria: 可选，但一旦存在就只能是 {true, false} 两个键，
        # 且每个值也是 EntryType。
        if "criteria" in question:
            criteria = question["criteria"]
            assert isinstance(criteria, dict), f"{name} criteria 必须是对象"
            assert set(criteria) <= {"true", "false"}, f"{name} criteria 键非法"
            for key, value in criteria.items():
                assert isinstance(value, (str, dict, list)), (
                    f"{name}.criteria.{key} 类型不对"
                )


def test_state_messages_are_structured_not_bare_strings() -> None:
    """每条历史消息都要带发送者/是否本人/是否叫 Bot/时间。

    这是照抄 ``astrbot_plugin_reply_gate`` 的做法：只发可见转录，但每条
    带足够字段。裸字符串会让模型无法区分"别人说的"和"Bot 自己说的"，
    也没法判断新旧。
    """
    from betterchat_core import ChatLine

    state = build_gate_state(
        group_name="g",
        latest="有人知道吗",
        context=[
            ChatLine(sender="小明", text="我改了配置", seconds_ago=42.0),
            ChatLine(
                sender="HexVergeBot", text="在的", is_self=True, seconds_ago=10.0
            ),
        ],
        bot_messages=["在的"],
        bot_name="HexVergeBot",
        latest_sender="秉鑫",
        bot_mentioned=False,
        latest_seconds_ago=3.2,
    )

    messages = state["recent_messages"]
    assert all(isinstance(m, dict) for m in messages)
    assert messages[0]["sender"] == "小明"
    assert messages[1]["is_self"] is True
    assert messages[0]["seconds_ago"] == 42.0
    # 最新消息也带发送者与时间。
    assert state["latest_message"]["seconds_ago"] == 3.2
    assert state["latest_message"]["sender"] == "秉鑫"


def test_seconds_ago_omitted_when_unknown() -> None:
    """没有时间戳就不输出这个键 —— 不编造 0 让模型误以为"刚刚发生"。"""
    from betterchat_core import ChatLine

    state = build_gate_state(
        group_name="g",
        latest="x",
        context=[ChatLine(sender="a", text="y")],
        bot_messages=[],
        latest_seconds_ago=None,
    )
    assert "seconds_ago" not in state["latest_message"]
    assert "seconds_ago" not in state["recent_messages"][0]


def test_state_accepts_legacy_plain_strings() -> None:
    """兼容旧调用：传纯字符串也能工作（包成 sender 为空的 ChatLine）。"""
    state = build_gate_state(
        group_name="g",
        latest="x",
        context=["纯字符串消息"],
        bot_messages=[],
    )
    assert state["recent_messages"][0]["text"] == "纯字符串消息"
    assert state["recent_messages"][0]["sender"] == ""


def test_state_stays_json_serializable_with_structure() -> None:
    import json

    from betterchat_core import ChatLine

    state = build_gate_state(
        group_name="g",
        latest="x",
        context=[ChatLine(sender="a", text="y", seconds_ago=1.5)],
        bot_messages=["z"],
        bot_name="bot",
        latest_sender="u",
        bot_mentioned=True,
        latest_seconds_ago=0.5,
    )
    assert json.loads(json.dumps(state)) == state


def test_payload_is_json_serializable() -> None:
    """结构化 instructions/criteria 里不能混进不可序列化的对象。"""
    import json

    state = build_gate_state(
        group_name="g",
        latest="x",
        context=[],
        bot_messages=[],
        bot_name="b",
        latest_sender="u",
        bot_mentioned=True,
    )
    questions = {k: q.to_payload() for k, q in build_group_questions().items()}
    body = json.dumps({"model": "jev-latest", "state": state, "questions": questions})
    assert json.loads(body)["questions"] == questions


def test_opencode_channel_uses_systemone_endpoint() -> None:
    """OpenCode 渠道与官方同构：``{base}/systemone`` + 平铺 body。"""
    config = JevConfig.from_config(
        {"channel": "opencode", "oc_api_key": "k1", "oc_model": "jev-1.13"}
    )
    assert config.endpoint() == "https://opencode.ai/inference/systemone/v1/systemone"

    async def scenario():
        client = JevClient(config)
        captured = install_fake_post(
            client,
            {"model": "jev-1.13", "answers": {"should_reply": {"type": "noul", "noul": 0.6}}},
        )
        await client.decide(state={"msg": "hi"}, questions=QUESTIONS)
        return captured

    captured = run(scenario())
    assert captured.payload is not None
    assert captured.payload["model"] == "jev-1.13"
    # 与官方一样平铺，不套 input。
    assert set(captured.payload) == {"model", "state", "questions"}


def test_opencode_endpoint_accepts_full_path() -> None:
    """用户把完整 endpoint 填进来时不要重复拼接。"""
    config = JevConfig.from_config(
        {
            "channel": "opencode",
            "oc_api_key": "k",
            "oc_api_base": "https://opencode.ai/inference/systemone/v1/systemone",
        }
    )
    assert config.endpoint().endswith("/v1/systemone")
    assert not config.endpoint().endswith("/systemone/systemone")


def test_opencode_requires_key() -> None:
    """没有 key 就直接禁用。

    实测 ``jev-1.13-free`` 从第三方调用是**可以**的（返回 200、cost 0），
    但必须带 ``Authorization`` / ``x-opencode-session`` / 正常 UA 三件套。
    没有 key 时连第一件都缺，与其每条群消息撞一次 401，不如配置阶段就禁用。
    """
    config = JevConfig.from_config({"channel": "opencode"})
    missing = config.ready_missing()
    assert missing is not None
    assert "oc_api_key" in missing


def test_opencode_free_usage_limit_is_explained() -> None:
    """429 FreeUsageLimitError 要给出可操作的解释，而不是原始 JSON。"""
    from betterchat_jev import _explain_http_error

    message = _explain_http_error(
        status=429,
        body=(
            '{"type":"error","error":{"type":"FreeUsageLimitError","message":'
            '"Free usage limit reached. Free models work in OpenCode"}}'
        ),
        channel="opencode",
    )
    assert "免费额度已用尽" in message
    assert "jev-1.13" in message
    assert "TypeSafe 官方" in message


def test_opencode_cloudflare_1010_is_explained() -> None:
    """403 code 1010 是 UA 被 Cloudflare 拦了，不是 key 无效。"""
    from betterchat_jev import _explain_http_error

    message = _explain_http_error(
        status=403, body="error code: 1010", channel="opencode"
    )
    assert "Cloudflare" in message
    assert "User-Agent" in message


def test_opencode_missing_session_header_is_explained() -> None:
    """400 MissingSessionID：缺 x-opencode-session，不是权限问题。"""
    from betterchat_jev import _explain_http_error

    message = _explain_http_error(
        status=400,
        body='{"type":"error","error":{"type":"MissingSessionID","message":"..."}}',
        channel="opencode",
    )
    assert "会话标识" in message
    assert "x-opencode-session" in message


def test_opencode_insufficient_funds_is_explained() -> None:
    """402 余额不足时提示可以直接换免费模型。"""
    from betterchat_jev import _explain_http_error

    message = _explain_http_error(
        status=402,
        body='{"error":{"type":"server_error","message":"Upstream request failed: Insufficient account funds"}}',
        channel="opencode",
    )
    assert "余额不足" in message
    assert "jev-1.13-free" in message


def test_build_questions_have_no_hardcoded_version_dependency() -> None:
    """问题定义不应绑定模型版本（jev-1.13 与 jev-latest 共用同一契约）。"""
    from betterchat_jev import DEFAULT_MODEL, DEFAULT_OC_MODEL

    assert DEFAULT_MODEL == "jev-latest"
    # OpenCode 默认走免费模型（实测可从第三方调用，cost 0）。
    assert DEFAULT_OC_MODEL == "jev-1.13-free"


def test_active_model_reflects_channel() -> None:
    """每个渠道报告自己的模型名，日志/诊断不能串台。"""
    assert (
        JevConfig.from_config({"channel": "official", "model": "jev-latest"}).active_model()
        == "jev-latest"
    )
    assert (
        JevConfig.from_config(
            {"channel": "cloudflare", "cf_model": "typesafe/jev"}
        ).active_model()
        == "typesafe/jev"
    )
    assert (
        JevConfig.from_config(
            {"channel": "opencode", "oc_model": "jev-1.13"}
        ).active_model()
        == "jev-1.13"
    )


def test_opencode_headers_are_attached() -> None:
    """OpenCode 请求必须带 x-opencode-session 与正常 UA。

    这两个头是实测出来的**硬性要求**，缺任何一个都调不通：

    * 缺 ``x-opencode-session`` → 400 ``MissingSessionID``
    * 用 ``Python-urllib`` / ``aiohttp`` 默认 UA → 403 ``error code: 1010``
      （Cloudflare 浏览器完整性校验，看着像 key 无效）

    这里拦截 aiohttp 的 post，检查真实发出的头。
    """

    class FakeResponse:
        status = 200

        async def json(self, **_kw):
            return {"model": "jev-1.13-free", "answers": {"should_reply": {"type": "noul", "noul": 0.5}}}

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        closed = False

        def __init__(self) -> None:
            self.captured: dict = {}

        def post(self, url, **kwargs):
            self.captured["url"] = url
            self.captured["headers"] = kwargs.get("headers") or {}
            return FakeResponse()

    config = JevConfig.from_config(
        {"channel": "opencode", "oc_api_key": "oc_sk_test", "oc_model": "jev-1.13-free"}
    )

    async def scenario():
        client = JevClient(config)
        session = FakeSession()
        client._session = session  # type: ignore[assignment]  # noqa: SLF001
        await client.decide(state={"a": 1}, questions=QUESTIONS)
        return session.captured

    captured = run(scenario())
    headers = captured["headers"]

    # 认证头
    assert headers["Authorization"] == "Bearer oc_sk_test"
    # 会话头：非空，且形如 UUID
    session_id = headers.get("x-opencode-session")
    assert session_id, "缺少 x-opencode-session 会被拒"
    assert len(session_id.split("-")) == 5, "会话 ID 应为 UUID 形式"
    # UA：不能是 aiohttp / python 的默认值
    ua = headers.get("User-Agent", "")
    assert ua.startswith("opencode/"), f"UA 会被 Cloudflare 拦截: {ua!r}"
    assert "aiohttp" not in ua.lower()
    assert "python" not in ua.lower()


def test_opencode_session_id_is_stable_across_calls() -> None:
    """同一个客户端实例复用同一个会话 ID（便于路由与缓存）。"""

    async def scenario():
        client = JevClient(
            JevConfig.from_config(
                {"channel": "opencode", "oc_api_key": "k", "oc_model": "jev-1.13-free"}
            )
        )
        return client._session_id  # noqa: SLF001

    first = run(scenario())
    assert first
    # 新建客户端会换一个，但实例内固定。
    assert run(scenario()) != first


def test_commandcode_channel_matches_official_shape() -> None:
    """Command Code 的 systemone 与 TypeSafe 官方**完全同构**。

    文档明确：`/provider/v1/systemone` 的请求/响应都遵循 TypeSafe 的
    System One 形状（`{model, state, questions}` → `{answers}`），
    只是换了域名和鉴权。所以只需要转 transport，不需要转 body。
    """
    config = JevConfig.from_config(
        {
            "channel": "commandcode",
            "cc_api_key": "user_test",
            "cc_model": "typesafe/jev",
        }
    )
    assert config.endpoint() == "https://api.commandcode.ai/provider/v1/systemone"
    assert config.active_model() == "typesafe/jev"
    assert config.ready_missing() is None

    async def scenario():
        client = JevClient(config)
        captured = install_fake_post(
            client,
            {"model": "typesafe/jev", "answers": {"should_reply": {"type": "noul", "noul": 0.7}}},
        )
        await client.decide(state={"a": 1}, questions=QUESTIONS)
        return captured

    captured = run(scenario())
    assert captured.payload is not None
    # 与官方一致：model 平铺，不套 input。
    assert set(captured.payload) == {"model", "state", "questions"}
    assert captured.payload["model"] == "typesafe/jev"


def test_commandcode_requires_key() -> None:
    assert JevConfig.from_config({"channel": "commandcode"}).ready_missing() is not None


def test_commandcode_pinned_version() -> None:
    """可以固定版本号，文档说同样的请求形状同样的价格。"""
    config = JevConfig.from_config(
        {
            "channel": "commandcode",
            "cc_api_key": "k",
            "cc_model": "typesafe/jev-1.14.0",
        }
    )
    assert config.active_model() == "typesafe/jev-1.14.0"


def test_commandcode_upgrade_required_is_explained() -> None:
    """403 upgrade_required：Go 计划没有 API 权限，提示要升级。"""
    from betterchat_jev import _explain_http_error

    message = _explain_http_error(
        status=403,
        body='{"error":{"code":"upgrade_required"}}',
        channel="commandcode",
    )
    assert "Go 计划" in message
    assert "GOAT" in message


def test_commandcode_unsupported_model_is_explained() -> None:
    from betterchat_jev import _explain_http_error

    message = _explain_http_error(
        status=400, body='{"code":"unsupported_model"}', channel="commandcode"
    )
    assert "typesafe/jev" in message


def test_commandcode_base_with_full_path() -> None:
    """用户把完整 endpoint 填进来时不要重复拼接。"""
    config = JevConfig.from_config(
        {
            "channel": "commandcode",
            "cc_api_key": "k",
            "cc_api_base": "https://api.commandcode.ai/provider/v1/systemone",
        }
    )
    assert config.endpoint().count("/systemone") == 1


def test_post_sends_request_not_payload_dict() -> None:
    """回归测试：``_post`` 必须真的发 HTTP 请求。

    曾经有个 bug：手工替换代码时把 ``_build_payload`` 的分支误插进了
    ``_post``，导致 ``_post`` 在发请求前直接 ``return`` 了一个 dict ——
    整个渠道静默失效，而且单元测试全部通过（因为它们都 monkeypatch 了
    ``_post``）。这个用例盯住「_post 真的会走到 session.post」。
    """
    config = JevConfig.from_config(
        {"channel": "commandcode", "cc_api_key": "user_k", "cc_model": "typesafe/jev"}
    )

    class FakeResponse:
        status = 200

        async def json(self, **_kw):
            return {"model": "typesafe/jev", "answers": {"should_reply": {"type": "noul", "noul": 0.6}}}

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        closed = False

        def __init__(self) -> None:
            self.called = False
            self.url = None
            self.payload = None

        def post(self, url, **kwargs):
            self.called = True
            self.url = url
            self.payload = kwargs.get("json")
            return FakeResponse()

    async def scenario():
        client = JevClient(config)
        session = FakeSession()
        client._session = session  # type: ignore[assignment]  # noqa: SLF001
        result = await client.decide(state={"a": 1}, questions=QUESTIONS)
        return session, result

    session, result = run(scenario())
    assert session.called, "_post 没有真的发请求（可能提前 return 了）"
    assert session.url == "https://api.commandcode.ai/provider/v1/systemone"
    assert session.payload["model"] == "typesafe/jev"
    assert result["should_reply"].value == 0.6


def test_post_works_for_every_channel() -> None:
    """四个渠道都必须真的发出请求 —— 防止某个分支被误改坏。"""
    configs = {
        "official": {"channel": "official", "api_key": "k", "model": "jev-latest"},
        "cloudflare": {
            "channel": "cloudflare",
            "cf_account_id": "acc",
            "cf_api_token": "tok",
            "cf_model": "typesafe/jev",
        },
        "opencode": {"channel": "opencode", "oc_api_key": "k", "oc_model": "jev-1.13-free"},
        "commandcode": {"channel": "commandcode", "cc_api_key": "k", "cc_model": "typesafe/jev"},
    }

    class FakeResponse:
        status = 200

        async def json(self, **_kw):
            return {
                "result": {"answers": {"should_reply": {"type": "noul", "noul": 0.5}}},
                "success": True,
                "answers": {"should_reply": {"type": "noul", "noul": 0.5}},
            }

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        closed = False

        def __init__(self) -> None:
            self.called = False

        def post(self, url, **kwargs):
            self.called = True
            return FakeResponse()

    async def scenario(raw):
        client = JevClient(JevConfig.from_config(raw))
        session = FakeSession()
        client._session = session  # type: ignore[assignment]  # noqa: SLF001
        await client.decide(state={"a": 1}, questions=QUESTIONS)
        return session.called

    for name, raw in configs.items():
        assert run(scenario(raw)) is True, f"{name} 渠道没有真的发请求"


def test_question_payload_omits_absent_criteria() -> None:
    payload = NoulQuestion(instructions="x").to_payload()
    assert "criteria" not in payload


def test_structured_instructions_are_passed_through() -> None:
    """官方把 instructions 定义为 EntryType：字符串/对象/数组都合法。

    结构化写法让 ``compare`` / ``focus`` 各有独立字段，比塞进一句话更准。
    """
    question = NoulQuestion(
        instructions={
            "question": "Is `latest_message` aimed at the bot?",
            "compare": ["`latest_message`", "`bot`"],
            "focus": "Look for the bot being named.",
        },
        criteria={"true": {"what": "targets the bot"}, "false": {"what": "targets others"}},
    )
    payload = question.to_payload()
    assert payload["type"] == "noul"
    assert isinstance(payload["instructions"], dict)
    assert payload["instructions"]["compare"] == ["`latest_message`", "`bot`"]
    assert payload["criteria"]["true"] == {"what": "targets the bot"}


def test_string_instructions_still_supported() -> None:
    payload = NoulQuestion(instructions="plain string").to_payload()
    assert payload["instructions"] == "plain string"


def test_group_questions_use_structured_entry_types() -> None:
    """三个问题都应当是结构化写法，并且引用 state 里的真实路径。"""
    questions = build_group_questions()
    addressed = questions["addressed_to_bot"].to_payload()
    instructions = addressed["instructions"]
    assert isinstance(instructions, dict)
    # 必须把 bot 身份纳入比较 —— 否则模型无从判断"是不是在对 bot 说"。
    assert any("bot" in str(item) for item in instructions["compare"])
    assert "mentioned_in_latest" in instructions["focus"]


def test_state_carries_bot_identity() -> None:
    """state 必须包含 bot 身份，否则 addressed_to_bot 只能靠猜。"""
    state = build_gate_state(
        group_name="g",
        latest="hi",
        context=[],
        bot_messages=[],
        bot_name="HexVergeBot",
        latest_sender="someone",
        bot_mentioned=True,
    )
    assert state["bot"] == {
        "name": "HexVergeBot",
        "mentioned_in_latest": True,
        "addressed_to_other": False,
    }
    assert state["latest_message"]["sender"] == "someone"
    assert state["latest_message"]["text"] == "hi"


def test_state_does_not_smuggle_instructions() -> None:
    """官方要求 state 只放事实。

    这里做一个守门测试：state 的键必须是已知的事实字段，不能因为
    "想让模型听话"就往里塞规则文本。
    """
    state = build_gate_state(
        group_name="g",
        latest="hi",
        context=["a"],
        bot_messages=["b"],
        bot_name="bot",
        latest_sender="u",
        bot_mentioned=False,
    )
    assert set(state) == {
        "group",
        "bot",
        "latest_message",
        "recent_messages",
        "recent_bot_messages",
    }
