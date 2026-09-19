from __future__ import annotations

from datetime import datetime, timezone

from rai_card.gates import GateReport
from rai_card.models import ComplianceBundleSpec, DynamicMetrics, StaticCard
from rai_card.policy import GovernancePolicy


def render_model_card(
    static: StaticCard,
    dynamic: DynamicMetrics,
    report: GateReport,
    policy: GovernancePolicy,
    *,
    generated_at: datetime | None = None,
) -> str:
    ts = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    f = dynamic.fidelity
    rights = static.decision_rights
    verdict = "PASS" if report.passed else "BLOCKED"
    users = "\n".join(f"- {u}" for u in static.intended_users)
    oos = "\n".join(f"- {u}" for u in static.out_of_scope)
    prohibited = "\n".join(f"- {u}" for u in static.prohibited_uses)
    pipeline = " → ".join(f"`{s}`" for s in static.pipeline)
    failure_modes = (
        "\n".join(
            f"- **{m.slice}** — {m.handling}: {m.description}"
            for m in static.known_failure_modes
        )
        or "_None documented._"
    )
    slice_rows = "\n".join(
        f"| {s.name} | {s.n} | {s.semantic_match:.3f} | {s.over_permission_rate:.3f} | "
        f"{'yes' if s.in_production else 'no'} |"
        for s in dynamic.slices
    ) or "| _none_ | | | | |"
    coverage_rows = "\n".join(
        f"| `{k}` | {v} |" for k, v in sorted(dynamic.construct_coverage.items())
    )
    check_rows = "\n".join(
        f"| `{c.check_id}` | {'PASS' if c.passed else c.severity.upper()} | {c.title} | {c.message} |"
        for c in report.checks
    )
    targets = ", ".join(f"`{t}`" for t in static.regulatory_basis.authorization_targets)
    nist = ", ".join(static.regulatory_basis.nist_ai_rmf_functions)
    frameworks = "\n".join(f"- {x}" for x in policy.frameworks)

    return f"""# Model Card: {static.model_name}

> Auto-generated on {ts} by `rai-card` (policy `{policy.policy_id}`).
> Build artifact. Edit `model_card.yaml` and `metrics.json` — not this file.

**Deploy verdict: `{verdict}`** — {len(report.errors)} error(s), {len(report.warnings)} warning(s).

| Field | Value |
|---|---|
| System ID | `{static.model_id}` |
| Eval version | `{dynamic.version}` |
| Risk tier | `{static.risk_tier.value}` |
| Owner | {static.owner_team} |
| Pipeline | {pipeline} |
| Last reviewed | {static.last_reviewed.isoformat()} |
| Recertify by | {static.recertification_due.isoformat()} |
| Gold set hash | `{dynamic.lineage.gold_set_hash}` |
| Model hash | `{dynamic.lineage.model_artifact_hash}` |
| Eval harness SHA | `{dynamic.lineage.eval_harness_git_sha}` |
| Evaluated on | {dynamic.lineage.evaluated_on.isoformat()} |
| Enforcement encodings | {targets} |

## 1. Intended use

{static.intended_use}

**Intended users**

{users}

This card describes a **policy-authoring assistant**. It does not evaluate live authorization requests. Evaluation here means: gold NL vs gold ODRL vs compiled Cedar/Rego, measured by a separate harness.

## 2. Out of scope and prohibited uses

**Out of scope**

{oos}

**Prohibited**

{prohibited}

## 3. Decision rights

| Control | Value |
|---|---|
| Automation | `{rights.automation_level.value}` |
| Sole determinant | `{str(rights.sole_determinant).lower()}` |
| Author override | `{str(rights.human_override).lower()}` |
| Sign-off before PEP attach | `{str(rights.attach_to_pep_requires_signoff).lower()}` |
| Fail closed on ambiguous NL | `{str(rights.fail_closed_on_ambiguity).lower()}` |
| Review | {rights.review.channel} (SLA {rights.review.sla_days} days) |

## 4. Limitations

{static.limitations}

### Known failure modes

{failure_modes}

## 5. Ethical considerations

{static.ethical_considerations}

The red-line metric is **over-permission**: the conversion grants a right the source language did not. Under-permission is fail-closed and is tolerated up to a bound.

## 6. Evaluation evidence (not editable)

{static.evaluation_data_summary or ""}

| Metric | Value | Policy |
|---|---|---|
| Gold n | {f.gold_n} | ≥ {policy.fidelity.min_gold_n} |
| Semantic match | {f.semantic_match:.3f} | ≥ {policy.fidelity.min_semantic_match:.3f} |
| Constraint preservation | {f.odrl_constraint_preservation:.3f} | ≥ {policy.fidelity.min_constraint_preservation:.3f} |
| Over-permission rate | {f.over_permission_rate:.3f} | ≤ {policy.fidelity.max_over_permission_rate:.3f} |
| Under-permission rate | {f.under_permission_rate:.3f} | ≤ {policy.fidelity.max_under_permission_rate:.3f} |
| Cedar agreement | {f.cedar_roundtrip_agree:.3f} | ≥ {policy.fidelity.min_cedar_agree:.3f} |
| Rego agreement | {f.rego_roundtrip_agree:.3f} | ≥ {policy.fidelity.min_rego_agree:.3f} |

### ODRL construct coverage

| Construct | Gold cases |
|---|---:|
{coverage_rows}

### Slices

| Slice | n | Semantic match | Over-permission | In production |
|---|---:|---:|---:|---|
{slice_rows}

## 7. Regulatory mapping

| Instrument | Statement |
|---|---|
| EU AI Act | {static.regulatory_basis.eu_ai_act_role} |
| ODRL profile | {static.regulatory_basis.odrl_profile} |
| NIST AI RMF | {nist} |

{frameworks}

## 8. Contact

- Owner: `{static.contact.owner_email}`
- Model risk: `{static.contact.model_risk_email}`
- Ethics: `{static.contact.ethics_escalation}`

{static.caveats_and_recommendations or ""}

## 9. Gate results

| Check | Result | Title | Detail |
|---|---|---|---|
{check_rows}
"""


