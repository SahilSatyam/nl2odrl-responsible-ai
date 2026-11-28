"""Behavioral equivalence: do two policies decide the same access requests alike?

Uses the same three-valued KB + Z3 satisfiability oracle as
:mod:`odrl_agent.reasoning`, including ``action_refinements`` and KB set
operators. Duties and obligations do not flip allow/deny (monitored separately via
``duty_recorded``). Offers are refused (``decide`` returns ``False`` and
``agreement`` does not score them as access decisions).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pydantic import TypeAdapter, ValidationError

from odrl_agent.grounder import normalize_name
from odrl_agent.ir import ConstraintIR, ConstraintNode
from odrl_agent.kb import DEFAULT_KBS, SET_OPERATORS
from odrl_agent.reasoning import _numeric, check_satisfiable, includes

MAX_REQUESTS = 10
ORDERED = ("integer", "decimal", "date", "dateTime", "duration")
_NODE_LIST = TypeAdapter(list[ConstraintNode])


def name_key(name: str | None) -> str | None:
    """Comparable form of a party/asset: IRIs reduce to their last segment."""
    if not name:
        return None
    text = name.strip()
    if ":" in text and " " not in text:
        text = text.rstrip("/#").replace("#", "/").split("/")[-1].split(":")[-1]
    return normalize_name(text)


def _operands_in(nodes: list[ConstraintNode]) -> set[str]:
    out: set[str] = set()
    for node in nodes:
        if isinstance(node, ConstraintIR):
            out.add(node.left_operand)
        else:
            out.update(_operands_in(list(node.constraints)))
    return out


def _applies(rule: dict[str, Any], policy: dict[str, Any], request: dict[str, Any]) -> bool:
    assignee = name_key(rule.get("assignee") or policy.get("assignee"))
    if assignee is not None and assignee != name_key(request["assignee"]):
        return False
    target = name_key(rule.get("target"))
    if target is not None and target != name_key(request["target"]):
        return False
    if not includes(rule["action"], request["action"]):
        return False
    raw_nodes = list(rule.get("constraints", [])) + list(rule.get("action_refinements", []))
    if not raw_nodes:
        return True
    try:
        parsed = _NODE_LIST.validate_python(raw_nodes)
    except ValidationError:
        return False

    context = request.get("context", {}) or {}
    req_constraints: list[ConstraintIR] = []
    present_ops: set[str] = set()
    for lo, given in context.items():
        if not isinstance(given, (list, tuple)) or len(given) != 2:
            continue
        vtype, text = given
        try:
            req_constraints.append(
                ConstraintIR(
                    left_operand=lo,  # type: ignore[arg-type]
                    operator="eq",
                    right_operand=str(text),
                    value_type=vtype,  # type: ignore[arg-type]
                )
            )
            present_ops.add(lo)
        except ValidationError:
            continue

    missing = frozenset(_operands_in(parsed) - present_ops)
    return check_satisfiable(parsed, req_constraints, missing_operands=missing) == "sat"


def decide(policy: dict[str, Any] | None, request: dict[str, Any]) -> bool:
    """True when ``policy`` (an IR dump) permits ``request``."""
    if not policy or policy.get("policy_type") == "Offer":
        return False
    rules = policy.get("rules", [])
    if any(_applies(r, policy, request) for r in rules if r["modality"] == "prohibition"):
        return False
    return any(_applies(r, policy, request) for r in rules if r["modality"] == "permission")


# ---- synthetic requests ---------------------------------------------------------


def _shift(vtype: str, text: str, k: int) -> str:
    """``text`` moved by ``k`` smallest steps (1 unit, 1 day, 1 second)."""
    if vtype in ("integer",):
        return str(int(text) + k)
    if vtype == "decimal":
        return str(float(text) + k)
    if vtype == "date":
        return (date.fromisoformat(text) + timedelta(days=k)).isoformat()
    if vtype == "dateTime":
        moment = datetime.fromisoformat(text) + timedelta(seconds=k)  # 3.11+ accepts "Z"
        return moment.isoformat()
    seconds = int(_numeric("duration", text) or 0) + k
    return f"PT{max(seconds, 0)}S"


def _atoms(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("kind") == "logical":
            out.extend(_atoms(node["constraints"]))
        else:
            out.append(node)
    return out


def _contexts(rule: dict[str, Any]) -> list[dict[str, list[str]]]:
    """Contexts that sit on, below and above every bound (plus KB sub/disjoint probes)."""
    atoms = _atoms(list(rule.get("constraints", [])) + list(rule.get("action_refinements", [])))
    if not atoms:
        return [{}]
    contexts = []
    for k in (0, -1, 1):
        ctx: dict[str, list[str]] = {}
        for atom in atoms:
            vtype, bound = atom["value_type"], atom["right_operand"]
            if vtype in ORDERED:
                ctx[atom["left_operand"]] = [vtype, _shift(vtype, bound, k)]
            else:
                ctx[atom["left_operand"]] = ["string", bound if k == 0 else f"{bound}-other"]
        contexts.append(ctx)

    # Probe KB sub-concepts and disjoint concepts for set operators (e.g. isPartOf vs eq).
    base_ctx = dict(contexts[0])
    for atom in atoms:
        lo, op, bound = atom["left_operand"], atom["operator"], atom["right_operand"]
        kb = DEFAULT_KBS.get(lo)
        if kb is None or op not in SET_OPERATORS:
            continue
        concepts = kb.ground(bound, op) or []
        for g in concepts:
            for sub in kb.strictly_under(g)[:1]:
                probe = dict(base_ctx)
                probe[lo] = ["string", sub]
                contexts.append(probe)
            for disj in kb.disjoint_with(g)[:1]:
                probe = dict(base_ctx)
                probe[lo] = ["string", disj]
                contexts.append(probe)
    return contexts


def requests_for(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Up to ``MAX_REQUESTS`` access requests derived from the gold policy."""
    out: list[dict[str, Any]] = []
    rules = [r for r in policy.get("rules", []) if r["modality"] in ("permission", "prohibition")]
    for rule in rules:
        base = {
            "assignee": rule.get("assignee") or policy.get("assignee") or "someone-else",
            "target": rule.get("target") or "some-asset",
            "action": rule["action"],
        }
        for ctx in _contexts(rule):
            out.append({**base, "context": ctx})
    if rules:
        first = rules[0]
        base = {"assignee": first.get("assignee") or "someone-else",
                "target": first.get("target") or "some-asset", "action": first["action"]}
        ctx = _contexts(first)[0]
        out.append({**base, "assignee": "unrelated-party", "context": ctx})
        out.append({**base, "target": "unrelated-asset", "context": ctx})
    seen, unique = set(), []
    for req in out:
        key = repr(sorted((k, repr(v)) for k, v in req.items()))
        if key not in seen:
            seen.add(key)
            unique.append(req)
    # Keep one request per rule+context first, then the negative probes.
    return unique[:MAX_REQUESTS] if len(unique) <= MAX_REQUESTS else (
        unique[: MAX_REQUESTS - 2] + unique[-2:])


def agreement(gold: dict[str, Any], predicted: dict[str, Any] | None) -> tuple[int, int]:
    """(agreeing requests, total requests) of ``predicted`` against ``gold``."""
    if gold.get("policy_type") == "Offer":
        return 0, 0
    requests = requests_for(gold)
    agree = sum(decide(gold, r) == decide(predicted, r) for r in requests)
    return agree, len(requests)
