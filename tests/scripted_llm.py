"""Offline structured-output model for tests (httpx.MockTransport, no network)."""
from __future__ import annotations

import json
from typing import Any

import httpx

from odrl_agent.llm import StructuredLlm

USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


def scripted_transport(
    replies: list[dict[str, Any] | int | str], requests: list[dict[str, Any]] | None = None
) -> httpx.MockTransport:
    """Serve ``replies`` in order.

    A dict is returned as the JSON content of a chat completion; an int is
    returned as an HTTP status with no body (to exercise retries); a str is
    returned verbatim as message content (to exercise off-protocol replies).
    Each request body is appended to ``requests`` when given.
    """
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(json.loads(request.content.decode("utf-8")))
        if not queue:
            return httpx.Response(500, text="script exhausted")
        reply = queue.pop(0)
        if isinstance(reply, int):
            return httpx.Response(reply, text=f"status {reply}")
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": USAGE,
            },
        )

    return httpx.MockTransport(handler)


def scripted_llm(
    replies: list[dict[str, Any] | int | str], requests: list[dict[str, Any]] | None = None
) -> StructuredLlm:
    return StructuredLlm(
        base_url="https://model.test/v1",
        api_key="test-key",
        transport=scripted_transport(replies, requests),
        sleep=lambda _s: None,
    )


def rule(
    action: str = "use",
    target: str | None = "urn:data:dataset-7",
    assignee: str | None = "research-team",
    quote: str = "Allow the research team to use dataset-7",
    modality: str = "permission",
    **extra: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "modality": modality,
        "action": action,
        "source_spans": [{"quote": quote}],
    }
    if target is not None:
        out["target"] = target
    if assignee is not None:
        out["assignee"] = assignee
    out.update(extra)
    return out


def ir(*rules: dict[str, Any], **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "analysis": "one grant",
        "policy_type": "Offer",
        "assigner": "data-steward",
        "rules": list(rules),
    }
    out.update(extra)
    return out


def install(monkeypatch, replies, requests=None):
    """Route every pipeline model client built from config to a scripted model.

    Each new client gets a fresh copy of ``replies`` (so two runs in one test
    see the same script). Only the test seam ``StructuredLlm.from_config`` is
    patched; no network, no ``urlopen``.
    """
    import copy

    from odrl_agent.llm import StructuredLlm

    monkeypatch.setenv("LLAMA_API_KEY", "test-key")
    monkeypatch.setenv("LLAMA_BASE_URL", "https://model.test/v1")

    def _from_config(cls, config, **overrides):
        del cls, config, overrides
        return scripted_llm(copy.deepcopy(list(replies)), requests)

    monkeypatch.setattr(StructuredLlm, "from_config", classmethod(_from_config))


#: The benign one-grant IR the reference scenario text supports.
BASIC_IR = ir(rule())
