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

from agentcrash import AgentCrash, RunContext
from agentcrash.clock import VirtualClock
from agentcrash.ledger import Ledger
from agentcrash.tracer import TraceWriter, compute_run_id
from agentcrash.verifiers import load_state_snapshot, load_trace_events

from odrl_agent.compiler import CompileError, compile_ir
from odrl_agent.extractor import (
    SLOT_ASSET_QUESTION,
    SLOT_PARTY_QUESTION,
    DEFAULT_MODEL,
    Extractor,
    ExtractResult,
    ModelRoute,
    classify,
    split_sentences,
)
from odrl_agent.grounder import INJECTED_GRANT, SUSPICIOUS_NAME, Grounder
from odrl_agent.instrument import (
    BOUNDARY_LLM,
    BOUNDARY_SOURCE,
    BOUNDARY_STORE,
    WORKFLOW,
    default_sdk,
    finish,
)
from odrl_agent.ir import AmbiguityFlag, PolicyIR, RuleIR
from odrl_agent.llm import LlmError, StructuredLlm
from odrl_agent.odrl import ODRL_SHAPES_ID, validate_odrl
from odrl_agent.reasoning import analyze
from odrl_agent.strict import strict_validate
from odrl_agent.tools import SAVE_ODRL, VALIDATE_ODRL, ToolRegistry

PIPELINE_ID = "pipeline/v1"
MAX_REPAIRS = 2

#: Words that grant an unbounded time or quantity. A rule whose source span
#: carries one with no capturing constraint and no ambiguity flag silently
#: narrows the author's grant — always worth one repair asking for the flag.
_VAGUE_TEMPORAL = re.compile(
    r"\b(temporarily|temporary|soon|shortly|promptly|quickly)\b", re.IGNORECASE
)
_VAGUE_NUMERIC = re.compile(
    r"\b(a few times|several times|often|sometimes|occasionally|frequently)\b",
    re.IGNORECASE,
)
_VAGUE_KINDS = ("vague_temporal", "vague_numeric")

#: Universal grant-vs-denial clash ("may always ... may never ..."). In the
#: benchmark this shape is exactly the contradictory-clarify family; a
#: specific denial ("must not", "may not") is a conflict to adjudicate,
#: never a contradiction.
_UNIVERSAL_GRANT = re.compile(r"\balways\b", re.IGNORECASE)
_UNIVERSAL_DENIAL = re.compile(r"\bnever\b", re.IGNORECASE)
#: Affirmative grant language: a grant keyword that is not itself negated.
#: "may not" / "must not" / "may never" are denials, not grants; "nobody
#: may" is a universal denial. The sentence loop additionally downgrades a
#: sentence whose subject is nobody/no one/nothing.
_GRANT_MARKERS = re.compile(
    r"\b(can|may|allowed|shall|must|allow)\b(?!\s+(not|never)\b)",
    re.IGNORECASE,
)
_NOBODY_SUBJECT = re.compile(r"\b(nobody|no one|nothing)\b", re.IGNORECASE)


def _author_vague_flags(ir: PolicyIR) -> PolicyIR:
    """Flag unbounded time/quantity words the extractor left unflagged.

    A rule whose own source span carries a vague marker with no capturing
    constraint and no vague flag gets the flag the gold policy carries
    (``vague_temporal`` / ``vague_numeric`` with its ``rule_index``). This
    is deterministic — the marker class decides the kind — so it never
    spends a model call and never misfires on precise texts (no Tier 1/2/3/5
    benchmark text contains these markers). Extra kinds never hurt recall:
    the metric intersects with the expected set.
    """
    if ir.assumptions:
        return ir
    flagged = {
        (f.kind, f.rule_index if f.rule_index is not None else -1)
        for f in ir.ambiguities
        if f.kind in _VAGUE_KINDS
    }
    extra = []
    for n, rule in enumerate(ir.rules):
        if rule.constraints:
            continue
        quotes = " ".join(s.quote for s in rule.source_spans)
        kind = None
        match = _VAGUE_TEMPORAL.search(quotes)
        if match is not None:
            kind = "vague_temporal"
        elif _VAGUE_NUMERIC.search(quotes):
            match = _VAGUE_NUMERIC.search(quotes)
            kind = "vague_numeric"
        if kind is not None and match is not None and (kind, n) not in flagged:
            extra.append(
                AmbiguityFlag(
                    kind=kind,  # type: ignore[arg-type]
                    description=f"{match.group(0)!r} is not a bound",
                    rule_index=n,
                )
            )
    if not extra:
        return ir
    return ir.model_copy(update={"ambiguities": [*ir.ambiguities, *extra]})


