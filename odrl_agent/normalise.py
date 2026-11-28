"""Pure IR normalisation pass (Salas et al. 2603.12926v1) with bounded size cap (Phase 2.2).

Transforms permissions and prohibitions into disjoint simple rules:
1. Conjoin rule constraints and rewrite ``gteq``/``lteq``/ordered ``neq`` into ``{eq, lt, gt}``.
2. Expand logical operators (``and``, ``andSequence``, ``or``, ``xone``) to DNF.
3. Simplify numeric/ordered intervals per operand, drop empty intervals (``R_bot``),
   and split remaining intervals on constants appearing in partner P/F rules.

Obligations and ``DutyIR`` constraints are NEVER split (2603 Theorem 6): they are
copied unchanged. If the emitted simple permission/prohibition count would exceed
``MAX_NORMAL_RULES`` (64), returns a copy of the source IR with an ``other``
ambiguity (``"normal form exploded"``) without raising.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any

from odrl_agent.ir import (
    ORDERED_VALUE_TYPES,
    AmbiguityFlag,
    ConstraintIR,
    ConstraintNode,
    LogicalConstraintIR,
    PolicyIR,
    RuleIR,
)
from odrl_agent.reasoning import _numeric

MAX_NORMAL_RULES = 64

_NEG_ATOM: dict[str, str] = {
    "eq": "neq",
    "neq": "eq",
    "gt": "lteq",
    "lteq": "gt",
    "lt": "gteq",
    "gteq": "lt",
    "isAnyOf": "isNoneOf",
    "isNoneOf": "isAnyOf",
}


class _ExplosionError(Exception):
    """Internal control flow when normal form exceeds ``MAX_NORMAL_RULES``."""


def _is_ordered(c: ConstraintIR) -> bool:
    return c.value_type in ORDERED_VALUE_TYPES


def _atom_to_dnf(c: ConstraintIR) -> list[list[ConstraintNode]]:
    """Rewrite one atomic constraint into DNF clauses using ``{eq, lt, gt}`` for ordered types."""
    if _is_ordered(c):
        if c.operator == "gteq":
            return [
                [c.model_copy(update={"operator": "eq"})],
                [c.model_copy(update={"operator": "gt"})],
            ]
        if c.operator == "lteq":
            return [
                [c.model_copy(update={"operator": "lt"})],
                [c.model_copy(update={"operator": "eq"})],
            ]
        if c.operator == "neq":
            return [
                [c.model_copy(update={"operator": "lt"})],
                [c.model_copy(update={"operator": "gt"})],
            ]
    return [[c.model_copy(deep=True)]]


def _and_clauses(
    left: list[list[ConstraintNode]],
    right: list[list[ConstraintNode]],
    cap: int,
) -> list[list[ConstraintNode]]:
    out: list[list[ConstraintNode]] = []
    for a in left:
        for b in right:
            out.append([*a, *b])
            if len(out) > cap:
                raise _ExplosionError()
    return out


def _negate_node_dnf(node: ConstraintNode, cap: int) -> list[list[ConstraintNode]] | None:
    """Negate ``node`` into DNF clauses, or ``None`` for non-negatable set operators."""
    if isinstance(node, ConstraintIR):
        neg_op = _NEG_ATOM.get(node.operator)
        if neg_op is None:
            return None
        return _atom_to_dnf(node.model_copy(update={"operator": neg_op}))
    if node.operator in ("and", "andSequence"):
        out: list[list[ConstraintNode]] = []
        for child in node.constraints:
            neg = _negate_node_dnf(child, cap)
            if neg is None:
                return None
            out.extend(neg)
            if len(out) > cap:
                raise _ExplosionError()
        return out
    if node.operator == "or":
        acc: list[list[ConstraintNode]] = [[]]
        for child in node.constraints:
            neg = _negate_node_dnf(child, cap)
            if neg is None:
                return None
            acc = _and_clauses(acc, neg, cap)
        return acc
    # xone over 2 branches: (a and b) or (~a and ~b)
    if node.operator == "xone" and len(node.constraints) == 2:
        a, b = node.constraints
        pos_a, pos_b = _node_to_dnf(a, cap), _node_to_dnf(b, cap)
        neg_a, neg_b = _negate_node_dnf(a, cap), _negate_node_dnf(b, cap)
        if neg_a is None or neg_b is None:
            return None
        both = _and_clauses(pos_a, pos_b, cap)
        neither = _and_clauses(neg_a, neg_b, cap)
        combined = both + neither
        if len(combined) > cap:
            raise _ExplosionError()
        return combined
    return None


def _node_to_dnf(node: ConstraintNode, cap: int) -> list[list[ConstraintNode]]:
    if isinstance(node, ConstraintIR):
        return _atom_to_dnf(node)
    if node.operator in ("and", "andSequence"):
        acc: list[list[ConstraintNode]] = [[]]
        for child in node.constraints:
            acc = _and_clauses(acc, _node_to_dnf(child, cap), cap)
        return acc
    if node.operator == "or":
        out: list[list[ConstraintNode]] = []
        for child in node.constraints:
            out.extend(_node_to_dnf(child, cap))
            if len(out) > cap:
                raise _ExplosionError()
        return out
    # xone: big-OR over (b_k and /\_{j != k} ~b_j); preserve untouched if any branch cannot negate.
    negated = [_negate_node_dnf(child, cap) for child in node.constraints]
    if any(n is None for n in negated):
        return [[node.model_copy(deep=True)]]
    out = []
    for k, child in enumerate(node.constraints):
        branch = _node_to_dnf(child, cap)
        for j, neg in enumerate(negated):
            if j != k and neg is not None:
                branch = _and_clauses(branch, neg, cap)
        out.extend(branch)
        if len(out) > cap:
            raise _ExplosionError()
    return out


def _conj_to_dnf(nodes: list[ConstraintNode], cap: int) -> list[list[ConstraintNode]]:
    if not nodes:
        return [[]]
    acc: list[list[ConstraintNode]] = [[]]
    for node in nodes:
        acc = _and_clauses(acc, _node_to_dnf(node, cap), cap)
    return acc


@dataclass(frozen=True)
class _Bound:
    num: float
    strict: bool
    vt: str
    raw: str


def _tighter_low(a: _Bound | None, b: _Bound) -> _Bound:
    if a is None:
        return b
    if b.num > a.num:
        return b
    if b.num == a.num and b.strict and not a.strict:
        return b
    return a


def _tighter_high(a: _Bound | None, b: _Bound) -> _Bound:
    if a is None:
        return b
    if b.num < a.num:
        return b
    if b.num == a.num and b.strict and not a.strict:
        return b
    return a


def _is_empty_interval(low: _Bound | None, high: _Bound | None) -> bool:
    if low is None or high is None:
        return False
    if low.num > high.num:
        return True
    return low.num == high.num and (low.strict or high.strict)


def _interval_atoms(lo: str, low: _Bound | None, high: _Bound | None) -> list[ConstraintIR]:
    if low is not None and high is not None and low.num == high.num and not low.strict and not high.strict:
        return [ConstraintIR(left_operand=lo, operator="eq", right_operand=low.raw, value_type=low.vt)]  # type: ignore[arg-type]
    out: list[ConstraintIR] = []
    if low is not None:
        out.append(
            ConstraintIR(
                left_operand=lo,
                operator="gt" if low.strict else "gteq",
                right_operand=low.raw,
                value_type=low.vt,  # type: ignore[arg-type]
            )
        )
    if high is not None:
        out.append(
            ConstraintIR(
                left_operand=lo,
                operator="lt" if high.strict else "lteq",
                right_operand=high.raw,
                value_type=high.vt,  # type: ignore[arg-type]
            )
        )
    return out


def _split_interval(
    lo: str,
    atoms: list[ConstraintIR],
    constants: list[tuple[float, str, str]],
) -> list[list[ConstraintIR]] | None:
    """Simplify ``atoms`` for one ordered operand and split on ``constants``.

    Returns ``None`` if the interval is empty (contradictory).
    """
    low: _Bound | None = None
    high: _Bound | None = None
    for c in atoms:
        num = _numeric(c.value_type, c.right_operand)
        if num is None:
            continue
        if c.operator == "eq":
            low = _tighter_low(low, _Bound(num, False, c.value_type, c.right_operand))
            high = _tighter_high(high, _Bound(num, False, c.value_type, c.right_operand))
        elif c.operator == "gt":
            low = _tighter_low(low, _Bound(num, True, c.value_type, c.right_operand))
        elif c.operator == "lt":
            high = _tighter_high(high, _Bound(num, True, c.value_type, c.right_operand))
    if _is_empty_interval(low, high):
        return None
    if low is not None and high is not None and low.num == high.num:
        return [_interval_atoms(lo, low, high)]

    interior = [
        (num, vt, raw)
        for num, vt, raw in constants
        if (low is None or num > low.num) and (high is None or num < high.num)
    ]
    if not interior:
        return [_interval_atoms(lo, low, high)]

    pieces: list[list[ConstraintIR]] = []
    cur_low = low
    for num, vt, raw in interior:
        split_pt = _Bound(num, True, vt, raw)
        pieces.append(_interval_atoms(lo, cur_low, split_pt))
        point = _Bound(num, False, vt, raw)
        pieces.append(_interval_atoms(lo, point, point))
        cur_low = split_pt
    pieces.append(_interval_atoms(lo, cur_low, high))
    return pieces


def _has_symbolic_contradiction(nodes: list[ConstraintNode]) -> bool:
    eqs: dict[str, set[str]] = {}
    neqs: dict[str, set[str]] = {}
    for node in nodes:
        if not isinstance(node, ConstraintIR) or _is_ordered(node):
            continue
        if node.operator == "eq":
            eqs.setdefault(node.left_operand, set()).add(node.right_operand.casefold())
        elif node.operator == "neq":
            neqs.setdefault(node.left_operand, set()).add(node.right_operand.casefold())
    for lo, vals in eqs.items():
        if len(vals) > 1:
            return True
        if vals & neqs.get(lo, set()):
            return True
    return False


def _dedup_nodes(nodes: list[ConstraintNode]) -> list[ConstraintNode]:
    seen: set[str] = set()
    out: list[ConstraintNode] = []
    for n in nodes:
        key = n.model_dump_json(exclude={"span"})
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def _collect_constants(clauses_per_rule: list[list[list[ConstraintNode]]]) -> dict[str, list[tuple[float, str, str]]]:
    by_op: dict[str, dict[float, tuple[str, str]]] = {}
    for clauses in clauses_per_rule:
        for clause in clauses:
            for node in clause:
                if isinstance(node, ConstraintIR) and _is_ordered(node):
                    num = _numeric(node.value_type, node.right_operand)
                    if num is not None:
                        by_op.setdefault(node.left_operand, {}).setdefault(
                            num, (node.value_type, node.right_operand)
                        )
    return {
        lo: [(num, vt, raw) for num, (vt, raw) in sorted(vals.items(), key=lambda kv: kv[0])]
        for lo, vals in by_op.items()
    }


def normalise(ir: PolicyIR, *, cap: int = MAX_NORMAL_RULES) -> PolicyIR:
    """Return the normalised :class:`PolicyIR` (permissions/prohibitions in DNF + split intervals).

    Obligations and ``DutyIR`` constraints are copied unchanged. If the emitted
    simple permission/prohibition rule count would exceed ``cap``, returns a copy
    of ``ir`` with an ``other`` ambiguity (``"normal form exploded"``).
    """
    pf_rules = [r for r in ir.rules if r.modality in ("permission", "prohibition")]
    if not pf_rules:
        return ir.model_copy(deep=True)

    try:
        expanded_pf: list[tuple[RuleIR, list[list[ConstraintNode]]]] = []
        total_dnf = 0
        for rule in pf_rules:
            clauses = _conj_to_dnf(list(rule.constraints), cap)
            total_dnf += len(clauses)
            if total_dnf > cap:
                raise _ExplosionError()
            expanded_pf.append((rule, clauses))

        constants = _collect_constants([clauses for _, clauses in expanded_pf])
        simple_pf_count = 0
        out_rules: list[RuleIR] = []
        pf_idx = 0

        for rule in ir.rules:
            if rule.modality == "obligation":
                out_rules.append(rule.model_copy(deep=True))
                continue

            _orig, clauses = expanded_pf[pf_idx]
            pf_idx += 1

            for clause in clauses:
                ordered_groups: dict[str, list[ConstraintIR]] = {}
                other_nodes: list[ConstraintNode] = []
                for node in clause:
                    if (
                        isinstance(node, ConstraintIR)
                        and _is_ordered(node)
                        and node.operator in ("eq", "gt", "lt")
                    ):
                        ordered_groups.setdefault(node.left_operand, []).append(node)
                    else:
                        other_nodes.append(node)

                if _has_symbolic_contradiction(other_nodes):
                    continue
                other_nodes = _dedup_nodes(other_nodes)

                per_op_splits: list[list[list[ConstraintIR]]] = []
                empty = False
                for lo in sorted(ordered_groups):
                    pieces = _split_interval(lo, ordered_groups[lo], constants.get(lo, []))
                    if pieces is None:
                        empty = True
                        break
                    per_op_splits.append(pieces)
                if empty:
                    continue

                if not per_op_splits:
                    simple_pf_count += 1
                    if simple_pf_count > cap:
                        raise _ExplosionError()
                    out_rules.append(
                        rule.model_copy(deep=True, update={"constraints": other_nodes})
                    )
                    continue

                for combo in product(*per_op_splits):
                    simple_pf_count += 1
                    if simple_pf_count > cap:
                        raise _ExplosionError()
                    flat_ordered: list[ConstraintNode] = [
                        atom for piece in combo for atom in piece
                    ]
                    out_rules.append(
                        rule.model_copy(
                            deep=True,
                            update={"constraints": [*flat_ordered, *other_nodes]},
                        )
                    )
    except _ExplosionError:
        already = any(
            a.kind == "other" and a.description == "normal form exploded"
            for a in ir.ambiguities
        )
        ambiguities = list(ir.ambiguities)
        if not already:
            ambiguities.append(AmbiguityFlag(kind="other", description="normal form exploded"))
        return ir.model_copy(deep=True, update={"ambiguities": ambiguities})

    return ir.model_copy(deep=True, update={"rules": out_rules})
