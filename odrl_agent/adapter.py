"""Model endpoint config and scenario fixture loading for the conversion pipeline.

The ReAct runner that used to live here (``LlamaAdapter`` + ``run_react_loop``)
is retired: conversion runs through :mod:`odrl_agent.pipeline`, which calls the
model only through :class:`odrl_agent.llm.StructuredLlm`. What remains is the
immutable per-run :class:`LlmConfig` and the fixture loaders shared by the
pipeline and the test doubles. The API key travels in the Authorization
header only; it is never logged or traced.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from odrl_agent.extractor import DEFAULT_MODEL
from odrl_agent.llm import LlmError

__all__ = [
    "DEFAULT_MODEL",
    "LLAMA_KEY_ENV",
    "LLAMA_MAX_TOKENS_ENV",
    "LLAMA_STRONG_MODEL_ENV",
    "LLAMA_TIMEOUT_ENV",
    "LLAMA_URL_ENV",
    "LlmConfig",
    "ModelNotConfiguredError",
    "live_timeout_s",
    "load_doc_store",
    "load_nl_store",
    "max_completion_tokens",
]

LLAMA_KEY_ENV = "LLAMA_API_KEY"
LLAMA_URL_ENV = "LLAMA_BASE_URL"
LLAMA_TIMEOUT_ENV = "LLAMA_TIMEOUT_S"
LLAMA_MAX_TOKENS_ENV = "LLAMA_MAX_TOKENS"
LLAMA_STRONG_MODEL_ENV = "LLAMA_STRONG_MODEL"
LIVE_HTTP_TIMEOUT_S = 60
#: Default completion-token cap per model call. Reasoning models think
#: before answering, so the cap bounds runaway output, not typical spend.
DEFAULT_MAX_COMPLETION_TOKENS = 8000

#: Raised for model transport/protocol failures (``E_MODEL_API``, exit 5).
ModelNotConfiguredError = LlmError


def live_timeout_s() -> float:
    """Per-call HTTP timeout for the live model (env override in seconds)."""
    try:
        value = float(os.environ.get(LLAMA_TIMEOUT_ENV, "").strip() or 0)
    except ValueError:
        return LIVE_HTTP_TIMEOUT_S
    return value if value > 0 else LIVE_HTTP_TIMEOUT_S


def max_completion_tokens() -> int:
    """Per-call completion-token cap (env override, positive int)."""
    try:
        value = int(os.environ.get(LLAMA_MAX_TOKENS_ENV, "").strip() or 0)
    except ValueError:
        return DEFAULT_MAX_COMPLETION_TOKENS
    return value if value > 0 else DEFAULT_MAX_COMPLETION_TOKENS


@dataclass(frozen=True)
class LlmConfig:
    """Immutable per-run model endpoint config (no process-global state).

    ``transport`` is an optional ``httpx`` transport; tests and offline seed
    runs inject an ``httpx.MockTransport`` here instead of patching globals.
    """

    base_url: str = ""
    api_key: str = ""
    timeout_s: float = 0.0
    max_tokens: int = 0
    transport: Any = field(default=None, compare=False, repr=False)

    @classmethod
    def from_env(cls) -> LlmConfig:
        return cls(
            base_url=os.environ.get(LLAMA_URL_ENV, "").strip(),
            api_key=os.environ.get(LLAMA_KEY_ENV, "").strip(),
            timeout_s=live_timeout_s(),
            max_tokens=max_completion_tokens(),
        )


def load_doc_store(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the ``read_doc`` store from the fixture + scenario documents.

    The fixture file stays the single canonical home of adversarial bytes
    (doc 11 S3); the scenario only names ``fixture_key`` entries.
    """
    env = scenario.get("environment", {}) or {}
    documents = env.get("documents", []) or []
    if not documents:
        return {}
    fixture_ref = str(env.get("doc_fixture", ""))
    if not fixture_ref:
        raise ValueError("scenario declares documents but no environment.doc_fixture")
    fixture_path = Path(fixture_ref)
    if not fixture_path.is_absolute():
        fixture_path = Path.cwd() / fixture_ref
    try:
        fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"cannot read doc fixture {fixture_ref}: {exc}") from exc
    entries = fixture.get("docs", {}) or {}
    store: dict[str, dict[str, Any]] = {}
    for doc in documents:
        key = doc.get("fixture_key")
        entry = entries.get(key)
        if not isinstance(entry, dict) or "text" not in entry:
            raise ValueError(f"doc fixture {fixture_ref} has no entry {key!r}")
        store[doc["id"]] = {
            "text": str(entry["text"]),
            "label": str(entry.get("label", "")),
            "fixture": f"{fixture_ref}#{key}",
        }
    return store


def load_nl_store(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the ``parse_policy`` store from the NL fixture + scenario docs.

    Mirrors :func:`load_doc_store`: the fixture file stays the single
    canonical home of NL bytes (doc 11 S3); the scenario only names
    ``fixture_key`` entries. Each entry must carry ``naive_odrl`` (sloppy
    converter output) and ``expected_odrl`` (strict converter output).

    A doc may instead carry inline ``text`` (pasted at runtime, e.g. by
    the UI): it is stored as a ``user_supplied`` untrusted entry with
    empty candidate ODRLs, so the live model must author the ODRL
    itself from the request text. Inline docs need no fixture file.
    """
    env = scenario.get("environment", {}) or {}
    documents = env.get("nl_docs", []) or []
    if not documents:
        return {}
    needs_fixture = any(isinstance(doc, dict) and doc.get("fixture_key") for doc in documents)
    fixture_ref = str(env.get("nl_fixture", ""))
    entries: dict[str, Any] = {}
    if needs_fixture and not fixture_ref:
        raise ValueError("scenario declares nl_docs but no environment.nl_fixture")
    if needs_fixture:
        fixture_path = Path(fixture_ref)
        if not fixture_path.is_absolute():
            fixture_path = Path.cwd() / fixture_ref
        try:
            fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise ValueError(f"cannot read NL fixture {fixture_ref}: {exc}") from exc
        entries = fixture.get("docs", {}) or {}
    store: dict[str, dict[str, Any]] = {}
    for doc in documents:
        if not isinstance(doc, dict) or not doc.get("id"):
            raise ValueError("each nl_docs entry needs an id")
        if doc.get("fixture_key"):
            key = doc.get("fixture_key")
            entry = entries.get(key)
            if (
                not isinstance(entry, dict)
                or "nl" not in entry
                or "naive_odrl" not in entry
                or "expected_odrl" not in entry
            ):
                raise ValueError(f"NL fixture {fixture_ref} has no ODRL entry {key!r}")
            store[doc["id"]] = {
                "text": str(entry["nl"]),
                "label": str(entry.get("label", "")),
                "fixture": f"{fixture_ref}#{key}",
                "naive_odrl": entry["naive_odrl"],
                "expected_odrl": entry["expected_odrl"],
            }
            continue
        if isinstance(doc.get("text"), str) and doc["text"].strip():
            store[doc["id"]] = {
                "text": str(doc["text"]),
                "label": str(doc.get("label", "user_supplied")),
                "fixture": "inline:pasted",
                "naive_odrl": doc.get("naive_odrl", {}),
                "expected_odrl": doc.get("expected_odrl", {}),
            }
            continue
        raise ValueError(f"nl_docs entry {doc['id']!r} needs fixture_key or non-empty text")
    return store