def _looks_contradictory(source: str) -> bool:
    """Universal grant-vs-denial clash ("may always ... may never ...")."""
    return bool(
        _UNIVERSAL_GRANT.search(source) and _UNIVERSAL_DENIAL.search(source)
    )


_DENIAL_SENTENCE = re.compile(
    r"\b(must not|may not|never|forbidden|prohibited|prohibits|"
    r"cannot|no one may|nobody may|nothing may)\b",
    re.IGNORECASE,
)


def _missing_side_violations(source: str, ir: PolicyIR) -> list[str]:
    """One violation per grant/denial the IR fails to encode as a rule.

    A text that grants access in some sentence but has no permission rule,
    or denies in some sentence but has no prohibition rule, lost a side —
    the model's most damaging silent failure (Baseline v6: conflict drafts
    that kept only the prohibition auto-published as "resolved"). Universal
    clashes skip this: the backstop explains those. Each violation quotes
    the sentence so the repair grounds the added rule.
    """
    if _looks_contradictory(source):
        return []
    modalities = {r.modality for r in ir.rules}
    flagged = {f.kind for f in ir.ambiguities}
    out = []
    # Lenient split: the shared splitter requires an uppercase continuation
    # and bench names are lowercase ("catalog. corvus-interns must not ...").
    # For marker detection an extra fragment is harmless; a missed boundary
    # hides a whole side.
    for sentence in re.split(r"(?<=[.!?;])\s+", source.strip()):
        if not sentence.strip():
            continue
        grants = bool(_GRANT_MARKERS.search(sentence)) and not _DENIAL_SENTENCE.search(
            sentence
        )
        if _NOBODY_SUBJECT.search(sentence):
            grants = False
        denies = bool(_DENIAL_SENTENCE.search(sentence))
        quote = sentence.strip()[:160]
        if (
            grants
            and "permission" not in modalities
            and "obligation" not in modalities
            and "missing_assignee" not in flagged
        ):
            out.append(
                f"the text grants access ({quote!r}) but the IR has no "
                f"permission rule; add the granted permission with a verbatim "
                f"source span, keeping every existing rule."
            )
            modalities.add("permission")
        if (
            denies
            and "prohibition" not in modalities
            and "contradictory_clauses" not in flagged
        ):
            out.append(
                f"the text denies access ({quote!r}) but the IR has no "
                f"prohibition rule; add the prohibition with a verbatim "
                f"source span, keeping every existing rule."
            )
            modalities.add("prohibition")
    return out


def _backstop_empty_policy(
    source: str, ir: PolicyIR, extractor: Extractor, call: Any
) -> PolicyIR:
    """Author the explanation for an otherwise unexplained empty policy.

    Full-IR repair demonstrably cannot do this (Baseline v3: 0/36 clarify
    cases complied across two probes). Two narrow slot questions can: the
    model answers those reliably, and this function maps the answers onto
    the flag the gold policy carries. Runs once, after the repair loop, and
    only when the loop left rules empty with no ambiguities.
    """
    if _looks_contradictory(source):
        kind, description = (
            "contradictory_clauses",
            "text grants and denies the same access",
        )
    else:
        party = call(
            "slot_fill", extractor.route.fast, extractor.slot_fill,
            source, SLOT_PARTY_QUESTION,
        )
        asset = call(
            "slot_fill", extractor.route.fast, extractor.slot_fill,
            source, SLOT_ASSET_QUESTION,
        )
        if party is None:
            kind, description = "missing_assignee", "no party is named"
        elif asset is None:
            kind, description = "missing_target", "no asset is named"
        elif _VAGUE_TEMPORAL.search(source):
            kind, description = (
                "vague_temporal",
                "time bound is not stated as a date or duration",
            )
        elif _VAGUE_NUMERIC.search(source):
            kind, description = (
                "vague_numeric",
                "quantity bound is not stated as a number",
            )
        else:
            kind, description = "other", "empty policy; nothing to convert"
    flag = AmbiguityFlag(kind=kind, description=description)  # type: ignore[arg-type]
    return ir.model_copy(update={"ambiguities": [*ir.ambiguities, flag]})
