"""Behavioral equivalence: do two policies decide the same access requests alike?

A small deterministic ODRL decision evaluator over the IR (permit iff some
permission applies and no prohibition applies; prohibition wins). It is not a
full ODRL semantics engine (that is the Phase 5 FORCE bridge); it covers
assignee, target, action hierarchy and constraint trees, which is what the
benchmark's gold policies exercise. Duties and obligations do not change an
access decision.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from odrl_agent.grounder import normalize_name
from odrl_agent.reasoning import _numeric, includes

MAX_REQUESTS = 10
ORDERED = ("integer", "decimal", "date", "dateTime", "duration")


def name_key(name: str | None) -> str | None:
    """Comparable form of a party/asset: IRIs reduce to their last segment."""
    if not name:
        return None
    text = name.strip()
    if ":" in text and " " not in text:
        text = text.rstrip("/#").replace("#", "/").split("/")[-1].split(":")[-1]
    return normalize_name(text)


def _holds(node: dict[str, Any], context: dict[str, list[str]]) -> bool:
    if node.get("kind") == "logical":
        results = [_holds(child, context) for child in node["constraints"]]
        op = node["operator"]
        if op == "or":
            return any(results)
        if op == "xone":
            return sum(results) == 1
        return all(results)  # and, andSequence
    given = context.get(node["left_operand"])
    if given is None:
        return False
    vtype, text = given
    op, bound, bound_type = node["operator"], node["right_operand"], node["value_type"]
    if bound_type in ORDERED and vtype in ORDERED:
        left, right = _numeric(vtype, text), _numeric(bound_type, bound)
        if left is None or right is None:
            return False
        return {"eq": left == right, "neq": left != right, "gt": left > right,
                "gteq": left >= right, "lt": left < right, "lteq": left <= right
                }.get(op, False)
    left_s, right_s = normalize_name(text), normalize_name(bound)
    if op in ("isAnyOf", "isAllOf"):
        return left_s in {normalize_name(x) for x in bound.split(",")}
    if op == "isNoneOf":
        return left_s not in {normalize_name(x) for x in bound.split(",")}
    if op == "neq":
        return left_s != right_s
    return left_s == right_s


def _applies(rule: dict[str, Any], policy: dict[str, Any], request: dict[str, Any]) -> bool:
    assignee = name_key(rule.get("assignee") or policy.get("assignee"))
    if assignee is not None and assignee != name_key(request["assignee"]):
        return False
    target = name_key(rule.get("target"))
    if target is not None and target != name_key(request["target"]):
        return False
    if not includes(rule["action"], request["action"]):
        return False
    return all(_holds(c, request.get("context", {})) for c in rule.get("constraints", []))


def decide(policy: dict[str, Any] | None, request: dict[str, Any]) -> bool:
    """True when ``policy`` (an IR dump) permits ``request``."""
    if not policy:
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
    """Contexts that sit on, below and above every bound in ``rule``."""
    atoms = _atoms(rule.get("constraints", []))
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
    requests = requests_for(gold)
    agree = sum(decide(gold, r) == decide(predicted, r) for r in requests)
    return agree, len(requests)
