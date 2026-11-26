"""OpenAI-compatible structured-output client (httpx).

One call returns one JSON object that matches a JSON Schema, requested with
``response_format: {"type": "json_schema", ...}``. Transient failures (429,
500, 502, 503, 504 and transport errors) back off exponentially, honouring
``Retry-After``; other HTTP errors fail fast. The client is constructed per
run from an immutable :class:`~odrl_agent.adapter.LlmConfig`, so no process
global (``os.environ``, ``urlopen``) is read or patched here.

Tests inject an ``httpx.MockTransport`` through ``transport=``.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_S = 0.5
MAX_BACKOFF_S = 8.0
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_TOKENS = 8000


class LlmError(RuntimeError):
    """The model call failed or returned something off-protocol."""

    code = "E_MODEL_API"


class LlmNotConfiguredError(LlmError):
    """No endpoint or key configured."""


class TruncatedOutputError(LlmError):
    """The model hit max_tokens mid-reply (finish_reason "length").

    Unlike other model errors this one is worth one bounded retry: the same
    input often completes on a second attempt (Baseline v1/v2 truncations
    moved between cases across runs, never stuck to one input).
    """


@dataclass
class LlmUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    attempts: int = 0

    def add(self, usage: dict[str, Any]) -> None:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                setattr(self, key, getattr(self, key) + value)

    def as_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "attempts": self.attempts,
        }


@dataclass
class StructuredLlm:
    """Minimal structured-output chat client for OpenAI-compatible endpoints."""

    base_url: str
    api_key: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_s: float = DEFAULT_BACKOFF_S
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    usage: LlmUsage = field(default_factory=LlmUsage)

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> StructuredLlm:
        """Build from an ``LlmConfig`` (duck-typed: base_url, api_key, timeout_s, max_tokens)."""
        kwargs: dict[str, Any] = {
            "base_url": getattr(config, "base_url", "") or "",
            "api_key": getattr(config, "api_key", "") or "",
            "timeout_s": float(getattr(config, "timeout_s", 0) or DEFAULT_TIMEOUT_S),
            "max_tokens": int(getattr(config, "max_tokens", 0) or DEFAULT_MAX_TOKENS),
            "transport": getattr(config, "transport", None),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            header = response.headers.get("retry-after", "").strip()
            if header:
                try:
                    return min(max(float(header), 0.0), MAX_BACKOFF_S)
                except ValueError:
                    pass
        return min(self.backoff_s * (2**attempt), MAX_BACKOFF_S)

    def complete_json(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        schema_name: str = "PolicyIR",
    ) -> dict[str, Any]:
        """Return the parsed JSON object the model produced for ``schema``."""
        if not self.base_url or not self.api_key:
            raise LlmNotConfiguredError(
                f"model {model!r} needs a base URL and API key (LLAMA_BASE_URL / LLAMA_API_KEY)"
            )
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": False},
            },
        }
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload: Any = None
        last_error = ""
        with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
            for attempt in range(self.max_attempts):
                self.usage.attempts += 1
                response: httpx.Response | None = None
                try:
                    response = client.post(url, json=body, headers=headers)
                except httpx.TransportError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                else:
                    if response.status_code < 400:
                        try:
                            payload = response.json()
                        except ValueError as exc:
                            raise LlmError(f"model {model!r} returned non-JSON body") from exc
                        break
                    detail = response.text[:300]
                    last_error = f"HTTP {response.status_code}: {detail}"
                    if response.status_code not in TRANSIENT_STATUS:
                        raise LlmError(f"model {model!r} rejected the request ({last_error})")
                if attempt + 1 < self.max_attempts:
                    self.sleep(self._retry_delay(attempt, response))
        if payload is None:
            raise LlmError(
                f"model {model!r} unavailable after {self.max_attempts} attempts ({last_error})"
            )
        self.usage.calls += 1
        if isinstance(payload.get("usage"), dict):
            self.usage.add(payload["usage"])
        try:
            choice = payload["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LlmError(f"model {model!r} response has no choices[0].message") from exc
        if choice.get("finish_reason") == "length":
            raise TruncatedOutputError(
                f"model {model!r} output truncated at max_tokens={self.max_tokens}"
            )
        try:
            value = json.loads(content)
        except ValueError as exc:
            # Structured output guarantees a bare JSON object; anything else is
            # off-protocol. No brace-scraping fallback, by design.
            raise LlmError(f"model {model!r} content is not a JSON object: {content[:200]!r}") from exc
        if not isinstance(value, dict):
            raise LlmError(f"model {model!r} returned JSON that is not an object")
        return value
