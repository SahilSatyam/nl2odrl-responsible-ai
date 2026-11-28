"""UFO-L legal position report over a :class:`PolicyIR` (Phase 2.3, Mustafa et al. 2606.24344v1).

Maps ODRL rules into Hohfeldian / UFO-L correlative pairs without mutating the
ODRL JSON-LD graph:

* permission -> ``Permission`` (assignee) + ``NoRight`` (assigner)
* strong=True permission -> also ``Immunity`` (assignee) + ``Disability`` (assigner)
* prohibition -> ``Duty-to-Omit`` (assignee) + ``Right-to-Omission`` (assigner)
* remedy on prohibition -> ``Power`` (assigner) + ``Subjection`` (assignee)
* obligation / precondition duty -> ``Duty-to-Act`` (assignee) + ``Right-to-Action`` (assigner)
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from odrl_agent.ir import DutyIR, PolicyIR, RuleIR

OPEN_BEHAVIOUR_REASON = (
    "behaviour='open' is a normative vacuum (no universal permission); review required"
)


def _duty_positions(
    duty: DutyIR,
    *,
    rule: RuleIR,
    rule_index: int,
    policy: PolicyIR,
) -> list[dict[str, Any]]:
    assigner = rule.assigner or policy.assigner
    assignee = duty.assignee or rule.assignee or policy.assignee
    target = duty.target or rule.target
    if duty.role == "remedy":
        return [
            {
                "position": "Power",
                "relator": "rho_R",
                "bearer": assigner,
                "counterparty": assignee,
                "action": duty.action,
                "prohibited_action": rule.action,
                "target": target,
                "rule_index": rule_index,
            },
            {
                "position": "Subjection",
                "relator": "rho_R",
                "bearer": assignee,
                "counterparty": assigner,
                "action": duty.action,
                "prohibited_action": rule.action,
                "target": target,
                "rule_index": rule_index,
            },
        ]
    return [
        {
            "position": "Duty-to-Act",
            "relator": "rho_D",
            "bearer": assignee,
            "counterparty": assigner,
            "action": duty.action,
            "target": target,
            "rule_index": rule_index,
        },
        {
            "position": "Right-to-Action",
            "relator": "rho_D",
            "bearer": assigner,
            "counterparty": assignee,
            "action": duty.action,
            "target": target,
            "rule_index": rule_index,
        },
    ]


def report(ir: PolicyIR) -> dict[str, Any]:
    """Return the finite UFO-L Hohfeldian position report for ``ir``."""
    positions: list[dict[str, Any]] = []
    for idx, rule in enumerate(ir.rules):
        assigner = rule.assigner or ir.assigner
        assignee = rule.assignee or ir.assignee
        if rule.modality == "permission":
            positions.append({
                "position": "Permission",
                "relator": "rho_P",
                "bearer": assignee,
                "counterparty": assigner,
                "action": rule.action,
                "target": rule.target,
                "rule_index": idx,
                "strong": bool(rule.strong),
            })
            positions.append({
                "position": "NoRight",
                "relator": "rho_P",
                "bearer": assigner,
                "counterparty": assignee,
                "action": rule.action,
                "target": rule.target,
                "rule_index": idx,
            })
            if rule.strong:
                positions.append({
                    "position": "Immunity",
                    "relator": "rho_I",
                    "bearer": assignee,
                    "counterparty": assigner,
                    "action": rule.action,
                    "target": rule.target,
                    "rule_index": idx,
                })
                positions.append({
                    "position": "Disability",
                    "relator": "rho_I",
                    "bearer": assigner,
                    "counterparty": assignee,
                    "action": rule.action,
                    "target": rule.target,
                    "rule_index": idx,
                })
            for duty in rule.duties:
                positions.extend(_duty_positions(duty, rule=rule, rule_index=idx, policy=ir))
        elif rule.modality == "prohibition":
            positions.append({
                "position": "Duty-to-Omit",
                "relator": "rho_F",
                "bearer": assignee,
                "counterparty": assigner,
                "action": rule.action,
                "target": rule.target,
                "rule_index": idx,
            })
            positions.append({
                "position": "Right-to-Omission",
                "relator": "rho_F",
                "bearer": assigner,
                "counterparty": assignee,
                "action": rule.action,
                "target": rule.target,
                "rule_index": idx,
            })
            for duty in rule.duties:
                positions.extend(_duty_positions(duty, rule=rule, rule_index=idx, policy=ir))
        elif rule.modality == "obligation":
            positions.append({
                "position": "Duty-to-Act",
                "relator": "rho_D",
                "bearer": assignee,
                "counterparty": assigner,
                "action": rule.action,
                "target": rule.target,
                "rule_index": idx,
            })
            positions.append({
                "position": "Right-to-Action",
                "relator": "rho_D",
                "bearer": assigner,
                "counterparty": assignee,
                "action": rule.action,
                "target": rule.target,
                "rule_index": idx,
            })
            for duty in rule.duties:
                positions.extend(_duty_positions(duty, rule=rule, rule_index=idx, policy=ir))

    counts = dict(sorted(Counter(p["position"] for p in positions).items()))
    return {
        "policy_type": ir.policy_type,
        "behaviour": ir.behaviour,
        "normative_vacuum": ir.behaviour == "open",
        "positions": positions,
        "summary": counts,
    }
