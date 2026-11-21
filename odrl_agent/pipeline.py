"""Fixed-stage conversion pipeline (replaces the ReAct loop).

Stages: ``ingest -> (conditional decompose) -> extract -> ground -> compile ->
verify -> [repair <= 2] -> draft``. The model is called only by the
extractor (extract, decompose, repair); validation, identity, persistence
and the publish decision are code. ``validate_odrl`` and ``save_odrl`` run
through :class:`~odrl_agent.tools.ToolRegistry` as deterministic function
calls, so traces keep the shape the verifiers already judge.

Repair is shrink-only: a repaired IR may drop or narrow permissions but may
not add a permission, nor remove a prohibition, obligation, duty or
constraint. A widening repair is rejected and the previous IR stands.
"""
from __future__ import annotations

import copy
import json
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from agentcrash.clock import VirtualClock
from agentcrash.ledger import Ledger
from agentcrash.tracer import TraceWriter, compute_run_id

from odrl_agent.compiler import CompileError, compile_ir
from odrl_agent.extractor import (
    DEFAULT_MODEL,
    ExtractResult,
    Extractor,
    ModelRoute,
    classify,
)
from odrl_agent.ir import PolicyIR, RuleIR
from odrl_agent.llm import LlmError, StructuredLlm
from odrl_agent.odrl import ODRL_SHAPES_ID, validate_odrl
from odrl_agent.tools import SAVE_ODRL, VALIDATE_ODRL, ToolRegistry

PIPELINE_ID = "pipeline/v1"
MAX_REPAIRS = 2
STAGES = ("ingest", "decompose", "extract", "ground", "compile", "verify", "repair", "draft")


class PipelineError(LlmError):
    """The pipeline could not run (configuration or model transport)."""

    code = "E_MODEL_API"


@dataclass
class PolicyDraft:
    """What the pipeline hands to a reviewer or the publish gate."""

    status: str  # ready | needs_review | no_rules | failed
    odrl: dict[str, Any] | None
    ir: dict[str, Any] | None
    valid: bool
    errors: list[str]
    ambiguities: list[dict[str, Any]]
    assumptions: list[dict[str, Any]]
    dropped_rules: list[dict[str, Any]] = field(default_factory=list)
    repairs: int = 0
    rejected_repairs: list[str] = field(default_factory=list)
    route: str = "SIMPLE"
    shapes: str = ODRL_SHAPES_ID

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- grounding ------------------------------------------------------------


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def locate(quote: str, source: str) -> tuple[int, int] | None:
    """Find ``quote`` in ``source`` (exact, then whitespace/case-insensitive)."""
    start = source.find(quote)
    if start >= 0:
        return start, start + len(quote)
    needle = _normalize(quote)
    if not needle:
        return None
    pattern = r"\s+".join(re.escape(part) for part in needle.split(" "))
    match = re.search(pattern, source, re.IGNORECASE)
    return (match.start(), match.end()) if match else None


def ground(ir: PolicyIR, source: str) -> tuple[PolicyIR, list[str]]:
    """Resolve span offsets; return the grounded IR and grounding violations."""
    grounded = ir.model_copy(deep=True)
    violations: list[str] = []
    for index, rule in enumerate(grounded.rules):
        if not rule.source_spans:
            violations.append(f"rules.{index}.source_spans: rule cites no source quote")
            continue
        found = False
        for span in rule.source_spans:
            where = locate(span.quote, source)
            if where is None:
                violations.append(
                    f"rules.{index}.source_spans: quote {span.quote[:80]!r} is not in the source"
                )
                continue
            span.start, span.end = where
            found = True
        if found:
            # A rule with at least one real span is grounded; drop the bad quotes.
            rule.source_spans = [s for s in rule.source_spans if s.start is not None]
            violations = [v for v in violations if not v.startswith(f"rules.{index}.")]
    return grounded, violations


def _is_grounded(rule: RuleIR) -> bool:
    return any(span.start is not None for span in rule.source_spans)


# ---- shrink-only ----------------------------------------------------------


def _key(rule: RuleIR, ir: PolicyIR) -> tuple[str, str, str, str]:
    return (
        rule.modality,
        rule.action,
        (rule.target or "").casefold(),
        (rule.assignee or ir.assignee or "").casefold(),
    )


