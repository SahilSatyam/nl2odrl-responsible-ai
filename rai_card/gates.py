"""Governance checks for an NL → ODRL → Cedar/Rego assistant.

Over-permission (the model grants more than the source language allowed) is
the ethical failure mode. Under-permission is operational pain; fail-closed
is the required default. Verdicts are computed here, never copied from
metrics.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal

from rai_card.models import (
    ComplianceBundleSpec,
    DynamicMetrics,
    StaticCard,
    automation_rank,
)
from rai_card.policy import GovernancePolicy

Severity = Literal["error", "warning"]


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return str(value)


@dataclass(frozen=True)
class Check:
    check_id: str
    title: str
    passed: bool
    severity: Severity
    message: str
    control_mapping: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateReport:
    passed: bool
    checks: list[Check]
    generated_at: datetime
    policy_id: str
    model_id: str | None
    errors: list[str]
    warnings: list[str]

    def failing_errors(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    def failing_warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "warning"]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "passed": self.passed,
            "policy_id": self.policy_id,
            "model_id": self.model_id,
            "generated_at": self.generated_at.isoformat(),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": [
                {
                    "check_id": c.check_id,
                    "title": c.title,
                    "passed": c.passed,
                    "severity": c.severity,
                    "message": c.message,
                    "control_mapping": c.control_mapping,
                    "evidence": c.evidence,
                }
                for c in self.checks
            ],
        }
        return json.loads(json.dumps(payload, default=_json_default))


def run_governance_gate(
    raw_static: dict[str, Any],
    raw_dynamic: dict[str, Any],
    policy: GovernancePolicy,
    *,
    static: StaticCard | None,
    dynamic: DynamicMetrics | None,
    static_error: str | None,
    dynamic_error: str | None,
    bundle: ComplianceBundleSpec | None = None,
    bundle_error: str | None = None,
    now: date | None = None,
    strict: bool = False,
) -> GateReport:
    today = now or datetime.now(timezone.utc).date()
    checks: list[Check] = [_completeness(raw_static, policy)]

    checks.append(_schema_check("schema.static", "Model card schema", static_error, "Mitchell 2019; EU AI Act Art. 11"))
    checks.append(_schema_check("schema.dynamic", "Evaluation evidence schema", dynamic_error, "NIST AI RMF MEASURE 2.3"))
    checks.append(_schema_check("schema.bundle", "Compliance bundle schema", bundle_error, "NIST AI RMF GOVERN 1.1"))

    if static and dynamic:
        checks.extend(_identity(static, dynamic, policy, raw_dynamic))
        checks.extend(_pipeline(static, policy))
        checks.extend(_lifecycle(static, policy, today))
        checks.extend(_decision_rights(static, policy))
        checks.extend(_fidelity(dynamic, policy))
        checks.extend(_constructs(dynamic, policy))
        checks.extend(_slices(static, dynamic, policy))
        checks.append(_computed_verdict(raw_dynamic))

    if bundle and static:
        checks.extend(_bundle_alignment(static, bundle))

    errors = [c.message for c in checks if not c.passed and c.severity == "error"]
    warnings = [c.message for c in checks if not c.passed and c.severity == "warning"]
    fail_on_warnings = strict or policy.strict_warnings_fail_ci
    passed = not errors and (not fail_on_warnings or not warnings)

    return GateReport(
        passed=passed,
        checks=checks,
        generated_at=datetime.now(timezone.utc),
        policy_id=policy.policy_id,
        model_id=(static.model_id if static else raw_static.get("model_id")),
        errors=errors,
        warnings=warnings,
    )


def _schema_check(check_id: str, title: str, error: str | None, mapping: str) -> Check:
    return Check(
        check_id=check_id,
        title=title,
        passed=error is None,
        severity="error",
        message="Valid." if error is None else f"Schema validation failed: {error}",
        control_mapping=mapping,
        evidence={"error": error} if error else {},
    )


def _completeness(raw_static: dict[str, Any], policy: GovernancePolicy) -> Check:
    missing = []
    for field_name in policy.required_static_fields:
        value = raw_static.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field_name)
        elif isinstance(value, list) and not value:
            missing.append(field_name)
    return Check(
        check_id="doc.completeness",
        title="Required governance fields",
        passed=not missing,
        severity="error",
        message=(
            "All required static governance fields are present."
            if not missing
            else "Missing or empty required field(s) in model_card.yaml: "
            + ", ".join(f"'{f}'" for f in missing)
        ),
        control_mapping="Mitchell 2019 §Limitations; EU AI Act Art. 13",
        evidence={"missing": missing},
    )


def _identity(
    static: StaticCard,
    dynamic: DynamicMetrics,
    policy: GovernancePolicy,
    raw_dynamic: dict[str, Any],
) -> list[Check]:
    checks: list[Check] = []
    if policy.identity.require_model_id_match:
        match = static.model_id == dynamic.model_id
        checks.append(
            Check(
                check_id="identity.model_id",
                title="System ID consistency",
                passed=match,
                severity="error",
                message=(
                    f"Card and evidence both describe `{static.model_id}`."
                    if match
                    else (
                        f"model_id mismatch: card `{static.model_id}` vs metrics "
                        f"`{dynamic.model_id}`."
                    )
                ),
                control_mapping="EU AI Act Art. 12 (traceability)",
                evidence={"static": static.model_id, "dynamic": dynamic.model_id},
            )
        )

    lineage = dynamic.lineage.model_dump(mode="json")
    missing = [
        name
        for name in policy.identity.require_artifact_hashes
        if not str(lineage.get(name) or "").strip()
    ]
    checks.append(
        Check(
            check_id="identity.lineage",
            title="Gold set and artifact hashes",
            passed=not missing,
            severity="error",
            message=(
                "Gold-set, model, and eval-harness hashes are present."
                if not missing
                else f"Missing lineage hashes: {missing}"
            ),
            control_mapping="NIST AI RMF MEASURE 2.2",
            evidence={"missing": missing, "lineage": lineage},
        )
    )
    if (raw_dynamic.get("fidelity") or {}).get("status") is not None:
        checks.append(
            Check(
                check_id="control.ignored_status",
                title="Embedded PASS stamp ignored",
                passed=True,
                severity="warning",
                message="metrics.json included fidelity.status; the gate ignores it.",
                control_mapping="NIST AI RMF GOVERN 1.2",
                evidence={"ignored": raw_dynamic["fidelity"]["status"]},
            )
        )
    return checks


def _computed_verdict(raw_dynamic: dict[str, Any]) -> Check:
    return Check(
        check_id="control.computed_verdict",
        title="Verdict is computed, not declared",
        passed=True,
        severity="error",
        message="Deploy verdict is computed from policy × evidence.",
        control_mapping="NIST AI RMF GOVERN 1.2",
        evidence={"ignored_status": (raw_dynamic.get("fidelity") or {}).get("status")},
    )


def _pipeline(static: StaticCard, policy: GovernancePolicy) -> list[Check]:
    present = {s.lower() for s in static.pipeline}
    missing = [s for s in policy.required_pipeline_stages if s.lower() not in present]
    return [
        Check(
            check_id="doc.pipeline",
            title="Documented conversion pipeline",
            passed=not missing,
            severity="error",
            message=(
                "Card names NL → ODRL → Cedar → Rego as the intended pipeline."
                if not missing
                else f"Card pipeline is missing required stages: {missing}"
            ),
            control_mapping="W3C ODRL; Cedar / OPA as enforcement encodings",
            evidence={"pipeline": static.pipeline, "missing": missing},
        )
    ]


def _lifecycle(static: StaticCard, policy: GovernancePolicy, today: date) -> list[Check]:
    age = (today - static.last_reviewed).days
    fresh = 0 <= age <= policy.lifecycle.max_days_since_review
    checks = [
        Check(
            check_id="lifecycle.review_freshness",
            title="Model card recency",
            passed=fresh,
            severity="error",
            message=(
                f"Last reviewed {static.last_reviewed.isoformat()} ({age} days ago)."
                if fresh
                else (
                    f"Last reviewed {static.last_reviewed.isoformat()} ({age} days) "
                    f"exceeds {policy.lifecycle.max_days_since_review}-day window."
                )
            ),
            control_mapping="NIST AI RMF GOVERN 1.3",
            evidence={"age_days": age},
        )
    ]
    if policy.lifecycle.recertification_must_be_future:
        future = static.recertification_due >= today
        checks.append(
            Check(
                check_id="lifecycle.recertification",
                title="Recertification not expired",
                passed=future,
                severity="error",
                message=(
                    f"Recertification due {static.recertification_due.isoformat()}."
                    if future
                    else f"Recertification expired on {static.recertification_due.isoformat()}."
                ),
                control_mapping="EU AI Act Art. 9",
                evidence={"due": static.recertification_due.isoformat()},
            )
        )
    return checks


def _decision_rights(static: StaticCard, policy: GovernancePolicy) -> list[Check]:
    rights = static.decision_rights
    checks: list[Check] = []
    if policy.decision_rights.forbid_sole_automated_decision:
        ok = not rights.sole_determinant
        checks.append(
            Check(
                check_id="rights.sole_determinant",
                title="Not the sole enforcement decision",
                passed=ok,
                severity="error",
                message=(
                    "Generated policies are decision-support, not a PEP by themselves."
                    if ok
                    else "sole_determinant is true: generated Cedar/Rego cannot be the only control."
                ),
                control_mapping="EU AI Act Art. 14; fail-closed authorization",
            )
        )

    if static.risk_tier.value == "high":
        cap = policy.decision_rights.high_risk_max_automation
        ok = automation_rank(rights.automation_level) <= automation_rank(cap)
        checks.append(
            Check(
                check_id="rights.automation_cap",
                title="High-risk automation cap",
                passed=ok,
                severity="error",
                message=(
                    f"Automation `{rights.automation_level.value}` is within cap `{cap}`."
                    if ok
                    else f"Automation `{rights.automation_level.value}` exceeds cap `{cap}`."
                ),
                control_mapping="EU AI Act Art. 14",
            )
        )

    if policy.decision_rights.require_pep_signoff:
        checks.append(
            Check(
                check_id="rights.pep_signoff",
                title="Human sign-off before PEP attach",
                passed=rights.attach_to_pep_requires_signoff
                and rights.review.required_before_enforce,
                severity="error",
                message=(
                    "Cedar/Rego may attach to a PEP only after named human sign-off."
                    if rights.attach_to_pep_requires_signoff and rights.review.required_before_enforce
                    else "PEP attach is not gated on human sign-off."
                ),
                control_mapping="NIST AI RMF MANAGE 2.4; authorization change control",
            )
        )

    if policy.decision_rights.require_fail_closed_on_ambiguity:
        checks.append(
            Check(
                check_id="rights.fail_closed",
                title="Fail closed on ambiguous NL",
                passed=rights.fail_closed_on_ambiguity,
                severity="error",
                message=(
                    "Ambiguous natural language must not emit an allow."
                    if rights.fail_closed_on_ambiguity
                    else "fail_closed_on_ambiguity is false — over-permission risk."
                ),
                control_mapping="Least privilege; OECD AI robustness",
            )
        )

    if policy.decision_rights.require_human_override:
        checks.append(
            Check(
                check_id="rights.override",
                title="Human override of generated policy",
                passed=rights.human_override,
                severity="error",
                message=(
                    "Policy authors can reject or edit generated ODRL/Cedar/Rego."
                    if rights.human_override
                    else "human_override is false."
                ),
                control_mapping="EU AI Act Art. 14",
            )
        )
    return checks


def _fidelity(dynamic: DynamicMetrics, policy: GovernancePolicy) -> list[Check]:
    f = dynamic.fidelity
    p = policy.fidelity
    specs = [
        (
            "eval.gold_n",
            "Gold-set size",
            f.gold_n >= p.min_gold_n,
            f"gold_n={f.gold_n} vs min {p.min_gold_n}",
            "NIST AI RMF MEASURE 2.1",
        ),
        (
            "eval.semantic_match",
            "Semantic match vs gold ODRL",
            f.semantic_match >= p.min_semantic_match,
            f"semantic_match={f.semantic_match:.3f} vs min {p.min_semantic_match:.3f}",
            "W3C ODRL information model fidelity",
        ),
        (
            "eval.constraints",
            "ODRL constraint preservation",
            f.odrl_constraint_preservation >= p.min_constraint_preservation,
            f"constraint_preservation={f.odrl_constraint_preservation:.3f} vs min {p.min_constraint_preservation:.3f}",
            "Dropping constraints is silent privilege expansion",
        ),
        (
            "eval.over_permission",
            "Over-permission rate (false allow)",
            f.over_permission_rate <= p.max_over_permission_rate,
            f"over_permission_rate={f.over_permission_rate:.3f} vs max {p.max_over_permission_rate:.3f}",
            "Primary ethical harm: model grants more rights than the NL",
        ),
        (
            "eval.under_permission",
            "Under-permission rate (false deny)",
            f.under_permission_rate <= p.max_under_permission_rate,
            f"under_permission_rate={f.under_permission_rate:.3f} vs max {p.max_under_permission_rate:.3f}",
            "Fail-closed is preferred; still bounded for operability",
        ),
        (
            "eval.cedar",
            "ODRL → Cedar round-trip agreement",
            f.cedar_roundtrip_agree >= p.min_cedar_agree,
            f"cedar_agree={f.cedar_roundtrip_agree:.3f} vs min {p.min_cedar_agree:.3f}",
            "Cedar encoding must not widen ODRL permissions",
        ),
        (
            "eval.rego",
            "ODRL → Rego round-trip agreement",
            f.rego_roundtrip_agree >= p.min_rego_agree,
            f"rego_agree={f.rego_roundtrip_agree:.3f} vs min {p.min_rego_agree:.3f}",
            "OPA/Rego encoding must not widen ODRL permissions",
        ),
    ]
    return [
        Check(
            check_id=cid,
            title=title,
            passed=ok,
            severity="error",
            message=msg if ok else f"{msg} — outside policy.",
            control_mapping=mapping,
        )
        for cid, title, ok, msg, mapping in specs
    ]


def _constructs(dynamic: DynamicMetrics, policy: GovernancePolicy) -> list[Check]:
    missing = [c for c in policy.fidelity.required_constructs if c not in dynamic.construct_coverage]
    thin = [
        c
        for c in policy.fidelity.required_constructs
        if dynamic.construct_coverage.get(c, 0) < policy.fidelity.min_construct_n
    ]
    ok = not missing and not thin
    return [
        Check(
            check_id="eval.constructs",
            title="ODRL construct coverage",
            passed=ok,
            severity="error",
            message=(
                "Gold set covers permission, prohibition, obligation, and constraint."
                if ok
                else f"Missing constructs {missing}; below min n {thin}."
            ),
            control_mapping="W3C ODRL 2.2 core rule types",
            evidence={"coverage": dynamic.construct_coverage, "missing": missing, "thin": thin},
        )
    ]


def _slices(static: StaticCard, dynamic: DynamicMetrics, policy: GovernancePolicy) -> list[Check]:
    named = {m.slice for m in static.known_failure_modes}
    prod_fail = []
    undocumented = []
    for sl in dynamic.slices:
        if sl.in_production and (
            sl.over_permission_rate > policy.slices.max_over_permission_if_in_production
            or sl.semantic_match < policy.slices.min_semantic_match_if_in_production
        ):
            prod_fail.append(sl.name)
        if (
            policy.slices.require_known_failure_modes_for_out_of_prod_slices
            and not sl.in_production
            and sl.name not in named
        ):
            undocumented.append(sl.name)
    return [
        Check(
            check_id="slices.production_quality",
            title="In-production slices meet safety floor",
            passed=not prod_fail,
            severity="error",
            message=(
                "In-production slices meet semantic-match and over-permission floors."
                if not prod_fail
                else f"In-production slices below floor: {prod_fail}"
            ),
            control_mapping="Scoped deployment of known-weak constructs",
            evidence={"failing": prod_fail},
        ),
        Check(
            check_id="slices.documented_exclusions",
            title="Suppressed slices named on the card",
            passed=not undocumented,
            severity="error",
            message=(
                "Out-of-production slices are listed under known_failure_modes."
                if not undocumented
                else f"Undocumented suppressed slices: {undocumented}"
            ),
            control_mapping="Mitchell 2019 Limitations",
            evidence={"undocumented": undocumented},
        ),
    ]


def _bundle_alignment(static: StaticCard, bundle: ComplianceBundleSpec) -> list[Check]:
    id_ok = bundle.system_id == static.model_id
    att = bundle.attestations
    aligned = (
        att.no_auto_pep_attach == static.decision_rights.attach_to_pep_requires_signoff
        and att.fail_closed_on_ambiguity == static.decision_rights.fail_closed_on_ambiguity
        and att.not_legal_advice
        and att.gold_set_frozen
    )
    return [
        Check(
            check_id="bundle.system_id",
            title="Bundle describes this system",
            passed=id_ok,
            severity="error",
            message=(
                f"Compliance bundle system_id matches `{static.model_id}`."
                if id_ok
                else f"bundle.system_id `{bundle.system_id}` ≠ card `{static.model_id}`."
            ),
            control_mapping="Release integrity",
        ),
        Check(
            check_id="bundle.attestations",
            title="Bundle attestations match the card",
            passed=aligned,
            severity="error",
            message=(
                "Not-legal-advice, no auto-PEP-attach, fail-closed, frozen gold set."
                if aligned
                else "Bundle attestations contradict the model card or are incomplete."
            ),
            control_mapping="NIST AI RMF GOVERN 1.1 (accountability)",
            evidence=att.model_dump(),
        ),
    ]
