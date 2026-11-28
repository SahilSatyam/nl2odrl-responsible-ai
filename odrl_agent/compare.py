"""Asymmetric and symmetric policy comparison over normalised IRs (Phase 3.3).

Implements Salas et al. (2509.05139v1 Def. 4-5, Thm. 1) and (2603.12926v1 Cor. 3)
together with Mustafa et al. (2602.19883) KB-backed constraint entailment:

* Consistent form + normalisation: ``carve`` then ``normalise`` (permissions
  and prohibitions only; obligations are never DNF-split).
* Asymmetric containment (``requester <= provider``):
  1. Every requester permission is contained in the provider's simple permissions
     and does not overlap any provider prohibition.
  2. Every provider obligation has a requester obligation contained in it (and
     precondition duties on matched permissions are preserved).
* Offer vs Agreement is evaluated as asymmetric containment only (``symmetric``
  is ``False``), and ``evaluator.decide`` is never called on an Offer.
* Zero model calls.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import z3

from odrl_agent.carve import carve
from odrl_agent.grounder import normalize_name
from odrl_agent.ir import ConstraintNode, DutyIR, PolicyIR, RuleIR
from odrl_agent.kb import DEFAULT_KBS, OperandKB, ground_constraints
from odrl_agent.normalise import normalise
from odrl_agent.reasoning import _Z3_LOCK, _Encoder, check_satisfiable, includes


def _norm_entity(name: str | None) -> str | None:
    if not name:
        return None
    text = name.strip()
    if ":" in text and " " not in text:
        text = text.rstrip("/#").replace("#", "/").split("/")[-1].split(":")[-1]
    return normalize_name(text)


def _assignee(rule: RuleIR, ir: PolicyIR) -> str | None:
    return rule.assignee or ir.assignee


def _same_entity(a: str | None, b: str | None) -> bool:
    na, nb = _norm_entity(a), _norm_entity(b)
    return na is None or nb is None or na == nb


def _covers_entity(sup: str | None, sub: str | None) -> bool:
    """True when ``sup`` covers every entity denoted by ``sub`` (None = any)."""
    nsup, nsub = _norm_entity(sup), _norm_entity(sub)
    if nsup is None:
        return True
    if nsub is None:
        return False
    return nsup == nsub


def _node_sig(node: ConstraintNode) -> str:
    return node.model_dump_json(exclude={"span"})


def _rule_nodes(rule: RuleIR) -> list[ConstraintNode]:
    return list(rule.constraints) + list(rule.action_refinements)


def _syntactic_subset(sup_nodes: list[ConstraintNode], sub_nodes: list[ConstraintNode]) -> bool:
    if not sup_nodes:
        return True
    sub_sigs = {_node_sig(n) for n in sub_nodes}
    return all(_node_sig(n) in sub_sigs for n in sup_nodes)


def _entails_disjunction(
    sub_nodes: list[ConstraintNode],
    sup_lists: list[list[ConstraintNode]],
    kbs: Mapping[str, OperandKB],
) -> bool:
    """True when ``sub_nodes`` provably entails ``\\/_{L in sup_lists} /\\ L``."""
    if not sup_lists:
        return False
    if any(not lst for lst in sup_lists):
        return True
    if any(_syntactic_subset(lst, sub_nodes) for lst in sup_lists):
        return True

    sub_verdict = check_satisfiable(sub_nodes, kbs=kbs)
    if sub_verdict == "unsat":
        return True
    if sub_verdict == "unknown":
        return False

    valid_sups = [lst for lst in sup_lists if check_satisfiable(lst, kbs=kbs) != "unknown"]
    if not valid_sups:
        return False

    with _Z3_LOCK:
        enc = _Encoder(kbs=kbs)
        lhs = enc.all(sub_nodes)
        rhs = z3.Or(*[enc.all(lst) for lst in valid_sups])
        solver = z3.Solver()
        solver.set("timeout", 2000)
        solver.add(lhs, z3.Not(rhs), *enc.axioms)
        return solver.check() == z3.unsat


def _entails(
    sub_nodes: list[ConstraintNode],
    sup_nodes: list[ConstraintNode],
    kbs: Mapping[str, OperandKB],
) -> bool:
    return _entails_disjunction(sub_nodes, [sup_nodes], kbs)


def _duty_constraints(duty: DutyIR) -> list[ConstraintNode]:
    out = list(duty.constraints)
    if duty.deadline is not None:
        out.append(duty.deadline)
    return out


def _duty_covered(prov_duty: DutyIR, req_duty: DutyIR, kbs: Mapping[str, OperandKB]) -> bool:
    if not includes(prov_duty.action, req_duty.action):
        return False
    if not _covers_entity(prov_duty.target, req_duty.target):
        return False
    if not _covers_entity(prov_duty.assignee, req_duty.assignee):
        return False
    return _entails(_duty_constraints(req_duty), _duty_constraints(prov_duty), kbs)


def _rule_summary(rule: RuleIR, ir: PolicyIR) -> dict[str, Any]:
    return {
        "modality": rule.modality,
        "action": rule.action,
        "target": rule.target,
        "assignee": _assignee(rule, ir),
    }


def _check_asymmetric(
    provider: PolicyIR,
    requester: PolicyIR,
    kbs: Mapping[str, OperandKB],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(extra_permissions, missing_obligations)`` for ``requester <= provider``."""
    prov_norm = normalise(carve(ground_constraints(provider, kbs=kbs)))
    req_norm = normalise(carve(ground_constraints(requester, kbs=kbs)))

    prov_perms = [r for r in prov_norm.rules if r.modality == "permission"]
    prov_prohibs = [r for r in prov_norm.rules if r.modality == "prohibition"]
    prov_obls = [r for r in prov_norm.rules if r.modality == "obligation"]
    req_perms = [r for r in req_norm.rules if r.modality == "permission"]
    req_obls = [r for r in req_norm.rules if r.modality == "obligation"]

    extra_permissions: list[dict[str, Any]] = []
    missing_obligations: list[dict[str, Any]] = []

    for r_req in req_perms:
        r_nodes = _rule_nodes(r_req)
        r_ass = _assignee(r_req, req_norm)

        # Unsatisfiable permission grants nothing.
        if r_nodes and check_satisfiable(r_nodes, kbs=kbs) == "unsat":
            continue

        # Check overlapping provider prohibitions (even non-negatable set operators).
        blocked = False
        for f_prov in prov_prohibs:
            if f_prov.action != r_req.action:
                continue
            if not (_same_entity(r_req.target, f_prov.target) and _same_entity(r_ass, _assignee(f_prov, prov_norm))):
                continue
            f_nodes = _rule_nodes(f_prov)
            if not f_nodes or check_satisfiable(r_nodes, f_nodes, kbs=kbs) != "unsat":
                blocked = True
                break
        if blocked:
            extra_permissions.append(_rule_summary(r_req, req_norm))
            continue

        matching_prov = [
            p
            for p in prov_perms
            if p.action == r_req.action
            and _covers_entity(p.target, r_req.target)
            and _covers_entity(_assignee(p, prov_norm), r_ass)
        ]
        if not matching_prov:
            extra_permissions.append(_rule_summary(r_req, req_norm))
            continue

        if not _entails_disjunction(r_nodes, [_rule_nodes(p) for p in matching_prov], kbs):
            extra_permissions.append(_rule_summary(r_req, req_norm))
            continue

        # Check precondition duties on matched provider permissions.
        perms_with_duties = [p for p in matching_prov if any(d.role == "precondition" for d in p.duties)]
        if len(perms_with_duties) == len(matching_prov):
            req_pre = [d for d in r_req.duties if d.role == "precondition"]
            satisfied_by_some = False
            for p in perms_with_duties:
                prov_pre = [d for d in p.duties if d.role == "precondition"]
                if all(any(_duty_covered(pd, rd, kbs) for rd in req_pre) for pd in prov_pre):
                    satisfied_by_some = True
                    break
            if not satisfied_by_some:
                for pd in perms_with_duties[0].duties:
                    if pd.role == "precondition" and not any(_duty_covered(pd, rd, kbs) for rd in req_pre):
                        missing_obligations.append({
                            "modality": "duty",
                            "action": pd.action,
                            "for_action": perms_with_duties[0].action,
                            "target": pd.target or perms_with_duties[0].target,
                            "assignee": pd.assignee or _assignee(perms_with_duties[0], prov_norm),
                        })

    for o_prov in prov_obls:
        o_prov_nodes = _rule_nodes(o_prov)
        o_prov_ass = _assignee(o_prov, prov_norm)
        matched = False
        for o_req in req_obls:
            if not includes(o_prov.action, o_req.action):
                continue
            if not _covers_entity(o_prov.target, o_req.target):
                continue
            if not _covers_entity(o_prov_ass, _assignee(o_req, req_norm)):
                continue
            if _entails(_rule_nodes(o_req), o_prov_nodes, kbs):
                matched = True
                break
        if not matched:
            missing_obligations.append(_rule_summary(o_prov, prov_norm))

    return extra_permissions, missing_obligations


def compare(
    provider: PolicyIR,
    requester: PolicyIR,
    *,
    kbs: Mapping[str, OperandKB] | None = None,
) -> dict[str, Any]:
    """Compare ``requester`` against ``provider`` without model calls.

    Returns ``{symmetric, asymmetric, missing_obligations, extra_permissions}``.
    ``asymmetric`` is ``True`` when ``requester`` is contained in ``provider``
    (no extra permissions and no missing obligations).
    """
    active_kbs = DEFAULT_KBS if kbs is None else kbs
    extra_permissions, missing_obligations = _check_asymmetric(provider, requester, active_kbs)
    asymmetric = not extra_permissions and not missing_obligations

    if (
        "Offer" in (provider.policy_type, requester.policy_type)
        and provider.policy_type != requester.policy_type
    ):
        symmetric = False
    elif not asymmetric:
        symmetric = False
    else:
        rev_extra, rev_missing = _check_asymmetric(requester, provider, active_kbs)
        symmetric = not rev_extra and not rev_missing

    return {
        "symmetric": symmetric,
        "asymmetric": asymmetric,
        "missing_obligations": missing_obligations,
        "extra_permissions": extra_permissions,
    }
