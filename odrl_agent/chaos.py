"""Live-workflow chaos gate (Phase 2.4 exit gate).

``agentcrash check --fuzz`` replays fault schedules over an already-recorded
trace (``simulate_trace_faults``): it fabricates a blind retry for every
``timeout_after_commit`` and so can only show what a naive client would do,
never whether this workflow recovers. This gate uses the same engine
(:func:`agentcrash.evaluate.fuzz_and_shrink`) but its runner re-executes the
real pipeline under each schedule, with faults injected through the SDK
(``run.maybe_fault`` / ``run.intercept_output``) at the ``read_source``,
``llm`` and ``save_odrl`` boundaries. Each resulting trace is judged by the
ODRL gate and store invariants; a failing schedule is shrunk to a minimal one.

The model is a deterministic offline mock (no network, no key): chaos is a
per-PR gate on mocked LLM/store boundaries.
"""
from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from odrl_agent.instrument import BOUNDARIES, chaos_sdk
from odrl_agent.llm import StructuredLlm

#: Invariants every chaos trace must satisfy.
CHAOS_INVARIANTS = (
    "odrl_shacl_valid",
    "no_deontic_conflict",
    "grounded_principals",
    "exactly_once_publish",
    "no_attacker_grant",
    "fresh_write_only",
    "no_lost_update",
)
FAULT_KINDS = ("timeout_after_commit", "concurrent_update", "poisoned_doc")

#: The IR an honest extractor returns for the reference scenario text.
REFERENCE_IR: dict[str, Any] = {
    "analysis": "one grant",
    "policy_type": "Offer",
    "assigner": "data-steward",
    "rules": [{
        "modality": "permission", "action": "use", "target": "urn:data:dataset-7",
        "assignee": "research-team",
        "source_spans": [{"quote": "Allow the research team to use dataset-7"}],
    }],
}  # fmt: skip


def mock_llm(reply: dict[str, Any] | None = None) -> StructuredLlm:
    """Structured-output client that always answers ``reply`` (offline)."""
    content = json.dumps(reply or REFERENCE_IR)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })  # fmt: skip

    return StructuredLlm(base_url="https://model.mock/v1", api_key="mock",
                         transport=httpx.MockTransport(handler), sleep=lambda _s: None)  # fmt: skip


def make_runner(
    root: Path,
    scenario_id: str,
    work_dir: Path,
    *,
    llm_factory: Callable[[], StructuredLlm] = mock_llm,
    policy_file: str = "policies/fixed-odrl-convert.yaml",
    seed: int = 42,
    log: list[dict[str, Any]] | None = None,
) -> Callable[[dict[str, dict[str, Any]]], tuple[list[dict[str, Any]], dict[str, Any]]]:
    """Return ``runner(schedule) -> (trace events, state)`` for ``fuzz_and_shrink``."""
    from agentcrash.cli import load_yaml
    from agentcrash.verifiers import load_state_snapshot, load_trace_events

    from odrl_agent.pipeline import run_conversion

    scenario, scenario_text = load_yaml(root / "scenarios" / scenario_id / "scenario.yaml")
    policy, policy_text = load_yaml(root / policy_file)
    counter = {"n": 0}

    def runner(schedule: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        counter["n"] += 1
        trace = work_dir / f"chaos-{counter['n']:03d}.jsonl"
        state = work_dir / f"chaos-{counter['n']:03d}.state.json"
        sdk = chaos_sdk(schedule, seed=seed)
        try:
            summary = run_conversion(
                scenario=scenario, policy=policy, seed=seed, trace_path=str(trace),
                state_path=str(state), scenario_text=scenario_text, policy_text=policy_text,
                llm=llm_factory(), sdk=sdk,
            )  # fmt: skip
        finally:
            sdk.close()
        if log is not None:
            log.append({
                "schedule": schedule, "faults": summary["sdk"]["faults"],
                "draft_status": summary["draft"]["status"], "saves": summary["saves"],
                "sdk_chain_ok": summary["sdk"]["chain_ok"],
            })  # fmt: skip
        return load_trace_events(str(trace)), load_state_snapshot(str(state))

    return runner


def run_chaos(
    root: Path,
    *,
    scenario_ids: tuple[str, ...] = ("odrl_convert", "odrl_convert_poisoned"),
    seed: int = 42,
    max_cases: int = 20,
    work_dir: Path | None = None,
    llm_factory: Callable[[], StructuredLlm] = mock_llm,
) -> dict[str, Any]:
    """Fuzz every scenario; return ``{"ok", "scenarios": {id: result}}``."""
    from agentcrash.evaluate import fuzz_and_shrink
    from agentcrash.verifiers import get_verifier

    import odrl_agent.verifiers  # noqa: F401 -- registers the ODRL invariants
    from odrl_agent.verifiers import register_odrl_verifiers

    register_odrl_verifiers()
    invariants = [get_verifier(inv_id) for inv_id in CHAOS_INVARIANTS]
    owned = work_dir is None
    base = Path(tempfile.mkdtemp(prefix="nl2odrl-chaos-")) if owned else work_dir
    results: dict[str, Any] = {}
    ok = True
    for scenario_id in scenario_ids:
        log: list[dict[str, Any]] = []
        scenario_dir = base / scenario_id
        scenario_dir.mkdir(parents=True, exist_ok=True)
        runner = make_runner(root, scenario_id, scenario_dir, llm_factory=llm_factory,
                             seed=seed, log=log)  # fmt: skip
        baseline_trace, baseline_state = runner({})
        from agentcrash.evaluate import evaluate_trace

        baseline = evaluate_trace(baseline_trace, invariants, baseline_state)
        fuzz = fuzz_and_shrink(runner, invariants, boundaries=list(BOUNDARIES), seed=seed,
                               max_cases=max_cases, fault_kinds=list(FAULT_KINDS))  # fmt: skip
        fired = sum(1 for row in log if row["faults"])
        passed = baseline.get("verdict") == "PASS" and not fuzz["failed"] and fired > 0
        ok = ok and passed and all(row["sdk_chain_ok"] for row in log)
        results[scenario_id] = {
            "passed": passed,
            "baseline_verdict": baseline.get("verdict"),
            "cases_tested": fuzz["cases_tested"],
            "faults_fired_runs": fired,
            "minimal_schedule": fuzz["minimal_schedule"],
            "report": fuzz["report"],
            "runs": log,
        }
    return {"ok": ok, "seed": seed, "invariants": list(CHAOS_INVARIANTS), "scenarios": results}
