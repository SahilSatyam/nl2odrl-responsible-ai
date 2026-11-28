"""Deterministic ODRL 2.2 compiler: ``PolicyIR`` -> JSON-LD + RDF graph.

Pure Python. No model calls, no network, no clock. The same IR always
yields byte-identical JSON-LD (``canonical_json``) and an isomorphic graph.
Party and asset names become IRIs here (code owns identity); literals are
typed here (code owns syntax). The graph is parsed with the bundled local
``@context`` so validation never fetches anything.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

import rdflib

from odrl_agent.ir import (
    ConstraintIR,
    DutyIR,
    LogicalConstraintIR,
    PolicyIR,
    RuleIR,
)
from odrl_agent.odrl import ODRL_CONTEXT, jsonld_to_graph

XSD = "http://www.w3.org/2001/XMLSchema#"
PARTY_PREFIX = "urn:nl2odrl:party:"
ASSET_PREFIX = "urn:nl2odrl:asset:"
POLICY_PREFIX = "urn:nl2odrl:policy:"
_ABSOLUTE_IRI = re.compile(r"^(urn|https?|did|tag):[^\s]+$", re.IGNORECASE)
_ODRL = rdflib.Namespace("http://www.w3.org/ns/odrl/2/")


class CompileError(ValueError):
    """The IR is valid but cannot become a complete ODRL policy."""


def mint_iri(name: str, prefix: str) -> str:
    """Return ``name`` when it is already an absolute IRI, else a stable URN.

    Non-ASCII characters are percent-encoded rather than folded, so
    look-alike names stay distinct IRIs.
    """
    text = name.strip()
    if _ABSOLUTE_IRI.match(text):
        return text
    slug = re.sub(r"\s+", "-", text.casefold())
    return prefix + quote(slug, safe="-._~")


def canonical_json(doc: Any) -> str:
    """Key-order-insensitive serialisation for comparison and hashing."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _literal(value_type: str, raw: str) -> Any:
    if value_type == "string":
        return raw
    if value_type == "iri":
        return {"@id": raw}
    return {"@value": raw, "@type": XSD + value_type}


def _constraint(node: ConstraintIR | LogicalConstraintIR) -> dict[str, Any]:
    if isinstance(node, LogicalConstraintIR):
        children = [_constraint(child) for child in node.constraints]
        value: Any = {"@list": children} if node.operator == "andSequence" else children
        return {"type": "LogicalConstraint", node.operator: value}
    return {
        "type": "Constraint",
        "leftOperand": node.left_operand,
        "operator": node.operator,
        "rightOperand": _literal(node.value_type, node.right_operand),
    }


def _action(rule: RuleIR | DutyIR) -> Any:
    refinements = getattr(rule, "action_refinements", [])
    if not refinements:
        return rule.action
    return {
        "rdf:value": {"@id": f"odrl:{rule.action}"},
        "refinement": [_constraint(node) for node in refinements],
    }


def _duty(duty: DutyIR) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "Duty", "action": _action(duty)}
    if duty.target:
        out["target"] = mint_iri(duty.target, ASSET_PREFIX)
    if duty.assignee:
        out["assignee"] = mint_iri(duty.assignee, PARTY_PREFIX)
    constraints = [_constraint(node) for node in duty.constraints]
    if duty.deadline is not None:
        constraints.append(_constraint(duty.deadline))
    if constraints:
        out["constraint"] = constraints
    if duty.consequences:
        out["consequence"] = [_duty(child) for child in duty.consequences]
    return out


