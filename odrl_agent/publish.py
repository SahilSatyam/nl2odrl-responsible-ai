"""Draft-vs-publish HITL gate: the review queue and Approve & Publish (Phase 2.3).

The pipeline auto-publishes only a draft with zero ambiguities, assumptions,
dropped rules, grounding issues and conflicts and a full SHACL pass. Every
other draft is held. A reviewer then decides, with a reason:

``approve``
    Publish the draft as compiled. Allowed when the remaining alerts are
    things a human may vouch for (grounding, ambiguities, assumptions,
    dropped rules). Never allowed when SHACL fails, or when a conflict or
    unsatisfiable constraint remains: those must be fixed by an edit.
``edit``
    Publish a reviewer-corrected IR. It is re-grounded, recompiled,
    SHACL-validated and re-checked for conflicts; the same blocking rules
    apply. Edits that widen the original grants are allowed (the human holds
    the authority the model never has) but are recorded as such.
``reject``
    Close the draft without publishing.

Every check is recomputed here; nothing is taken from the stored draft's own
verdicts. One decision per run (``run_key`` is the primary key): a second
decision is refused, so a draft is published at most once.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from odrl_agent import db as db_module
from odrl_agent.compiler import CompileError, compile_ir
from odrl_agent.ir import PolicyIR
from odrl_agent.kb import ground_constraints
from odrl_agent.odrl import contains_attacker_grant, validate_odrl
from odrl_agent.positions import OPEN_BEHAVIOUR_REASON
from odrl_agent.reasoning import UNKNOWN_CONSTRAINT, analyze

PUBLICATIONS_SQL = (
    "CREATE TABLE IF NOT EXISTS nl2odrl_publications ("
    " run_key TEXT PRIMARY KEY,"
    " decision TEXT NOT NULL,"
    " reason TEXT NOT NULL,"
    " reviewer_fp TEXT NOT NULL,"
    " edited INTEGER NOT NULL DEFAULT 0,"
    " widening_json TEXT NOT NULL DEFAULT '[]',"
    " alerts_json TEXT NOT NULL DEFAULT '[]',"
    " doc_json TEXT,"
    " ir_json TEXT,"
    " created_at TEXT NOT NULL)"
)
DECISIONS = ("approve", "edit", "reject")
MAX_REASON = 2000


class PublishError(ValueError):
    """A refused decision; ``code`` and ``status`` map to the HTTP reply."""

    def __init__(self, code: str, hint: str, status: int, detail: Any = None) -> None:
        super().__init__(hint)
        self.code = code
        self.hint = hint
        self.status = status
        self.detail = detail


def ensure_tables(conn: Any) -> None:
    conn.execute(PUBLICATIONS_SQL)


def run_end_detail(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events or []):
        detail = event.get("detail") or {}
        if detail.get("phase") == "run_end":
            return detail
    return {}


def draft_of(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    draft = run_end_detail(events).get("draft")
    return draft if isinstance(draft, dict) else None


def source_of(events: list[dict[str, Any]]) -> str:
    for event in events or []:
        detail = event.get("detail") or {}
        if detail.get("phase") == "pipeline_stage" and detail.get("stage") == "ingest":
            return str(detail.get("text", ""))
    return ""


def decision_for(conn: Any, run_key: str) -> dict[str, Any] | None:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT run_key, decision, reason, reviewer_fp, edited, widening_json, alerts_json,"
        " created_at FROM nl2odrl_publications WHERE run_key = ?",
        (run_key,),
    ).fetchone()
    if row is None:
        return None
    out = {k: row[k] for k in row.keys()}
    out["edited"] = bool(out["edited"])
    out["widening"] = json.loads(out.pop("widening_json") or "[]")
    out["alerts"] = json.loads(out.pop("alerts_json") or "[]")
    return out


@dataclass
class Review:
    """Freshly recomputed verdicts for one candidate document."""

    doc: dict[str, Any] | None
    ir: dict[str, Any] | None
    valid: bool
    shacl_errors: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    widening: list[str] = field(default_factory=list)

    def blocking(self) -> list[str]:
        out = []
        if self.doc is None:
            out.append("no compiled document to publish")
        if not self.valid:
            out.extend(f"shacl: {e}" for e in self.shacl_errors or ["document is not valid"])
        out.extend(self.conflicts)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "shacl_errors": self.shacl_errors,
            "conflicts": self.conflicts,
            "alerts": self.alerts,
            "widening": self.widening,
            "blocking": self.blocking(),
            "odrl": self.doc,
        }


def review_draft(
    draft: dict[str, Any], source: str, edited_ir: dict[str, Any] | None = None
) -> Review:
    """Recompute every gate for the stored draft, or for a reviewer edit."""
    from odrl_agent.grounder import Grounder
    from odrl_agent.pipeline import ground as resolve_spans
    from odrl_agent.pipeline import widening

    raw_ir = edited_ir if edited_ir is not None else draft.get("ir")
    if not isinstance(raw_ir, dict):
        return Review(None, None, False, ["draft carries no IR"])
    try:
        ir = PolicyIR.model_validate(raw_ir)
    except ValidationError as exc:
        raise PublishError(
            "E_SCHEMA", "edited IR does not match the PolicyIR schema", 400,
            json.loads(exc.json(include_url=False)),
        ) from exc  # fmt: skip
    grounded, span_violations = resolve_spans(ir, source)
    grounded, gate = Grounder().ground(grounded, source)
    grounded = ground_constraints(grounded)
    widened: list[str] = []
    if edited_ir is not None and isinstance(draft.get("ir"), dict):
        try:
            widened = widening(PolicyIR.model_validate(draft["ir"]), grounded)
        except ValidationError:
            widened = []
    if not grounded.rules:
        return Review(None, grounded.model_dump(mode="json"), False, ["no rules to publish"])
    try:
        doc, _graph = compile_ir(grounded)
    except CompileError as exc:
        return Review(None, grounded.model_dump(mode="json"), False, [f"compile: {exc}"])
    valid, errors = validate_odrl(doc)
    findings = analyze(grounded)
    blocking_conflicts = findings.blocking_messages()
    unknown_alerts = [
        f"reasoning: {f.code} {f.path}: {f.detail}"
        for f in findings.unknown
    ]
    alerts = list(span_violations) + gate.messages() + unknown_alerts
    if grounded.behaviour == "open":
        alerts.append(OPEN_BEHAVIOUR_REASON)
    alerts += [f"ambiguity: {a.kind}: {a.description}" for a in grounded.ambiguities]
    alerts += [f"assumption: {a.field}={a.assumed_value}" for a in grounded.assumptions]
    if edited_ir is None:
        alerts += [f"dropped rule: {r.get('action')} -> {r.get('assignee')}"
                   for r in draft.get("dropped_rules") or []]  # fmt: skip
    return Review(
        doc, grounded.model_dump(mode="json"), valid, errors, blocking_conflicts, alerts, widened
    )


def decide(
    conn: Any,
    *,
    run_key: str,
    events: list[dict[str, Any]],
    decision: str,
    reason: str,
    edited_ir: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one reviewer decision; publish on approve/edit. Exactly once per run."""
    if decision not in DECISIONS:
        raise PublishError("E_CONFIG", f"decision must be one of {list(DECISIONS)}", 400)
    if not isinstance(reason, str) or not reason.strip():
        raise PublishError("E_CONFIG", "a reason is required for every decision", 400)
    if len(reason) > MAX_REASON:
        raise PublishError("E_CONFIG", f"reason must be at most {MAX_REASON} characters", 400)
    if decision == "edit" and not isinstance(edited_ir, dict):
        raise PublishError("E_CONFIG", "decision 'edit' needs an 'ir' object", 400)
    if decision != "edit" and edited_ir is not None:
        raise PublishError("E_CONFIG", "'ir' is only accepted with decision 'edit'", 400)
    draft = draft_of(events)
    if draft is None:
        raise PublishError("E_STATE", "run has no draft to review", 409)
    if draft.get("auto_publish"):
        raise PublishError("E_STATE", "draft was auto-published by the pipeline", 409)
    ensure_tables(conn)
    if decision_for(conn, run_key) is not None:
        raise PublishError("E_DECIDED", "this draft already has a decision", 409)

    review: Review | None = None
    if decision != "reject":
        review = review_draft(draft, source_of(events), edited_ir)
        blocking = review.blocking()
        if blocking:
            raise PublishError(
                "E_BLOCKED",
                "draft cannot be published until these are fixed: " + "; ".join(blocking),
                422,
                review.as_dict(),
            )
    now = db_module.utcnow()
    try:
        conn.execute(
            "INSERT INTO nl2odrl_publications (run_key, decision, reason, reviewer_fp, edited,"
            " widening_json, alerts_json, doc_json, ir_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_key, decision, reason.strip(), "local", 1 if decision == "edit" else 0,
                json.dumps(review.widening if review else []),
                json.dumps(review.alerts if review else []),
                json.dumps(review.doc, sort_keys=True) if review else None,
                json.dumps(review.ir, sort_keys=True) if review else None, now,
            ),
        )  # fmt: skip
        if review is not None:
            conn.execute(
                "INSERT INTO odrl_docs (run_key, step, outcome, doc_json, valid, errors_json,"
                " attacker) VALUES (?,?,?,?,?,?,?)",
                (
                    run_key, 0, "published", json.dumps(review.doc, sort_keys=True), 1,
                    json.dumps([]), 1 if contains_attacker_grant(review.doc) else 0,
                ),
            )  # fmt: skip
        conn.commit()
    except Exception as exc:  # sqlite3.IntegrityError on a racing second decision
        conn.rollback()
        if "UNIQUE" in str(exc) or "PRIMARY KEY" in str(exc):
            raise PublishError("E_DECIDED", "this draft already has a decision", 409) from exc
        raise
    out = decision_for(conn, run_key) or {}
    out["published"] = review is not None
    if review is not None:
        out["odrl"] = review.doc
    return out


def list_drafts(
    conn: Any, *, include_decided: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    """Held drafts, newest first."""
    ensure_tables(conn)
    query = (
        "SELECT r.run_key, r.created_at, r.nl_text, e.detail_json, p.decision"
        " FROM runs r JOIN events e ON e.run_key = r.run_key AND e.phase = 'run_end'"
        " LEFT JOIN nl2odrl_publications p ON p.run_key = r.run_key"
    )
    clauses, args = [], []
    if not include_decided:
        clauses.append("p.run_key IS NULL")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?"
    args.append(max(1, min(500, int(limit))))
    out = []
    for row in conn.execute(query, args):
        detail = json.loads(row["detail_json"] or "{}")
        draft = detail.get("draft") or {}
        if draft.get("auto_publish") or draft.get("status") not in ("needs_review", "no_rules"):
            continue
        out.append({
            "run_key": row["run_key"],
            "created_at": row["created_at"],
            "nl_text": row["nl_text"],
            "decision": row["decision"],
            "draft": draft,
        })  # fmt: skip
    return out