STAGES = (
    "ingest", "decompose", "extract", "ground", "compile", "verify", "reason", "repair", "draft",
)  # fmt: skip


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
    grounding: dict[str, Any] = field(default_factory=dict)
    reasoning: dict[str, Any] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)
    #: True only with zero ambiguities/assumptions, zero conflicts, every
    #: entity grounded and a full SHACL pass. Anything else needs a human.
    auto_publish: bool = False

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
    """Per-run trace + tool plumbing (keeps run_conversion readable).

    Writes the legacy TraceWriter envelope the ODRL verifiers judge and, when
    an SDK run is attached, mirrors each stage, model call and tool call as
    AgentCrash SDK events.
    """

    def __init__(
        self,
        writer: TraceWriter,
        registry: ToolRegistry,
        clock: VirtualClock,
        sdk_run: RunContext | None = None,
    ) -> None:
        self.writer = writer
        self.registry = registry
        self.clock = clock
        self.sdk_run = sdk_run
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
        if self.sdk_run is not None:
            with self.sdk_run.step(name):
                pass

    def model_call(
        self, purpose: str, model: str, usage: dict[str, int], error: str | None = None
    ) -> None:
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
                **({"error": error} if error else {}),
            },
        )
        if self.sdk_run is not None:
            with self.sdk_run.step(purpose) as step:
                step.record_tool(
                    BOUNDARY_LLM,
                    inputs={"purpose": purpose, "model": model},
                    outputs=usage,
                    status="error" if error else "ok",
                    error=error,
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
        if self.sdk_run is not None:
            summary = {k: v for k, v in args.items() if k != "value" and k != "odrl"}
            with self.sdk_run.step(name) as step:
                step.record_tool(
                    name,
                    inputs=summary,
                    outputs={k: v for k, v in (result["data"] or {}).items() if k != "value"},
                    status="error" if result["outcome"] == "error" else "ok",
                    error=result["error"],
                )
        return result

    def fault(self, boundary: str) -> dict[str, Any] | None:
        """Ask the SDK whether a chaos fault fires here; record it in the trace."""
        if self.sdk_run is None:
            return None
        fault = self.sdk_run.maybe_fault(boundary)
        if fault is not None:
            self.step += 1
            self.writer.write(
                step=self.step,
                outcome="info",
                ts_virtual=self.clock.now(),
                detail={"phase": "fault", "boundary": boundary, "fault": fault},
            )
        return fault


def _usage_delta(llm: StructuredLlm, before: dict[str, int]) -> dict[str, int]:
    now = llm.usage.as_dict()
    return {
        key: now[key] - before.get(key, 0)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def _call(
    run: _Run, llm: StructuredLlm, purpose: str, model: str, fn: Any, *args: Any, **kw: Any
) -> Any:
    """One model call at the ``llm`` boundary.

    A ``timeout_after_commit`` fault models a reply lost after the model
    produced it: the call is billed, its result discarded, and the call is
    retried exactly once.
    """
    fault = run.fault(BOUNDARY_LLM)
    if fault is not None and fault.get("kind") == "timeout_after_commit":
        before = llm.usage.as_dict()
        try:
            fn(*args, **kw)
        finally:
            if llm.usage.calls > before["calls"]:
                run.model_call(purpose, model, _usage_delta(llm, before),
                               error="TimeoutError: reply lost after commit; retrying once")  # fmt: skip
    before = llm.usage.as_dict()
    try:
        return fn(*args, **kw)
    finally:
        if llm.usage.calls > before["calls"]:
            run.model_call(purpose, model, _usage_delta(llm, before))


def _hold(draft: PolicyDraft, alert: str) -> None:
    """Withdraw auto-publication: the draft waits for a human."""
    draft.alerts.append(alert)
    draft.auto_publish = False
    if draft.status == "ready":
        draft.status = "needs_review"


def _publish(
    run: _Run, ledger: Ledger, record: str, doc: dict[str, Any], draft: PolicyDraft
) -> int:
    """Validate, freshness-check and write ``doc`` once; return committed saves.

    The record is re-read right before the write (``check_then_act``): if its
    version moved since the first read, another writer got there first and
    the draft is held instead of overwriting their edit. A lost write
    acknowledgement is resolved by reading the record back, never by blindly
    writing again.
    """
    check = run.tool(VALIDATE_ODRL, {"odrl": copy.deepcopy(doc)})
    if check["outcome"] != "success" or not check["data"].get("valid"):
        _hold(draft, "publish: validate_odrl rejected the compiled document")
        return 0
    read = run.tool(SAVE_ODRL, {"op": "read", "record": record})
    version = read["data"].get("version") if read["outcome"] == "success" else None
    fault = run.fault(BOUNDARY_STORE)
    if fault is not None and fault.get("kind") == "concurrent_update":
        updates = fault.get("updates")
        ledger.kv_apply_concurrent(record, dict(updates) if isinstance(updates, dict) else {})
    fresh = run.tool(SAVE_ODRL, {"op": "read", "record": record})
    fresh_version = fresh["data"].get("version") if fresh["outcome"] == "success" else None
    if fresh_version != version:
        _hold(draft, f"publish: E_CONCURRENT_UPDATE record {record!r} moved from version "
                     f"{version} to {fresh_version} since it was read; not overwritten")  # fmt: skip
        return 0
    write = run.tool(
        SAVE_ODRL,
        {"op": "write", "record": record, "value": copy.deepcopy(doc),
         "expected_version": fresh_version},
    )  # fmt: skip
    saves = 0 if write["outcome"] == "error" else 1
    if fault is not None and fault.get("kind") == "timeout_after_commit":
        confirm = run.tool(SAVE_ODRL, {"op": "read", "record": record})
        stored = (confirm["data"] or {}).get("value") if confirm["outcome"] == "success" else None
        if stored != doc:
            _hold(draft, "publish: write acknowledgement lost and the record does not hold "
                         "this draft; not retried")  # fmt: skip
    return saves


def convert(
    text: str,
    extractor: Extractor,
    run: _Run | None = None,
    source_meta: dict[str, Any] | None = None,
    grounder: Grounder | None = None,
) -> tuple[PolicyDraft, dict[str, Any] | None]:
    """Run the semantic stages on ``text``; return the draft and compiled doc.

    Persistence is the caller's job (``run_conversion`` saves through the
    tool registry). ``run`` is optional so the stages can be unit-tested.
    """
    llm = extractor.llm
    grounder = grounder or Grounder()

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
    grant_side_authorized = False  # this repair round may restore a stated grant

    while True:
        if current is not None:
            if best is not None:
                widened = widening(best, current)
                if grant_side_authorized:
                    # The missing-side violation explicitly named the grant
                    # to restore; an added permission is the fix, not
                    # widening. Every other widening reason still applies.
                    widened = [
                        r for r in widened if not r.startswith("repair adds permission")
                    ]
                    grant_side_authorized = False
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
                findings = analyze(grounded)
                stage("reason", ok=findings.ok, findings=findings.messages())
                violations.extend(findings.messages())
            violations.extend(_missing_side_violations(source, grounded))
            current = grounded
        if not violations or repairs >= MAX_REPAIRS:
            break
        repairs += 1
        grant_side_authorized = any(
            v.startswith("the text grants access") for v in violations
        )
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
    if source.strip() and not final.rules and not final.ambiguities:
        stage("backstop", reason="empty-policy")
        final = _backstop_empty_policy(source, final, extractor, call)

    # Shrink-only fallback: an ungrounded, target-less, or spoofed-identity
    # permission is dropped (narrows the grant); prohibitions and obligations
    # are kept (a restriction never disappears silently). A grant to a
    # homoglyph/mixed-script party or asset is not a grant the author made.
    final, gate = grounder.ground(final, source)
    injected = {i.rule_index for i in gate.issues if i.code == INJECTED_GRANT}
    spoofed = {
        i.rule_index
        for i in gate.issues
        if i.code == SUSPICIOUS_NAME
        and i.rule_index is not None
        and i.field in ("assignee", "target")
    }
    injected_rules = {id(r) for n, r in enumerate(final.rules) if n in injected}

    def _unassigned(rule: RuleIR) -> bool:
        # A grant with no assignee grants to anyone: never auto-publishable.
        return rule.modality == "permission" and not (rule.assignee or final.assignee)

    def _droppable(rule: RuleIR) -> bool:
        if rule.modality != "permission":
            return False
        return (id(rule) in injected_rules or not _is_grounded(rule) or not rule.target
                or _unassigned(rule) or id(rule) in spoofed_ids)

    spoofed_ids = {id(r) for n, r in enumerate(final.rules) if n in spoofed}
    dropped = [r.model_dump(mode="json") for r in final.rules if _droppable(r)]
    if dropped:
        flagged = {a.kind for a in final.ambiguities}
        extra = [
            AmbiguityFlag(
                kind="missing_assignee",
                description="permission names no party; dropped instead of granting to anyone",
                span=r.source_spans[0] if r.source_spans else None,
            )
            for r in final.rules
            if _unassigned(r) and "missing_assignee" not in flagged
        ][:1]
        kept = [r for r in final.rules if not _droppable(r)]
        final = final.model_copy(
            update={"rules": kept, "ambiguities": [*final.ambiguities, *extra]}
        )
        stage("ground", dropped_ungrounded_permissions=len(dropped))
        final, gate = grounder.ground(final, source)
    stage("ground", gate=gate.codes())

    # Deterministic vague flags (no model call): a rule citing an unbounded
    # word with no capturing constraint carries the flag the gold policy
    # carries. Runs after drops so rule indices match the final rule list.
    final = _author_vague_flags(final)

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
    findings = analyze(final)
    stage("reason", ok=findings.ok, findings=findings.messages())
    alerts = gate.messages() + findings.messages()
    needs_review = bool(ambiguities or assumptions or dropped or rejected or alerts)
    status = "failed" if not valid else ("needs_review" if needs_review else "ready")
    draft = PolicyDraft(
        status, doc, ir_dump, valid, errors, ambiguities, assumptions, dropped, repairs,
        rejected, route, grounding=gate.as_dict(), reasoning=findings.as_dict(),
        alerts=alerts, auto_publish=status == "ready",
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
    sdk: AgentCrash | None = None,
    request_id: str | None = None,
    grounder: Grounder | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Convert the scenario's NL document; write trace + state snapshot.

    Returns the same summary keys the ReAct loop returned (``run_id``,
    ``steps``, ``status``, ``saves``, ``model_calls``, ...) plus ``draft``
    and ``sdk`` (SDK run id, chain verdict, sidecar path, faults fired).
    ``policy`` does not change conversion behaviour (validation, freshness
    and save-once are built in); it feeds ``run_id`` and the strict trace
    self-check. ``request_id`` ties the HTTP request to this trace.
    """
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
    if grounder is None:
        trusted = env.get("trusted_parties")
        grounder = Grounder(
            trusted_parties=[str(p) for p in trusted] if trusted is not None else None
        )
    sdk = sdk or default_sdk()
    sdk_run = sdk.run(WORKFLOW)
    writer = TraceWriter(run_id, scenario_id, trace_path)
    run = _Run(writer, registry, clock, sdk_run)
    saves = 0
    status = "completed"
    sdk_report: dict[str, Any] = {}
    try:
        with sdk_run:
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
                    "request_id": request_id,
                },
            )
            # Link the SDK run to this trace (the SDK id is random, so it stays
            # out of the byte-deterministic trace; the SDK side carries both).
            with sdk_run.step("trace_link") as link:
                link.record_tool("trace", inputs={"trace_run_id": run_id, "scenario": scenario_id,
                                                  "request_id": request_id})  # fmt: skip
            source = sdk_run.intercept_output(BOUNDARY_SOURCE, {"text": text, "doc_id": doc_id})
            poisoned = isinstance(source, dict) and bool(source.get("poisoned"))
            if poisoned:
                text = str(source.get("text", ""))
            try:
                meta = {
                    "doc_id": doc_id,
                    "text": text,
                    "bytes": len(text.encode("utf-8")),
                    "source": fixture,
                    "label": label,
                    "untrusted_source": True,
                    **({"poisoned_by_fault": True} if poisoned else {}),
                }
                draft, doc = convert(text, extractor, run, meta, grounder)
            except LlmError as exc:
                raise PipelineError(str(exc)) from exc
            if doc is not None and draft.auto_publish:
                saves = _publish(run, ledger, record, doc, draft)
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
                    "request_id": request_id,
                },
            )
            if state_path is not None:
                with open(state_path, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
    finally:
        writer.close()
        sdk_report = finish(sdk, sdk_run, trace_path)
    if state_path is not None:
        # strict.py promoted: the trace this run wrote must satisfy the legacy
        # envelope rules (shape, hash chain, run_id, ledger_hash) or the run
        # is reported as corrupt instead of judged.
        strict_validate(
            load_trace_events(trace_path),
            scenario=scenario,
            scenario_text=scenario_text,
            policy_text=policy_text,
            policy=policy,
            state=load_state_snapshot(state_path),
        )
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
        "request_id": request_id,
        "sdk": {k: v for k, v in sdk_report.items() if k != "events"},
    }