def widening(old: PolicyIR, new: PolicyIR) -> list[str]:
    """Reasons ``new`` grants more than ``old``; empty when it only narrows."""
    reasons: list[str] = []
    old_rules = {_key(r, old): r for r in old.rules}
    new_rules = {_key(r, new): r for r in new.rules}
    for key, rule in new_rules.items():
        if rule.modality == "permission" and key not in old_rules:
            reasons.append(f"repair adds permission {key[1:]}")
    for key, rule in old_rules.items():
        if rule.modality in ("prohibition", "obligation") and key not in new_rules:
            reasons.append(f"repair removes {rule.modality} {key[1:]}")
        if rule.modality == "permission" and key in new_rules:
            after = new_rules[key]
            if len(after.constraints) < len(rule.constraints):
                reasons.append(f"repair removes constraints from permission {key[1:]}")
            if len(after.duties) < len(rule.duties):
                reasons.append(f"repair removes duties from permission {key[1:]}")
    if old.conflict_strategy == "prohibit" and new.conflict_strategy != "prohibit":
        reasons.append("repair relaxes conflict strategy from 'prohibit'")
    return reasons


# ---- orchestration --------------------------------------------------------


def _source_text(scenario: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return (doc_id, text, fixture, label) for the scenario's first NL doc."""
    from odrl_agent.adapter import load_nl_store

    store = load_nl_store(scenario)
    env = scenario.get("environment", {}) or {}
    for doc in env.get("nl_docs", []) or []:
        doc_id = str(doc.get("id", ""))
        entry = store.get(doc_id)
        if entry is not None:
            return (
                doc_id,
                str(entry.get("text", "")),
                str(entry.get("fixture", "")),
                str(entry.get("label", "")),
            )
    raise PipelineError("scenario has no natural-language document to convert")


def _record_key(scenario: dict[str, Any]) -> str:
    env = scenario.get("environment", {}) or {}
    records = env.get("odrl_records", []) or []
    return str(records[0]["key"]) if records else "odrl-policy-1"


class _Run:
    """Per-run trace + tool plumbing (keeps run_conversion readable)."""

    def __init__(self, writer: TraceWriter, registry: ToolRegistry, clock: VirtualClock) -> None:
        self.writer = writer
        self.registry = registry
        self.clock = clock
        self.step = 1
        self.tool_calls = 0

    def stage(self, name: str, **data: Any) -> None:
        self.step += 1
        self.writer.write(
            step=self.step,
            outcome="info",
            ts_virtual=self.clock.now(),
            detail={"phase": "pipeline_stage", "stage": name, **data},
        )

    def model_call(self, purpose: str, model: str, usage: dict[str, int]) -> None:
        self.step += 1
        self.writer.write(
            step=self.step,
            outcome="info",
            ts_virtual=self.clock.now(),
            detail={
                "phase": "model_call",
                "purpose": purpose,
                "model": model,
                "model_usage": usage,
            },
        )

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.step += 1
        self.tool_calls += 1
        self.writer.write(
            step=self.step,
            tool=name,
            args=args,
            outcome="info",
            ts_virtual=self.clock.now(),
            detail={"phase": "tool_call", "caller": "pipeline"},
        )
        result = self.registry.call(name, args, self.clock)
        self.writer.write(
            step=self.step,
            tool=name,
            args=args,
            outcome=str(result["outcome"]),
            latency=int(result["latency_ms"]),
            ts_virtual=self.clock.now(),
            detail={"phase": "tool_result", "error": result["error"], "data": result["data"]},
        )
        return result


def _usage_delta(llm: StructuredLlm, before: dict[str, int]) -> dict[str, int]:
    now = llm.usage.as_dict()
    return {
        key: now[key] - before.get(key, 0)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def _call(
    run: _Run, llm: StructuredLlm, purpose: str, model: str, fn: Any, *args: Any, **kw: Any
) -> Any:
    before = llm.usage.as_dict()
    try:
        return fn(*args, **kw)
    finally:
        if llm.usage.calls > before["calls"]:
            run.model_call(purpose, model, _usage_delta(llm, before))


def convert(
    text: str,
    extractor: Extractor,
    run: _Run | None = None,
    source_meta: dict[str, Any] | None = None,
) -> tuple[PolicyDraft, dict[str, Any] | None]:
    """Run the semantic stages on ``text``; return the draft and compiled doc.

    Persistence is the caller's job (``run_conversion`` saves through the
    tool registry). ``run`` is optional so the stages can be unit-tested.
    """
    llm = extractor.llm

    def stage(name: str, **data: Any) -> None:
        if run is not None:
            run.stage(name, **data)

    def call(purpose: str, model: str, fn: Any, *args: Any, **kw: Any) -> Any:
        if run is None:
            return fn(*args, **kw)
        return _call(run, llm, purpose, model, fn, *args, **kw)

    route_models = extractor.route

    source = text.strip()
    stage("ingest", chars=len(source), **(source_meta or {}))
    if not source:
        draft = PolicyDraft("failed", None, None, False, ["empty input"], [], [])
        stage("draft", status=draft.status)
        return draft, None

    route = classify(source)
    clauses: list[str] | None = None
    if route == "COMPLEX":
        clauses = call("decompose", route_models.fast, extractor.decompose, source)
        stage("decompose", route=route, clauses=len(clauses))
    else:
        stage("decompose", route=route, skipped=True)

    complex_input = route == "COMPLEX"
    result: ExtractResult = call(
        "extract",
        route_models.strong if complex_input else route_models.fast,
        extractor.extract,
        source,
        clauses,
        complex_input=complex_input,
    )
    stage("extract", schema_errors=result.errors, rules=len(result.ir.rules) if result.ir else 0)

    best: PolicyIR | None = None  # last IR that passed schema validation
    current: PolicyIR | None = result.ir
    violations = list(result.errors)
    doc: dict[str, Any] | None = None
    repairs = 0
    rejected: list[str] = []

    while True:
        if current is not None:
            if best is not None:
                widened = widening(best, current)
                if widened:
                    rejected.extend(widened)
                    stage("repair", rejected=widened)
                    current = best
                    break
            grounded, ground_violations = ground(current, source)
            stage("ground", violations=ground_violations)
            best = grounded
            violations = list(ground_violations)
            if grounded.rules:
                try:
                    doc, _graph = compile_ir(grounded)
                except CompileError as exc:
                    doc = None
                    violations.append(f"compile: {exc}")
                stage("compile", ok=doc is not None)
                if doc is not None:
                    valid, shacl_errors = validate_odrl(doc)
                    stage("verify", valid=valid, errors=shacl_errors)
                    violations.extend(f"shacl: {err}" for err in shacl_errors)
            current = grounded
        if not violations or repairs >= MAX_REPAIRS:
            break
        repairs += 1
        previous = best.model_dump(mode="json") if best is not None else result.raw
        repaired = call("repair", route_models.strong, extractor.repair, source, previous, violations)
        stage("repair", attempt=repairs, schema_errors=repaired.errors)
        if repaired.ir is None:
            violations = list(repaired.errors)
            current = None
            continue
        current = repaired.ir

    final = best
    if final is None:
        draft = PolicyDraft(
            "failed", None, result.raw, False, violations, [], [], repairs=repairs,
            rejected_repairs=rejected, route=route,
        )  # fmt: skip
        stage("draft", status=draft.status)
        return draft, None

    # Shrink-only fallback: an ungrounded or target-less permission is dropped
    # (narrows the grant); prohibitions and obligations are kept (a
    # restriction never disappears silently).
    def _droppable(rule: RuleIR) -> bool:
        return rule.modality == "permission" and (not _is_grounded(rule) or not rule.target)

    dropped = [r.model_dump(mode="json") for r in final.rules if _droppable(r)]
    if dropped:
        kept = [r for r in final.rules if not _droppable(r)]
        final = final.model_copy(update={"rules": kept})
        stage("ground", dropped_ungrounded_permissions=len(dropped))

    ir_dump = final.model_dump(mode="json")
    ambiguities = [a.model_dump(mode="json") for a in final.ambiguities]
    assumptions = [a.model_dump(mode="json") for a in final.assumptions]
    if not final.rules:
        draft = PolicyDraft(
            "no_rules", None, ir_dump, False, ["no grounded rules to compile"], ambiguities,
            assumptions, dropped, repairs, rejected, route,
        )  # fmt: skip
        stage("draft", status=draft.status)
        return draft, None

    try:
        doc, _graph = compile_ir(final)
    except CompileError as exc:
        draft = PolicyDraft(
            "failed", None, ir_dump, False, [f"compile: {exc}"], ambiguities, assumptions,
            dropped, repairs, rejected, route,
        )  # fmt: skip
        stage("draft", status=draft.status)
        return draft, None
    valid, errors = validate_odrl(doc)
    needs_review = bool(ambiguities or assumptions or dropped or rejected)
    status = "failed" if not valid else ("needs_review" if needs_review else "ready")
    draft = PolicyDraft(
        status, doc, ir_dump, valid, errors, ambiguities, assumptions, dropped, repairs,
        rejected, route,
    )  # fmt: skip
    stage("draft", status=status)
    return draft, doc if valid else None


def run_conversion(
    *,
    scenario: dict[str, Any],
    policy: dict[str, Any],
    seed: int,
    trace_path: str,
    state_path: str | None = None,
    scenario_text: str = "",
    policy_text: str = "",
    model: str = DEFAULT_MODEL,
    strong_model: str | None = None,
    llm_config: Any = None,
    llm: StructuredLlm | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Convert the scenario's NL document; write trace + state snapshot.

    Returns the same summary keys the ReAct loop returned (``run_id``,
    ``steps``, ``status``, ``saves``, ``model_calls``, ...) plus ``draft``.
    ``policy`` no longer changes conversion behaviour: validation, freshness
    and save-once are built into the stages. It still feeds ``run_id``.
    """
    del policy
    if model in ("fake", "stub"):
        raise PipelineError("offline stub/fake adapters were removed; use a live model id")
    if llm is None:
        from odrl_agent.adapter import LlmConfig

        llm = StructuredLlm.from_config(llm_config or LlmConfig.from_env())
    extractor = Extractor(llm, ModelRoute(fast=model, strong=strong_model or model))

    rng = random.Random(seed)
    clock = VirtualClock()
    ledger = Ledger()
    env = scenario.get("environment", {}) or {}
    for rec in env.get("odrl_records", []) or []:
        ledger.kv_seed(str(rec["key"]), dict(rec.get("value", {}) or {}))
    registry = ToolRegistry.from_scenario(scenario, ledger, rng)
    run_id = compute_run_id(scenario_text, policy_text, seed)
    scenario_id = str(scenario.get("scenario", "unknown"))
    doc_id, text, fixture, label = _source_text(scenario)
    ledger.record_doc(doc_id, fixture, label)
    record = _record_key(scenario)

    writer = TraceWriter(run_id, scenario_id, trace_path)
    run = _Run(writer, registry, clock)
    saves = 0
    status = "completed"
    try:
        writer.write(
            step=0,
            outcome="info",
            ts_virtual=clock.now(),
            detail={
                "phase": "run_start",
                "task": str(scenario.get("task", "")),
                "seed": seed,
                "model": model,
                "adapter": "pipeline",
                "plan": PIPELINE_ID,
                "stages": list(STAGES),
                "doc_id": doc_id,
                "untrusted_source": True,
            },
        )
        try:
            meta = {
                "doc_id": doc_id,
                "text": text,
                "bytes": len(text.encode("utf-8")),
                "source": fixture,
                "label": label,
                "untrusted_source": True,
            }
            draft, doc = convert(text, extractor, run, meta)
        except LlmError as exc:
            raise PipelineError(str(exc)) from exc
        if doc is not None:
            check = run.tool(VALIDATE_ODRL, {"odrl": copy.deepcopy(doc)})
            if check["outcome"] == "success" and check["data"].get("valid"):
                read = run.tool(SAVE_ODRL, {"op": "read", "record": record})
                version = read["data"].get("version") if read["outcome"] == "success" else None
                write = run.tool(
                    SAVE_ODRL,
                    {
                        "op": "write",
                        "record": record,
                        "value": copy.deepcopy(doc),
                        "expected_version": version,
                    },
                )
                if write["outcome"] != "error":
                    saves += 1
        if draft.status == "failed":
            status = "failed"
        snapshot = ledger.snapshot(run_id)
        writer.write(
            step=run.step,
            outcome="info",
            ts_virtual=clock.now(),
            detail={
                "phase": "run_end",
                "status": status,
                "draft_status": draft.status,
                "tool_calls": run.tool_calls,
                "saves": saves,
                "model_calls": llm.usage.calls,
                "ledger_hash": snapshot["ledger_hash"],
                "draft": draft.as_dict(),
            },
        )
        if state_path is not None:
            with open(state_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
    finally:
        writer.close()
    usage = llm.usage.as_dict()
    return {
        "run_id": run_id,
        "scenario": scenario_id,
        "steps": run.tool_calls,
        "status": status,
        "saves": saves,
        "model_calls": usage["calls"],
        "model_usage_total": {
            k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "ledger_hash": snapshot["ledger_hash"],
        "trace_path": trace_path,
        "state_path": state_path,
        "draft": draft.as_dict(),
    }