def _rule(rule: RuleIR, index: int, policy: PolicyIR) -> dict[str, Any]:
    if rule.modality == "obligation":
        out: dict[str, Any] = {"type": "Duty", "action": _action(rule)}
    else:
        if not rule.target:
            raise CompileError(f"rule {index} ({rule.modality}) has no target")
        out = {
            "type": "Permission" if rule.modality == "permission" else "Prohibition",
            "action": _action(rule),
        }
    if rule.target:
        out["target"] = mint_iri(rule.target, ASSET_PREFIX)
    if rule.assignee and (rule.modality == "obligation" or rule.assignee != policy.assignee):
        out["assignee"] = mint_iri(rule.assignee, PARTY_PREFIX)
    if rule.assigner and rule.assigner != policy.assigner:
        out["assigner"] = mint_iri(rule.assigner, PARTY_PREFIX)
    if rule.constraints:
        out["constraint"] = [_constraint(node) for node in rule.constraints]
    if rule.duties:
        prop_map = {"precondition": "duty", "remedy": "remedy", "consequence": "consequence"}
        by_role: dict[str, list[dict[str, Any]]] = {}
        for duty in rule.duties:
            prop = prop_map.get(duty.role)
            if prop:
                by_role.setdefault(prop, []).append(_duty(duty))
        out.update(by_role)
    return out


def _policy_uid(ir: PolicyIR) -> str:
    if ir.uid:
        return mint_iri(ir.uid, POLICY_PREFIX)
    digest = hashlib.sha256(
        canonical_json(ir.model_dump(mode="json", exclude={"analysis", "ambiguities"})).encode()
    ).hexdigest()
    return POLICY_PREFIX + digest[:16]


def compile_doc(ir: PolicyIR) -> dict[str, Any]:
    """Compile ``ir`` to a compact ODRL 2.2 JSON-LD document."""
    if not ir.rules:
        raise CompileError("no rules to compile")
    doc: dict[str, Any] = {
        "@context": ODRL_CONTEXT,
        "type": ir.policy_type,
        "uid": _policy_uid(ir),
    }
    if ir.conflict_strategy:
        doc["conflict"] = ir.conflict_strategy
    if ir.assigner:
        doc["assigner"] = mint_iri(ir.assigner, PARTY_PREFIX)
    if ir.assignee:
        doc["assignee"] = mint_iri(ir.assignee, PARTY_PREFIX)
    for key, modality in (
        ("permission", "permission"),
        ("prohibition", "prohibition"),
        ("obligation", "obligation"),
    ):
        compiled = [
            _rule(rule, index, ir)
            for index, rule in enumerate(ir.rules)
            if rule.modality == modality
        ]
        if compiled:
            doc[key] = compiled
    return doc


def _count_duty_roles(duties: list[DutyIR]) -> tuple[int, int, int]:
    duty_n = remedy_n = conseq_n = 0
    for d in duties:
        if d.role == "precondition":
            duty_n += 1
        elif d.role == "remedy":
            remedy_n += 1
        elif d.role == "consequence":
            conseq_n += 1
        sub_d, sub_r, sub_c = _count_duty_roles(d.consequences)
        duty_n += sub_d
        remedy_n += sub_r
        conseq_n += sub_c
    return duty_n, remedy_n, conseq_n


def _verify_graph(ir: PolicyIR, graph: rdflib.Graph) -> None:
    """Fail loudly when JSON-LD processing dropped anything we emitted."""
    duty_n = remedy_n = conseq_n = 0
    for r in ir.rules:
        d, rem, con = _count_duty_roles(r.duties)
        duty_n += d
        remedy_n += rem
        conseq_n += con
    expected = {
        "permission": sum(1 for r in ir.rules if r.modality == "permission"),
        "prohibition": sum(1 for r in ir.rules if r.modality == "prohibition"),
        "obligation": sum(1 for r in ir.rules if r.modality == "obligation"),
        "duty": duty_n,
        "remedy": remedy_n,
        "consequence": conseq_n,
    }
    for name, count in expected.items():
        found = len(list(graph.triples((None, _ODRL[name], None))))
        if found != count:
            raise CompileError(f"graph has {found} odrl:{name} triples, expected {count}")


def compile_ir(ir: PolicyIR) -> tuple[dict[str, Any], rdflib.Graph]:
    """Compile ``ir`` to ``(json_ld, rdf_graph)``; raises :class:`CompileError`."""
    doc = compile_doc(ir)
    graph = jsonld_to_graph(doc)
    _verify_graph(ir, graph)
    return doc, graph
