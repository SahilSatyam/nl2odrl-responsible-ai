"""Checked-in finite knowledge bases and deterministic constraint grounding (Phase 2.1).

Implements Mustafa et al. (2602.19883) per-operand knowledge bases
``KB_l = (C, <=, _||_, gamma)`` completely offline (no network, no model calls):

* ``spatial`` (mereological ``<=``): World, continents, EU, France, Germany, ...
* ``purpose`` (taxonomic ``<=``): Commercial _||_ NonCommercial, Research, ...

Other left operands remain surface strings unless an explicit KB map is
supplied. ``ground_constraints(ir)`` fills ``ConstraintIR.right_concepts``
deterministically without changing ``right_operand``.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from odrl_agent.ir import ConstraintIR, ConstraintNode, DutyIR, LogicalConstraintIR, PolicyIR

OrderKind = Literal["mereological", "taxonomic", "nominal"]
SET_MULTI_OPERATORS = frozenset({"isAnyOf", "isAllOf", "isNoneOf"})
SET_OPERATORS = frozenset({"isA", "hasPart", "isPartOf", "isAnyOf", "isAllOf", "isNoneOf"})


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


@dataclass(frozen=True)
class OperandKB:
    """Finite partial-order + disjointness knowledge base for one left operand."""

    operand: str
    order_kind: OrderKind
    concepts: frozenset[str]
    le: frozenset[tuple[str, str]]
    disjoint: frozenset[tuple[str, str]]
    aliases: dict[str, str]

    def ground(self, raw: str, operator: str) -> list[str] | None:
        """Map ``raw`` (or comma-separated items for multi-value ops) to concept IRIs."""
        if operator in SET_MULTI_OPERATORS:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            if not parts:
                return None
            out: list[str] = []
            for part in parts:
                hit = self.aliases.get(_norm(part))
                if hit is None:
                    return None
                out.append(hit)
            return out
        hit = self.aliases.get(_norm(raw))
        return [hit] if hit is not None else None

    def strictly_under(self, target: str) -> list[str]:
        """Sorted concepts ``x`` such that ``x <= target`` and ``x != target``."""
        return sorted(x for x in self.concepts if x != target and (x, target) in self.le)

    def strictly_above(self, target: str) -> list[str]:
        """Sorted concepts ``x`` such that ``target <= x`` and ``x != target``."""
        return sorted(x for x in self.concepts if x != target and (target, x) in self.le)

    def disjoint_with(self, target: str) -> list[str]:
        """Sorted concepts ``x`` provably disjoint with ``target``."""
        return sorted(x for x in self.concepts if (x, target) in self.disjoint)


def build_kb(
    *,
    operand: str,
    order_kind: OrderKind,
    edges: Sequence[tuple[str, str]] = (),
    disjoint: Sequence[tuple[str, str]] = (),
    aliases: Mapping[str, Sequence[str] | str] | None = None,
    extra_concepts: Sequence[str] = (),
) -> OperandKB:
    """Construct an :class:`OperandKB` with transitive ``<=`` and downward-closed ``_||_``."""
    concepts_set: set[str] = set(extra_concepts)
    for child, parent in edges:
        concepts_set.add(child)
        concepts_set.add(parent)
    for a, b in disjoint:
        concepts_set.add(a)
        concepts_set.add(b)
    if aliases:
        concepts_set.update(aliases.keys())
    concepts = frozenset(concepts_set)

    # Reflexive + transitive closure of <=
    le_set: set[tuple[str, str]] = {(c, c) for c in concepts}
    le_set.update(edges)
    changed = True
    while changed:
        changed = False
        new_pairs: set[tuple[str, str]] = set()
        for a, b in le_set:
            for c, d in le_set:
                if b == c and (a, d) not in le_set:
                    new_pairs.add((a, d))
        if new_pairs:
            le_set.update(new_pairs)
            changed = True
    le = frozenset(le_set)

    # Symmetric + downward-closed disjointness: a _||_ b and x <= a, y <= b => x _||_ y
    downward: dict[str, set[str]] = {c: set() for c in concepts}
    for x, a in le:
        downward[a].add(x)
    disj_set: set[tuple[str, str]] = set()
    for a, b in disjoint:
        for x in downward[a]:
            for y in downward[b]:
                disj_set.add((x, y))
                disj_set.add((y, x))
    overlap = le & disj_set
    if overlap:
        raise ValueError(f"inconsistent KB for {operand!r}: <= overlaps _||_ at {sorted(overlap)[:3]}")

    alias_map: dict[str, str] = {}
    for concept in sorted(concepts):
        alias_map[_norm(concept)] = concept
    if aliases:
        for concept, names in aliases.items():
            seq = (names,) if isinstance(names, str) else tuple(names)
            for name in seq:
                alias_map[_norm(name)] = concept

    return OperandKB(
        operand=operand,
        order_kind=order_kind,
        concepts=concepts,
        le=le,
        disjoint=frozenset(disj_set),
        aliases=alias_map,
    )


def without_disjoint(kb: OperandKB) -> OperandKB:
    """Return a copy of ``kb`` with all disjointness axioms removed (2602 ablation)."""
    return OperandKB(
        operand=kb.operand,
        order_kind=kb.order_kind,
        concepts=kb.concepts,
        le=kb.le,
        disjoint=frozenset(),
        aliases=dict(kb.aliases),
    )


_SP = "urn:nl2odrl:kb:spatial:"
_PU = "urn:nl2odrl:kb:purpose:"

SPATIAL_KB = build_kb(
    operand="spatial",
    order_kind="mereological",
    edges=(
        (_SP + "europe", _SP + "world"),
        (_SP + "eu", _SP + "europe"),
        (_SP + "france", _SP + "eu"),
        (_SP + "germany", _SP + "eu"),
        (_SP + "norway", _SP + "europe"),
        (_SP + "north-america", _SP + "world"),
        (_SP + "canada", _SP + "north-america"),
        (_SP + "us", _SP + "north-america"),
        (_SP + "asia", _SP + "world"),
        (_SP + "japan", _SP + "asia"),
        (_SP + "south-america", _SP + "world"),
        (_SP + "brazil", _SP + "south-america"),
        (_SP + "africa", _SP + "world"),
        (_SP + "kenya", _SP + "africa"),
    ),
    disjoint=(
        (_SP + "europe", _SP + "north-america"),
        (_SP + "europe", _SP + "asia"),
        (_SP + "europe", _SP + "south-america"),
        (_SP + "europe", _SP + "africa"),
        (_SP + "north-america", _SP + "asia"),
        (_SP + "north-america", _SP + "south-america"),
        (_SP + "north-america", _SP + "africa"),
        (_SP + "asia", _SP + "south-america"),
        (_SP + "asia", _SP + "africa"),
        (_SP + "south-america", _SP + "africa"),
        (_SP + "france", _SP + "germany"),
        (_SP + "france", _SP + "norway"),
        (_SP + "germany", _SP + "norway"),
        (_SP + "eu", _SP + "norway"),
        (_SP + "canada", _SP + "us"),
    ),
    aliases={
        _SP + "world": ("world", "global", "earth"),
        _SP + "europe": ("europe",),
        _SP + "eu": (
            "eu",
            "the eu",
            "european union",
            "the european union",
            "https://www.wikidata.org/wiki/q458",
        ),
        _SP + "france": ("france", "fr"),
        _SP + "germany": ("germany", "de"),
        _SP + "norway": ("norway", "no"),
        _SP + "north-america": ("north america",),
        _SP + "canada": ("canada", "ca"),
        _SP + "us": ("us", "usa", "united states", "united states of america"),
        _SP + "asia": ("asia",),
        _SP + "japan": ("japan", "jp"),
        _SP + "south-america": ("south america",),
        _SP + "brazil": ("brazil", "br"),
        _SP + "africa": ("africa",),
        _SP + "kenya": ("kenya", "ke"),
    },
)

PURPOSE_KB = build_kb(
    operand="purpose",
    order_kind="taxonomic",
    edges=(
        (_PU + "marketing", _PU + "commercial"),
        (_PU + "research", _PU + "non-commercial"),
        (_PU + "academic", _PU + "research"),
        (_PU + "education", _PU + "non-commercial"),
        (_PU + "archival", _PU + "non-commercial"),
    ),
    disjoint=(
        (_PU + "commercial", _PU + "non-commercial"),
    ),
    extra_concepts=(_PU + "fraud-detection", _PU + "benchmarking"),
    aliases={
        _PU + "commercial": ("commercial", "commercial use", "for-profit"),
        _PU + "non-commercial": (
            "noncommercial",
            "non-commercial",
            "non commercial",
            "non-commercial use",
        ),
        _PU + "marketing": ("marketing",),
        _PU + "research": (
            "research",
            "r&d",
            "scientific research",
            "https://w3id.org/dpv#researchanddevelopment",
        ),
        _PU + "academic": ("academic", "academic research"),
        _PU + "education": ("education", "educational"),
        _PU + "archival": ("archival", "archive"),
        _PU + "fraud-detection": ("fraud-detection", "fraud detection"),
        _PU + "benchmarking": ("benchmarking",),
    },
)

DEFAULT_KBS: dict[str, OperandKB] = {
    "spatial": SPATIAL_KB,
    "purpose": PURPOSE_KB,
}


def _ground_atom(c: ConstraintIR, kbs: Mapping[str, OperandKB]) -> None:
    kb = kbs.get(c.left_operand)
    if kb is None:
        return
    grounded = kb.ground(c.right_operand, c.operator)
    if grounded is not None:
        c.right_concepts = grounded
    elif c.right_concepts and all(g in kb.concepts for g in c.right_concepts):
        return
    else:
        c.right_concepts = []


def _ground_node(node: ConstraintNode, kbs: Mapping[str, OperandKB]) -> None:
    if isinstance(node, ConstraintIR):
        _ground_atom(node, kbs)
    elif isinstance(node, LogicalConstraintIR):
        for child in node.constraints:
            _ground_node(child, kbs)


def _ground_duty(duty: DutyIR, kbs: Mapping[str, OperandKB]) -> None:
    for node in duty.constraints:
        _ground_node(node, kbs)
    if duty.deadline is not None:
        _ground_atom(duty.deadline, kbs)
    for child in duty.consequences:
        _ground_duty(child, kbs)


def ground_constraints(
    ir: PolicyIR, kbs: Mapping[str, OperandKB] | None = None
) -> PolicyIR:
    """Return a copy of ``ir`` with ``right_concepts`` populated from ``kbs``."""
    active_kbs = DEFAULT_KBS if kbs is None else kbs
    out = ir.model_copy(deep=True)
    for rule in out.rules:
        for node in rule.constraints:
            _ground_node(node, active_kbs)
        for node in rule.action_refinements:
            _ground_node(node, active_kbs)
        for duty in rule.duties:
            _ground_duty(duty, active_kbs)
    return out
