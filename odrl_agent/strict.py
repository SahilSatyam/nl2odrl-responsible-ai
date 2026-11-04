"""Old-envelope strict validation for ODRL host traces (doc 06 S8 rules 1-5).

Host-owned: this enforces the legacy trace envelope (``event_id`` /
``prev_hash`` / ``ts_virtual`` / scenario / policy / ledger), which the
framework core no longer speaks. The core ``check`` judges the SDK
schema; hosts that still record the old envelope use this module to
validate it. Core never imports this module.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import jsonschema
from agentcrash.ledger import Ledger
from agentcrash.tracer import compute_run_id
from agentcrash.verifiers import TraceCorruptError

#: Envelope keys every legacy trace event must carry (doc 06 S5, ADR-006).
REQUIRED_EVENT_KEYS = (
    "event_id",
    "prev_hash",
    "ts_virtual",
    "run_id",
    "scenario",
    "step",
    "tool",
    "args",
    "outcome",
    "latency",
    "injector_event",
    "policy_decision",
)

__all__ = ["REQUIRED_EVENT_KEYS", "strict_validate"]


def _canonical(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)


def strict_validate(
    trace: list[dict[str, Any]],
    *,
    scenario: dict[str, Any],
    scenario_text: str,
    policy_text: str,
    policy: dict[str, Any],
    state: dict[str, Any],
    envelope_schema: dict[str, Any] | None = None,
) -> int:
    """Enforce doc 06 S8 rules 1-5; return the trace seed.

    Raises :exc:`TraceCorruptError` naming the first violation. Rule 4
    (idempotency keys on mutating retries) applies only when the policy
    requires a key; otherwise retries without a key are the (buggy but
    structurally valid) behavior the verifiers judge.
    """
    # Rule 1: envelope shape on every line.
    for event in trace:
        event_id = event.get("event_id", "line-?") if isinstance(event, dict) else "line-?"
        if envelope_schema is not None:
            try:
                jsonschema.validate(event, envelope_schema)
            except jsonschema.ValidationError as exc:
                raise TraceCorruptError(
                    f"event {event_id}: schema violation ({exc.message})"
                ) from exc
        else:
            missing = [key for key in REQUIRED_EVENT_KEYS if key not in event]
            if missing:
                raise TraceCorruptError(f"event {event_id}: missing fields {missing}")
    # Rule 2: gapless ids, prev_hash chain, monotonic ts_virtual.
    prev_hash = "GENESIS"
    prev_ts: int | None = None
    for seq, event in enumerate(trace, start=1):
        event_id = str(event.get("event_id"))
        if not event_id.endswith(f"_{seq:04d}"):
            raise TraceCorruptError(f"event {event_id}: gapless event_id broken at seq {seq}")
        if event.get("prev_hash") != prev_hash:
            raise TraceCorruptError(f"event {event_id}: prev_hash mismatch (broken chain)")
        ts = event.get("ts_virtual")
        if prev_ts is not None and ts < prev_ts:
            raise TraceCorruptError(f"event {event_id}: ts_virtual decreased")
        prev_ts = ts
        prev_hash = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
    # Rule 3: scenario matches and run_id recomputes from scenario+policy+seed.
    scenario_id = scenario.get("scenario")
    seed = scenario.get("seed", 42)
    for event in trace:
        detail = event.get("detail") or {}
        if detail.get("phase") == "run_start" and type(detail.get("seed")) is int:
            seed = detail["seed"]
            break
    expected_run_id = compute_run_id(scenario_text, policy_text, seed)
    for event in trace:
        if event.get("scenario") != scenario_id:
            raise TraceCorruptError(
                f"event {event.get('event_id')}: scenario {event.get('scenario')!r} "
                f"does not match {scenario_id!r}"
            )
        if event.get("run_id") != expected_run_id:
            raise TraceCorruptError(
                f"event {event.get('event_id')}: run_id {event.get('run_id')!r} "
                f"does not recompute (expected {expected_run_id!r} for seed {seed})"
            )
    # Rule 4: mutating retries carry a key when the policy requires it.
    keyed_tools = _keyed_tools(policy)
    if keyed_tools:
        for event in trace:
            detail = event.get("detail") or {}
            if detail.get("phase") != "tool_call" or event.get("tool") not in keyed_tools:
                continue
            key = (event.get("args") or {}).get("idempotency_key")
            if not isinstance(key, str) or not key:
                raise TraceCorruptError(
                    f"event {event.get('event_id')}: mutating retry of "
                    f"{event.get('tool')!r} at step {event.get('step')} "
                    "carries no idempotency_key (policy requires one)"
                )
    # Rule 5: final ledger_hash in trace equals the state snapshot hash.
    if state.get("schema_version") != "env_state/v1":
        raise TraceCorruptError(
            f"state schema_version must be 'env_state/v1' (got {state.get('schema_version')!r})"
        )
    if Ledger.ledger_hash(state) != state.get("ledger_hash"):
        raise TraceCorruptError("state ledger_hash does not match state contents")
    run_ends = [event for event in trace if (event.get("detail") or {}).get("phase") == "run_end"]
    if not run_ends:
        raise TraceCorruptError("trace has no run_end event carrying the final ledger_hash")
    for event in run_ends:
        if event.get("detail", {}).get("ledger_hash") != state.get("ledger_hash"):
            raise TraceCorruptError(
                f"event {event.get('event_id')}: final ledger_hash does not match "
                "the state snapshot"
            )
    return seed


def _keyed_tools(policy: dict[str, Any]) -> set[str]:
    """Return tool names whose retries must carry an idempotency key."""
    required: set[str] = set()
    rules = policy.get("rules", []) or []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("rule") not in ("on_tool_timeout", "on_mutating_retry"):
            continue
        retry = rule.get("retry") or {}
        if retry.get("require_idempotency_key") is not True:
            continue
        tool = rule.get("tool", "*")
        required.add(str(tool))
    return required
