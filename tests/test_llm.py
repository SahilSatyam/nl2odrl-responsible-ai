"""Structured-output client: request shape, backoff, fail-fast, off-protocol."""
from __future__ import annotations

import json

import httpx
import pytest
from scripted_llm import USAGE, scripted_llm

from odrl_agent.adapter import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    LIVE_HTTP_TIMEOUT_S,
    LlmConfig,
    ModelNotConfiguredError,
    live_timeout_s,
    max_completion_tokens,
)
from odrl_agent.llm import (
    LlmError,
    LlmNotConfiguredError,
    StructuredLlm,
    TruncatedOutputError,
)

SCHEMA = {"type": "object"}


def _call(llm: StructuredLlm) -> dict:
    return llm.complete_json(model="m", messages=[{"role": "user", "content": "x"}], schema=SCHEMA)


def test_request_uses_json_schema_response_format() -> None:
    requests: list[dict] = []
    llm = scripted_llm([{"ok": 1}], requests)
    assert _call(llm) == {"ok": 1}
    (body,) = requests
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["temperature"] == 0 and body["max_tokens"] == llm.max_tokens
    assert llm.usage.as_dict()["calls"] == 1
    assert llm.usage.total_tokens == USAGE["total_tokens"]


def test_transient_statuses_back_off_then_succeed() -> None:
    sleeps: list[float] = []
    llm = scripted_llm([429, 503, 502, {"ok": 2}])
    llm.sleep = sleeps.append
    assert _call(llm) == {"ok": 2}
    assert sleeps == [0.5, 1.0, 2.0]
    assert llm.usage.attempts == 4 and llm.usage.calls == 1


def test_retry_after_header_is_honoured() -> None:
    sleeps: list[float] = []
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    llm = StructuredLlm("https://m.test/v1", "k", transport=httpx.MockTransport(handler),
                        sleep=sleeps.append)
    assert _call(llm) == {}
    assert sleeps == [3.0]


def test_persistent_transient_failure_raises_after_budget() -> None:
    llm = scripted_llm([503, 503, 503, 503])
    with pytest.raises(LlmError, match="after 4 attempts"):
        _call(llm)
    assert llm.usage.calls == 0


def test_client_error_is_never_retried() -> None:
    llm = scripted_llm([400, {"ok": 1}])
    with pytest.raises(LlmError, match="HTTP 400"):
        _call(llm)
    assert llm.usage.attempts == 1


def test_transport_error_is_retried() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"a\":1}"}}]})

    llm = StructuredLlm("https://m.test/v1", "k", transport=httpx.MockTransport(handler),
                        sleep=lambda _s: None)
    assert _call(llm) == {"a": 1}


def test_prose_around_json_is_off_protocol() -> None:
    # No brace scraping: structured output must be a bare JSON object.
    llm = scripted_llm(['Sure! {"a": 1}'])
    with pytest.raises(LlmError, match="not a JSON object"):
        _call(llm)


def test_non_object_json_rejected() -> None:
    with pytest.raises(LlmError, match="not an object"):
        _call(scripted_llm(["[1, 2]"]))


def test_truncated_output_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "{\"a\":"}, "finish_reason": "length"}]})

    llm = StructuredLlm("https://m.test/v1", "k", transport=httpx.MockTransport(handler))
    with pytest.raises(TruncatedOutputError, match="truncated"):
        _call(llm)
    assert issubclass(TruncatedOutputError, LlmError)


def test_empty_choices_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    llm = StructuredLlm("https://m.test/v1", "k", transport=httpx.MockTransport(handler))
    with pytest.raises(LlmError, match="no choices"):
        _call(llm)


def test_missing_endpoint_or_key_refuses_without_request() -> None:
    with pytest.raises(LlmNotConfiguredError):
        _call(StructuredLlm("", "k"))
    with pytest.raises(LlmNotConfiguredError):
        _call(StructuredLlm("https://m.test/v1", ""))
    assert issubclass(LlmNotConfiguredError, ModelNotConfiguredError)


def test_key_travels_in_header_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    llm = StructuredLlm("https://m.test/v1", "sk-secret", transport=httpx.MockTransport(handler))
    _call(llm)
    assert seen[0].headers["authorization"] == "Bearer sk-secret"
    assert "sk-secret" not in seen[0].content.decode() and "sk-secret" not in str(seen[0].url)


def test_from_config_maps_llm_config() -> None:
    cfg = LlmConfig(base_url="https://m.test/v1", api_key="k", timeout_s=5, max_tokens=99)
    llm = StructuredLlm.from_config(cfg)
    assert (llm.base_url, llm.api_key, llm.timeout_s, llm.max_tokens) == (
        "https://m.test/v1", "k", 5.0, 99)


def test_env_defaults_and_overrides(monkeypatch) -> None:
    monkeypatch.delenv("LLAMA_TIMEOUT_S", raising=False)
    assert live_timeout_s() == LIVE_HTTP_TIMEOUT_S
    monkeypatch.setenv("LLAMA_TIMEOUT_S", "12.5")
    assert live_timeout_s() == 12.5
    monkeypatch.setenv("LLAMA_TIMEOUT_S", "nope")
    assert live_timeout_s() == LIVE_HTTP_TIMEOUT_S
    monkeypatch.setenv("LLAMA_MAX_TOKENS", "123")
    assert max_completion_tokens() == 123
    monkeypatch.setenv("LLAMA_MAX_TOKENS", "-4")
    assert max_completion_tokens() == DEFAULT_MAX_COMPLETION_TOKENS
    assert json.loads(json.dumps(USAGE)) == USAGE
