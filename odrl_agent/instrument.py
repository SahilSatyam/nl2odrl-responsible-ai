"""AgentCrash SDK instrumentation for the conversion pipeline (Phase 2.4).

The pipeline's boundaries run inside ``AgentCrash.run()`` / ``run.step()``.
Every model call and tool call is recorded as an SDK ``tool`` event, and
``run.maybe_fault()`` / ``run.intercept_output()`` are the single place where
chaos schedules reach the workflow. Three boundaries are instrumented:

``read_source``
    The untrusted NL document is read; a ``poisoned_doc`` fault replaces it.
``llm``
    Each structured model call; a ``timeout_after_commit`` fault loses the
    reply after the model produced it, and the pipeline retries once.
``save_odrl``
    The publish write; ``concurrent_update`` lands another writer between the
    freshness read and the write, ``timeout_after_commit`` loses the write's
    acknowledgement after it committed.

The legacy :class:`agentcrash.tracer.TraceWriter` trace stays the evidence the
ODRL verifiers judge. SDK events go to a hash-chained (optionally HMAC-signed)
sidecar ``<trace>.sdk.jsonl``, the configured ``/v1`` control plane (non-
blocking exporter) and, when enabled, OpenTelemetry.

Environment (read once, when the default SDK is built):

* ``NL2ODRL_AGENTCRASH=0`` disables SDK capture entirely.
* ``AGENTCRASH_ENDPOINT`` / ``AGENTCRASH_API_KEY`` enable ``/v1`` export.
* ``AGENTCRASH_SIGNING_KEY`` HMAC-signs the chain.
* ``NL2ODRL_OTEL=1`` exports spans through ``opentelemetry`` when installed.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from agentcrash import AgentCrash, RunContext, WorkflowConfig, export_otel_spans, verify_sdk_chain

PROJECT = "nl2odrl"
WORKFLOW = "nl2odrl.convert"
BOUNDARY_SOURCE = "read_source"
BOUNDARY_LLM = "llm"
BOUNDARY_STORE = "save_odrl"
BOUNDARIES = (BOUNDARY_SOURCE, BOUNDARY_LLM, BOUNDARY_STORE)

_default: AgentCrash | None = None
_default_lock = threading.Lock()


def build_sdk(**overrides: Any) -> AgentCrash:
    """A new SDK instance configured from the environment plus ``overrides``."""
    config = WorkflowConfig(
        project=PROJECT,
        environment=os.environ.get("NL2ODRL_ENV", "development"),
        enabled=os.environ.get("NL2ODRL_AGENTCRASH", "1") != "0",
        endpoint=os.environ.get("AGENTCRASH_ENDPOINT") or None,
        api_key=os.environ.get("AGENTCRASH_API_KEY") or None,
        signing_key=os.environ.get("AGENTCRASH_SIGNING_KEY") or None,
    )
    for name, value in overrides.items():
        setattr(config, name, value)
    return AgentCrash(config)


def default_sdk() -> AgentCrash:
    """Process-wide SDK used when a caller passes none (lazy, thread-safe)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = build_sdk()
        return _default


def chaos_sdk(schedule: dict[str, dict[str, Any]], seed: int = 42) -> AgentCrash:
    """An SDK with fault injection on and ``schedule`` armed (tests, fuzz gate)."""
    sdk = build_sdk(fault_injection=True, seed=seed, endpoint=None)
    sdk.configure_faults(schedule)
    return sdk


def finish(sdk: AgentCrash, run: RunContext, trace_path: str | None) -> dict[str, Any]:
    """Persist the run's SDK events, verify their chain, release memory.

    Returns ``{"run_id", "events", "chain_ok", "sidecar", "faults"}``. The
    events are popped from ``sdk.traces`` so a long-lived server holding the
    default SDK does not grow without bound.
    """
    events = sdk.trace(run.run_id)
    with sdk._lock:  # the SDK guards ``traces`` with this lock
        sdk.traces.pop(run.run_id, None)
    sidecar = None
    if trace_path and events:
        sidecar = str(Path(trace_path).with_suffix(".sdk.jsonl"))
        with open(sidecar, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    chain = verify_sdk_chain(events, signing_key=sdk.config.signing_key) if events else {"ok": True}
    if events and os.environ.get("NL2ODRL_OTEL") == "1":
        try:
            export_otel_spans(events)
        except ImportError:
            pass
    return {
        "run_id": run.run_id,
        "events": events,
        "chain_ok": bool(chain.get("ok")),
        "sidecar": sidecar,
        "faults": list(run.faults_fired),
    }


def load_sidecar(trace_path: str) -> list[dict[str, Any]]:
    path = Path(trace_path).with_suffix(".sdk.jsonl")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
