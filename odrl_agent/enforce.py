"""Synchronous ODRL runtime enforcement adapter (Phase 3.4, ODRE Algorithm 1).

Consumes ONLY a ``compile_doc`` output paired with its :class:`PolicyIR`
(verified by ``urn:nl2odrl:policy:`` uid prefix and canonical-JSON SHA-256)
plus a runtime context map ``M`` (and optional action handlers ``F``):

* Interprets numeric / ``date`` / ``dateTime`` / ``duration`` comparison
  operators (``lt``, ``lteq``, ``eq``, ``neq``, ``gt``, ``gteq``) and logical
  operators (``and``, ``andSequence``, ``or``, ``xone``).
* Evaluates KB-backed set operators (``isA``, ``isPartOf``, ``hasPart``,
  ``isAnyOf``, ``isAllOf``, ``isNoneOf``) via :mod:`odrl_agent.kb`. Per
  Mustafa et al. (2602.19883 Def. 11), an unmapped runtime value never
  satisfies a constraint (default deny).
* Unknown or abstract actions (with no registered handler) return
  ``(action, action)`` and are not executed.
* Precondition duties on satisfied permissions are listed as
  ``(duty_action, "pending_precondition")`` and are never executed.
* No Jinja, no second policy language, no ``Extractor`` import, no HTTP.
"""
from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from odrl_agent.compiler import POLICY_PREFIX, XSD, CompileError, canonical_json, compile_doc
from odrl_agent.grounder import normalize_name
from odrl_agent.ir import VALUE_TYPES, PolicyIR
from odrl_agent.kb import DEFAULT_KBS, SET_OPERATORS, OperandKB
from odrl_agent.reasoning import _numeric, includes


class EnforcementError(ValueError):
    """Raised when ``doc`` fails IR/hash verification or is an Offer."""


