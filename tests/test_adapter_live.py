"""Live LlamaAdapter tests with a mocked transport (no network, no key)."""

import io
import json
import urllib.error
import urllib.request

import pytest

from odrl_agent.adapter import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    DEFAULT_MODEL,
    MAX_COMPLETION_ATTEMPTS,
    LlamaAdapter,
    ModelNotConfiguredError,
    max_completion_tokens,
)


def _reply(monkeypatch, content):
    """Serve one canned chat-completions payload through urlopen."""

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    def _fake_urlopen(request, timeout=None):
        assert request.full_url.endswith("/chat/completions")
        assert request.get_header("Authorization", "").startswith("Bearer ")
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["model"] == DEFAULT_MODEL
        assert payload["temperature"] == 0
        assert "response_format" not in payload
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


def test_live_timeout_s_default_and_override(monkeypatch) -> None:
    from odrl_agent.adapter import LIVE_HTTP_TIMEOUT_S, live_timeout_s

    monkeypatch.delenv("LLAMA_TIMEOUT_S", raising=False)
    assert live_timeout_s() == LIVE_HTTP_TIMEOUT_S
    monkeypatch.setenv("LLAMA_TIMEOUT_S", "180")
    assert live_timeout_s() == 180.0
    monkeypatch.setenv("LLAMA_TIMEOUT_S", "bogus")
    assert live_timeout_s() == LIVE_HTTP_TIMEOUT_S
    monkeypatch.setenv("LLAMA_TIMEOUT_S", "-5")
    assert live_timeout_s() == LIVE_HTTP_TIMEOUT_S


def _live_env(monkeypatch):
    monkeypatch.setenv("LLAMA_API_KEY", "test-key")
    monkeypatch.setenv("LLAMA_BASE_URL", "https://llama.example/v1")


def test_missing_key_or_url_refuses(monkeypatch):
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_BASE_URL", raising=False)
    with pytest.raises(ModelNotConfiguredError):
        LlamaAdapter().complete([], ["save_odrl"], 0)


def test_valid_call_action_returned(monkeypatch):
    _live_env(monkeypatch)
    _reply(
        monkeypatch,
        json.dumps(
            {
                "type": "call",
                "tool": "save_odrl",
                "args": {"op": "read", "record": "odrl-policy-1"},
            }
        ),
    )
    action = LlamaAdapter().complete([], ["save_odrl"], 0)
    assert action["type"] == "call"
    assert action["tool"] == "save_odrl"
    assert action["args"] == {"op": "read", "record": "odrl-policy-1"}


def test_done_action_returned(monkeypatch):
    _live_env(monkeypatch)
    _reply(monkeypatch, json.dumps({"type": "done", "reason": "saved once"}))
    assert LlamaAdapter().complete([], ["save_odrl"], 0)["type"] == "done"


def test_conversational_preamble_is_stripped(monkeypatch):
    _live_env(monkeypatch)
    _reply(
        monkeypatch,
        "I'll re-read the record first."
        ' {"type": "call", "tool": "save_odrl",'
        ' "args": {"op": "read", "record": "odrl-policy-1"}}',
    )
    action = LlamaAdapter().complete([], ["save_odrl"], 0)
    assert action == {
        "type": "call",
        "tool": "save_odrl",
        "args": {"op": "read", "record": "odrl-policy-1"},
    }


def test_unknown_tool_rejected(monkeypatch):
    _live_env(monkeypatch)
    _reply(
        monkeypatch,
        json.dumps({"type": "call", "tool": "charge_card", "args": {}}),
    )
    with pytest.raises(ModelNotConfiguredError):
        LlamaAdapter().complete([], ["save_odrl"], 0)


def test_non_json_content_rejected(monkeypatch):
    _live_env(monkeypatch)
    _reply(monkeypatch, "Sure, here is what I would do...")
    with pytest.raises(ModelNotConfiguredError):
        LlamaAdapter().complete([], ["save_odrl"], 0)


def test_transport_error_fails_closed(monkeypatch):
    _live_env(monkeypatch)

    def _boom(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(ModelNotConfiguredError):
        LlamaAdapter().complete([], ["save_odrl"], 0)


def test_default_model_is_live():
    assert DEFAULT_MODEL == "muse-spark-1.3"
    assert LlamaAdapter()._model == DEFAULT_MODEL


def test_max_tokens_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("LLAMA_MAX_TOKENS", raising=False)
    assert max_completion_tokens() == DEFAULT_MAX_COMPLETION_TOKENS
    monkeypatch.setenv("LLAMA_MAX_TOKENS", "500")
    assert max_completion_tokens() == 500
    monkeypatch.setenv("LLAMA_MAX_TOKENS", "bogus")
    assert max_completion_tokens() == DEFAULT_MAX_COMPLETION_TOKENS
    monkeypatch.setenv("LLAMA_MAX_TOKENS", "-3")
    assert max_completion_tokens() == DEFAULT_MAX_COMPLETION_TOKENS


def _capture_reply(monkeypatch, content, calls):
    """Serve one canned payload; record each request body; count calls."""

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    def _fake_urlopen(request, timeout=None):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


def test_request_carries_max_tokens_cap(monkeypatch) -> None:
    _live_env(monkeypatch)
    monkeypatch.delenv("LLAMA_MAX_TOKENS", raising=False)
    calls: list = []
    _capture_reply(monkeypatch, json.dumps({"type": "done", "reason": "ok"}), calls)
    assert LlamaAdapter().complete([], ["save_odrl"], 0)["type"] == "done"
    assert calls[0]["max_tokens"] == DEFAULT_MAX_COMPLETION_TOKENS
    assert calls[0]["temperature"] == 0
    assert "response_format" not in calls[0]


def test_transport_flaky_then_success(monkeypatch) -> None:
    _live_env(monkeypatch)
    calls = {"n": 0}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": '{"type": "done", "reason": "ok"}'}}]}
            ).encode("utf-8")

    def _flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("blip")
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _flaky)
    assert LlamaAdapter().complete([], ["save_odrl"], 0)["type"] == "done"
    assert calls["n"] == 2


def test_transport_persistent_failure_retries_once_then_raises(monkeypatch) -> None:
    _live_env(monkeypatch)
    calls = {"n": 0}

    def _boom(request, timeout=None):
        calls["n"] += 1
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(ModelNotConfiguredError):
        LlamaAdapter().complete([], ["save_odrl"], 0)
    assert calls["n"] == MAX_COMPLETION_ATTEMPTS


def test_truncation_hint_names_token_env(monkeypatch) -> None:
    _live_env(monkeypatch)

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {"message": {"content": "thinking out loud"}, "finish_reason": "length"}
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse())
    with pytest.raises(ModelNotConfiguredError) as exc:
        LlamaAdapter().complete([], ["save_odrl"], 0)
    assert "LLAMA_MAX_TOKENS" in str(exc.value)


def test_empty_choices_rejected_without_name_error(monkeypatch) -> None:
    _live_env(monkeypatch)

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": []}).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse())
    with pytest.raises(ModelNotConfiguredError):
        LlamaAdapter().complete([], ["save_odrl"], 0)


def test_http_error_never_retried(monkeypatch) -> None:
    _live_env(monkeypatch)
    calls = {"n": 0}

    def _reject(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b"nope"))

    monkeypatch.setattr(urllib.request, "urlopen", _reject)
    with pytest.raises(ModelNotConfiguredError):
        LlamaAdapter().complete([], ["save_odrl"], 0)
    assert calls["n"] == 1
