"""Pure IR pass that materialises implied permissions and carves out prohibitions.

Implements Salas et al. (2509 / 2603) consistent permission carving without
mutating the extractor's source IR:

* When ``ir.action_closure == "permissions_only"``, each permission implies
  narrower actions along ``reasoning.action_parents`` (``odrl:includedIn``).
  Prohibitions are never inferred along the hierarchy.
* Overlapping prohibitions (same action, target, assignee, jointly satisfiable
  constraints) are subtracted from permissions by dropping unconditionally
  prohibited permissions or conjoining the negation of the prohibition's
  constraints. Permissions whose remaining constraint set is unsatisfiable are
  dropped.
* Obligations and duties are copied unchanged.
"""
from __future__ import annotations

from odrl_agent.ir import (
    ACTIONS,
    MAX_CONSTRAINT_DEPTH,
    ConstraintIR,
    ConstraintNode,
    LogicalConstraintIR,
    PolicyIR,
    RuleIR,
    constraint_depth,
)
from odrl_agent.reasoning import _assignee, _same, ancestors, satisfiable

_NEG_OPERATOR: dict[str, str] = {
    "eq": "neq",
    "neq": "eq",
    "gt": "lteq",
    "lteq": "gt",
    "gteq": "lt",
    "lt": "gteq",
    "isAnyOf": "isNoneOf",
    "isNoneOf": "isAnyOf",
}


def _negate_node(node: ConstraintNode) -> ConstraintNode | None:
    if isinstance(node, ConstraintIR):
        neg_op = _NEG_OPERATOR.get(node.operator)
        if neg_op is None:
            return None
        return ConstraintIR(
            left_operand=node.left_operand,
            operator=neg_op,  # type: ignore[arg-type]
            right_operand=node.right_operand,
            value_type=node.value_type,
            span=node.span,
        )
    children: list[ConstraintNode] = []
    for child in node.constraints:
        neg_child = _negate_node(child)
        if neg_child is None:
            return None
        children.append(neg_child)
    if node.operator in ("and", "andSequence"):
        return LogicalConstraintIR(operator="or", constraints=children, span=node.span)
    if node.operator == "or":
        return LogicalConstraintIR(operator="and", constraints=children, span=node.span)
    # xone over 2 branches: (a and b) or (~a and ~b)
    if len(node.constraints) == 2:
        a, b = node.constraints
        na, nb = children
        both = LogicalConstraintIR(operator="and", constraints=[a.model_copy(deep=True), b.model_copy(deep=True)])
        neither = LogicalConstraintIR(operator="and", constraints=[na, nb])
        out = LogicalConstraintIR(operator="or", constraints=[both, neither], span=node.span)
        if constraint_depth(out) <= MAX_CONSTRAINT_DEPTH:
            return out
    return None


def _negate_conjunction(nodes: list[ConstraintNode]) -> ConstraintNode | None:
    if len(nodes) == 1:
        return _negate_node(nodes[0])
    negated: list[ConstraintNode] = []
    for node in nodes:
        neg = _negate_node(node)
        if neg is None:
            return None
        negated.append(neg)
    out = LogicalConstraintIR(operator="or", constraints=negated)
    return out if constraint_depth(out) <= MAX_CONSTRAINT_DEPTH else None


def _slot_key(rule: RuleIR, ir: PolicyIR) -> tuple[str, str, str]:
    return (
        rule.action,
        (rule.target or "").casefold(),
        (_assignee(rule, ir) or "").casefold(),
    )


def _expand_permissions(ir: PolicyIR) -> list[RuleIR]:
    if ir.action_closure != "permissions_only":
        return [r.model_copy(deep=True) for r in ir.rules]
    explicit_perms = {_slot_key(r, ir) for r in ir.rules if r.modality == "permission"}
    materialized_sigs: set[tuple[str, str, str, str]] = set()
    expanded: list[RuleIR] = []
    for rule in ir.rules:
        copied = rule.model_copy(deep=True)
        expanded.append(copied)
        if rule.modality != "permission":
            continue
        for narrow in ACTIONS:
            if narrow == rule.action or rule.action not in ancestors(narrow):
                continue
            narrow_key = (narrow, (rule.target or "").casefold(), (_assignee(rule, ir) or "").casefold())
            if narrow_key in explicit_perms:
                continue
            implied = rule.model_copy(deep=True, update={"action": narrow})
            sig = (*narrow_key, implied.model_dump_json(exclude={"source_spans"}))
            if sig in materialized_sigs:
                continue
            materialized_sigs.add(sig)
            expanded.append(implied)
    return expanded


def carve(ir: PolicyIR) -> PolicyIR:
    """Return a derived :class:`PolicyIR` with overlapping prohibitions carved from permissions."""
    rules = _expand_permissions(ir)
    prohibs = [r for r in rules if r.modality == "prohibition"]
    out_rules: list[RuleIR] = []

    for rule in rules:
        if rule.modality != "permission":
            out_rules.append(rule)
            continue

        carved = rule.model_copy(deep=True)
        dropped = False
        was_carved = False

        for prohib in prohibs:
            if prohib.action != carved.action:
                continue
            if not (_same(carved.target, prohib.target) and _same(_assignee(carved, ir), _assignee(prohib, ir))):
                continue
            perm_nodes = list(carved.constraints) + list(carved.action_refinements)
            prohib_nodes = list(prohib.constraints) + list(prohib.action_refinements)
            if not satisfiable(perm_nodes, prohib_nodes):
                continue
            if not prohib_nodes:
                dropped = True
                break
            if prohib.constraints and not prohib.action_refinements:
                neg = _negate_conjunction(list(prohib.constraints))
                if neg is not None:
                    carved.constraints.append(neg)
                    was_carved = True
            elif prohib.action_refinements and not prohib.constraints:
                neg = _negate_conjunction(list(prohib.action_refinements))
                if neg is not None:
                    carved.action_refinements.append(neg)
                    was_carved = True
            else:
                neg = _negate_conjunction(prohib_nodes)
                if neg is not None:
                    carved.constraints.append(neg)
                    was_carved = True

        if dropped:
            continue
        if was_carved and not satisfiable(list(carved.constraints) + list(carved.action_refinements)):
            continue
        out_rules.append(carved)

    return ir.model_copy(update={"rules": out_rules})