def _doc_hash(doc: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def verify_compiled_doc(doc: Any, ir: PolicyIR) -> None:
    """Verify ``doc`` came unmodified from ``compile_doc(ir)`` and is enforceable."""
    if not isinstance(doc, Mapping):
        raise EnforcementError("compiled document must be a mapping")
    uid = doc.get("uid")
    if not isinstance(uid, str) or not uid.startswith(POLICY_PREFIX):
        raise EnforcementError(f"document uid must start with {POLICY_PREFIX!r}")
    try:
        expected = compile_doc(ir)
    except CompileError as exc:
        raise EnforcementError(f"PolicyIR cannot be compiled: {exc}") from exc
    if _doc_hash(doc) != _doc_hash(expected):
        raise EnforcementError("compiled document hash does not match PolicyIR")
    if ir.policy_type == "Offer" or doc.get("type") == "Offer":
        raise EnforcementError("not an agreement: Offer policies cannot be enforced")


def _norm_str(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _entity_key(name: str | None) -> str | None:
    if not name:
        return None
    text = str(name).strip()
    if ":" in text and " " not in text:
        text = text.rstrip("/#").replace("#", "/").split("/")[-1].split(":")[-1]
    return normalize_name(text)


def _action_name(rule: Mapping[str, Any]) -> str:
    act = rule.get("action")
    if isinstance(act, str):
        return act
    if isinstance(act, Mapping):
        val = act.get("rdf:value")
        if isinstance(val, Mapping):
            iri = str(val.get("@id", ""))
            return iri.removeprefix("odrl:")
    return str(act or "")


def _refinements(rule: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    act = rule.get("action")
    if isinstance(act, Mapping):
        refs = act.get("refinement")
        if isinstance(refs, list):
            return [r for r in refs if isinstance(r, Mapping)]
    return []


def _extract_rhs(raw_ro: Any, context: Mapping[str, Any]) -> tuple[str, str] | None:
    if isinstance(raw_ro, Mapping):
        if "@id" in raw_ro:
            vtype, raw = "iri", str(raw_ro["@id"])
        else:
            xsd = str(raw_ro.get("@type", ""))
            vtype = xsd.removeprefix(XSD) if xsd.startswith(XSD) else "string"
            raw = str(raw_ro.get("@value", ""))
    else:
        vtype, raw = "string", str(raw_ro)

    if raw.startswith("[=") and raw.endswith("]"):
        key = raw[2:-1].strip()
        if key not in context:
            return None
        val = context[key]
        if isinstance(val, (list, tuple)) and len(val) == 2 and str(val[0]) in VALUE_TYPES:
            return str(val[0]), str(val[1])
        return vtype, str(val)
    return vtype, raw


def _extract_lhs(
    left_operand: str,
    expected_vtype: str,
    context: Mapping[str, Any],
    now: datetime | date | str | None,
) -> tuple[str, str] | None:
    if left_operand in context:
        val = context[left_operand]
    elif left_operand == "dateTime" and now is not None:
        val = now
    else:
        return None

    if val is None:
        return None
    if isinstance(val, (list, tuple)) and len(val) == 2 and str(val[0]) in VALUE_TYPES:
        return str(val[0]), str(val[1])
    if isinstance(val, datetime):
        return "dateTime", val.isoformat()
    if isinstance(val, date):
        return "date", val.isoformat()
    if isinstance(val, bool):
        return "boolean", ("true" if val else "false")
    if isinstance(val, int):
        return ("decimal" if expected_vtype == "decimal" else "integer"), str(val)
    if isinstance(val, float):
        return "decimal", str(val)
    raw = str(val)
    if expected_vtype in ("date", "dateTime"):
        return ("dateTime" if "T" in raw else "date"), raw
    if expected_vtype in ("integer", "decimal", "duration", "boolean", "iri"):
        return expected_vtype, raw
    return "string", raw


def _eval_atomic(
    node: Mapping[str, Any],
    context: Mapping[str, Any],
    kbs: Mapping[str, OperandKB],
    now: datetime | date | str | None,
) -> bool:
    left_op = str(node.get("leftOperand", ""))
    op = str(node.get("operator", ""))
    rhs = _extract_rhs(node.get("rightOperand"), context)
    if rhs is None:
        return False
    rhs_vtype, rhs_raw = rhs

    lhs = _extract_lhs(left_op, rhs_vtype, context, now)
    if lhs is None:
        return False
    lhs_vtype, lhs_raw = lhs

    if left_op in kbs:
        kb = kbs[left_op]
        lhs_concepts = kb.ground(lhs_raw, "eq")
        rhs_concepts = kb.ground(rhs_raw, op)
        if not lhs_concepts or not rhs_concepts:
            return False
        lhs_c = lhs_concepts[0]
        if op == "eq":
            return lhs_c == rhs_concepts[0]
        if op == "neq":
            return lhs_c != rhs_concepts[0]
        if op in ("isA", "isPartOf"):
            return (lhs_c, rhs_concepts[0]) in kb.le
        if op == "hasPart":
            return (rhs_concepts[0], lhs_c) in kb.le
        if op == "isAnyOf":
            return any((lhs_c, r) in kb.le for r in rhs_concepts)
        if op == "isAllOf":
            return all((lhs_c, r) in kb.le for r in rhs_concepts)
        if op == "isNoneOf":
            return all((lhs_c, r) not in kb.le for r in rhs_concepts)
        return False

    if op in SET_OPERATORS:
        if op == "isAnyOf":
            choices = {_norm_str(p) for p in rhs_raw.split(",") if p.strip()}
            return _norm_str(lhs_raw) in choices
        if op == "isNoneOf":
            choices = {_norm_str(p) for p in rhs_raw.split(",") if p.strip()}
            return _norm_str(lhs_raw) not in choices
        return False

    if rhs_vtype in ("integer", "decimal", "date", "dateTime", "duration"):
        try:
            l_num = _numeric(lhs_vtype, lhs_raw)
            r_num = _numeric(rhs_vtype, rhs_raw)
        except ValueError:
            return False
        if l_num is None or r_num is None:
            return False
        if op == "eq":
            return l_num == r_num
        if op == "neq":
            return l_num != r_num
        if op == "lt":
            return l_num < r_num
        if op == "lteq":
            return l_num <= r_num
        if op == "gt":
            return l_num > r_num
        if op == "gteq":
            return l_num >= r_num
        return False

    l_cmp = _norm_str(lhs_raw) if rhs_vtype == "string" else lhs_raw
    r_cmp = _norm_str(rhs_raw) if rhs_vtype == "string" else rhs_raw
    if op == "eq":
        return l_cmp == r_cmp
    if op == "neq":
        return l_cmp != r_cmp
    return False


def _eval_node(
    node: Mapping[str, Any],
    context: Mapping[str, Any],
    kbs: Mapping[str, OperandKB],
    now: datetime | date | str | None,
) -> bool:
    kind = node.get("type")
    if kind == "LogicalConstraint":
        if "and" in node:
            return all(_eval_node(c, context, kbs, now) for c in node["and"])
        if "andSequence" in node:
            seq = node["andSequence"]
            items = seq["@list"] if isinstance(seq, Mapping) and "@list" in seq else seq
            return all(_eval_node(c, context, kbs, now) for c in items)
        if "or" in node:
            return any(_eval_node(c, context, kbs, now) for c in node["or"])
        if "xone" in node:
            return sum(1 for c in node["xone"] if _eval_node(c, context, kbs, now)) == 1
        return False
    return _eval_atomic(node, context, kbs, now)


def _rule_matches_context(
    rule: Mapping[str, Any],
    doc: Mapping[str, Any],
    context: Mapping[str, Any],
    kbs: Mapping[str, OperandKB],
    now: datetime | date | str | None,
) -> bool:
    if "assignee" in context:
        rule_assignee = rule.get("assignee") or doc.get("assignee")
        if rule_assignee is not None and _entity_key(str(rule_assignee)) != _entity_key(str(context["assignee"])):
            return False
    if "target" in context:
        rule_target = rule.get("target")
        if rule_target is not None and _entity_key(str(rule_target)) != _entity_key(str(context["target"])):
            return False
    if "action" in context:
        req_action = str(context["action"])
        if not includes(_action_name(rule), req_action):
            return False

    nodes: list[Mapping[str, Any]] = []
    if isinstance(rule.get("constraint"), list):
        nodes.extend(c for c in rule["constraint"] if isinstance(c, Mapping))
    nodes.extend(_refinements(rule))
    return all(_eval_node(n, context, kbs, now) for n in nodes)


def _invoke_handler(
    fn: Callable[..., Any],
    rule: Mapping[str, Any],
    context: Mapping[str, Any],
) -> Any:
    try:
        sig = inspect.signature(fn)
        positional = [
            p
            for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    except (ValueError, TypeError):
        return fn(rule, context)
    if has_varargs or len(positional) >= 2:
        return fn(rule, context)
    if len(positional) == 1:
        return fn(context)
    return fn()


def enforce(
    doc: dict[str, Any],
    ir: PolicyIR,
    context: Mapping[str, Any] | None = None,
    *,
    handlers: Mapping[str, Callable[..., Any]] | None = None,
    kbs: Mapping[str, OperandKB] | None = None,
    now: datetime | date | str | None = None,
) -> list[tuple[str, Any]]:
    """Evaluate compiled ``doc`` against ``context`` and return ODRE Usage Decisions.

    Verifies ``doc`` against ``compile_doc(ir)`` (uid prefix + hash match) and
    refuses ``Offer`` policies. Returns ``[(action, result), ...]`` where
    unsupported actions yield ``(action, action)`` and precondition duties on
    satisfied permissions yield ``(duty_action, "pending_precondition")``.
    """
    verify_compiled_doc(doc, ir)
    ctx = dict(context or {})
    active_kbs = DEFAULT_KBS if kbs is None else kbs

    prohibited: set[str] = set()
    for prohib in doc.get("prohibition", []) or []:
        if isinstance(prohib, Mapping) and _rule_matches_context(prohib, doc, ctx, active_kbs, now):
            prohibited.add(_action_name(prohib))

    decisions: list[tuple[str, Any]] = []
    for perm in doc.get("permission", []) or []:
        if not isinstance(perm, Mapping):
            continue
        if not _rule_matches_context(perm, doc, ctx, active_kbs, now):
            continue
        action = _action_name(perm)
        requested_action = str(ctx["action"]) if "action" in ctx else action
        if action in prohibited or requested_action in prohibited:
            continue
        if handlers and action in handlers:
            result = _invoke_handler(handlers[action], perm, ctx)
        else:
            result = action
        decisions.append((action, result))
        for duty in perm.get("duty", []) or []:
            if isinstance(duty, Mapping):
                decisions.append((_action_name(duty), "pending_precondition"))
    return decisions