def render_compliance_bundle(
    static: StaticCard,
    dynamic: DynamicMetrics,
    bundle: ComplianceBundleSpec,
    report: GateReport,
    policy: GovernancePolicy,
    *,
    generated_at: datetime | None = None,
) -> str:
    ts = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    verdict = "PASS" if report.passed else "BLOCKED"
    artifacts = "\n".join(f"- `{a}`" for a in bundle.required_artifacts)
    exhibits = "\n".join(f"- `{p}`" for p in bundle.exhibit_paths)
    att = bundle.attestations
    residual = "\n".join(
        f"- {c.message}" for c in report.checks if not c.passed
    ) or "- None from this gate (residual product risk still exists; see the model card)."

    return f"""# Compliance Bundle: {static.model_name}

> Auto-generated on {ts}. This is the **release envelope**, not the converter.
> Bundle spec: `{bundle.bundle_id}` · policy `{policy.policy_id}`.

**Gate: `{verdict}`**

## Purpose

Prove that a Natural Language → ODRL → Cedar / OPA-Rego **assistant** is documented, measured, and human-gated before anyone treats its output as enforceable policy.

This repository does **not** ship a compiler. The bundle records what must be true of whatever compiler you plug in later.

## Inventory (required for a release)

{artifacts}

## Frozen exhibits (illustrative gold pairs, not generated here)

{exhibits}

These files exist so a reviewer can see *what* evaluation means. They are not produced by this CI step.

## Attestations

| Claim | Value |
|---|---|
| Not legal advice | `{str(att.not_legal_advice).lower()}` |
| No auto-attach to a PEP | `{str(att.no_auto_pep_attach).lower()}` |
| Fail closed on ambiguous NL | `{str(att.fail_closed_on_ambiguity).lower()}` |
| Gold set frozen (hashed) | `{str(att.gold_set_frozen).lower()}` |
| Gold set hash | `{dynamic.lineage.gold_set_hash}` |
| System ID | `{bundle.system_id}` |
| Owner | {bundle.owner_team} |

## Human sign-off checklist (do not automate)

- [ ] Policy author reviewed ODRL against the source NL (permissions, prohibitions, obligations, constraints).
- [ ] Cedar and Rego encodings do not widen ODRL permissions (spot-check over-permit cases).
- [ ] Ambiguous sentences were rejected or sent to fail-closed, not guessed into an allow.
- [ ] No PEP / OPA / Cedar store was updated without a named reviewer on `{static.decision_rights.review.channel}`.
- [ ] Model card recertification date `{static.recertification_due.isoformat()}` is still in the future.

## Residual issues from this gate

{residual}

## How to use this dummy in a talk

1. Show `model_card.yaml` as the human-owned narrative (intended use, limitations, HITL).
2. Show `metrics.json` as eval-harness numbers — especially **over-permission**.
3. Run `python3 generate_card.py` and point at this bundle + `MODEL_CARD.md`.
4. Comment out `limitations` (or swap `model_card_BROKEN_demo.yaml`) and show the non-zero exit.
"""
