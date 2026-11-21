"""Symbolic conflict and satisfiability checks over a :class:`PolicyIR` (Phase 2.2).

Three deterministic analyses, Python + Z3 only (no OWL reasoner):

* **Unsatisfiable constraints** - each rule's (and duty's) constraint tree is
  encoded in Z3 with operand domain axioms (counts and durations are
  non-negative, percentages lie in 0..100). ``dateTime > 2027-01-01 AND
  dateTime < 2026-01-01`` or ``count < 0`` can never hold.
* **Deontic conflicts** - a permission and a prohibition overlap when they can
  apply to the same request: same target and assignee (a missing one means
  "any"), the prohibited action equals or includes the permitted one per the
  ``odrl:includedIn`` hierarchy in ``schemas/odrl-2.2.ttl``, and their
  constraints are jointly satisfiable. Without a ``conflict`` strategy that
  overlap is an undetected conflict. An obligation to do what is prohibited is
  a conflict whatever the strategy, because ODRL's strategy only orders
  permissions against prohibitions.
* **Action-hierarchy contradictions** are the hierarchy case above (e.g.
  prohibit ``use`` while permitting the narrower ``distribute``).

Operators without an order or equality meaning in Z3 (``isA``, ``hasPart``,
``isAnyOf``, ...) are left unconstrained, so the engine may miss an
unsatisfiable set relation but never reports a false one.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import z3

from odrl_agent.ir import ConstraintIR, LogicalConstraintIR, PolicyIR, RuleIR

_ODRL_NS = "http://www.w3.org/ns/odrl/2/"
_ONTOLOGY = Path(__file__).resolve().parent.parent / "schemas" / "odrl-2.2.ttl"
_DURATION = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)
_NON_NEGATIVE = (
    "count", "elapsedTime", "delayPeriod", "meteredTime", "timeInterval", "payAmount",
    "absoluteSize", "relativeSize", "resolution", "percentage",
)  # fmt: skip
_NUMERIC_TYPES = ("integer", "decimal", "date", "dateTime", "duration")

UNSATISFIABLE = "UNSATISFIABLE_CONSTRAINT"
DEONTIC_CONFLICT = "DEONTIC_CONFLICT"
HIERARCHY_CONFLICT = "ACTION_HIERARCHY_CONFLICT"
OBLIGATION_CONFLICT = "OBLIGATION_PROHIBITED"


@lru_cache(maxsize=1)
def action_parents() -> dict[str, frozenset[str]]:
    """Direct ``odrl:includedIn`` parents per action, from the bundled ontology."""
    import rdflib

    graph = rdflib.Graph()
    graph.parse(_ONTOLOGY, format="turtle")
    included = rdflib.URIRef(_ODRL_NS + "includedIn")
    parents: dict[str, set[str]] = {}
    for child, _p, parent in graph.triples((None, included, None)):
        c, p = str(child), str(parent)
        if c.startswith(_ODRL_NS) and p.startswith(_ODRL_NS):
            parents.setdefault(c[len(_ODRL_NS):], set()).add(p[len(_ODRL_NS):])
    return {k: frozenset(v) for k, v in parents.items()}


def ancestors(action: str) -> frozenset[str]:
    """All actions that include ``action`` (transitive), excluding itself."""
    parents = action_parents()
    seen: set[str] = set()
    stack = list(parents.get(action, ()))
    while stack:
        current = stack.pop()
        if current in seen or current == action:
            continue
        seen.add(current)
        stack.extend(parents.get(current, ()))
    return frozenset(seen)


def includes(broad: str, narrow: str) -> bool:
    """True when action ``broad`` equals or includes ``narrow``."""
    return broad == narrow or broad in ancestors(narrow)


def _duration_seconds(text: str) -> float:
    m = _DURATION.match(text)
    if not m:
        raise ValueError(f"bad duration {text!r}")
    y, mo, w, d, h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return (((y * 365 + mo * 30 + w * 7 + d) * 24 + h) * 60 + mi) * 60 + s


def _epoch(value_type: str, text: str) -> float:
    if value_type == "date":
        moment = datetime.combine(date.fromisoformat(text), datetime.min.time())
    else:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def _numeric(value_type: str, text: str) -> float | None:
    if value_type in ("integer", "decimal"):
        return float(text)
    if value_type in ("date", "dateTime"):
        return _epoch(value_type, text)
    if value_type == "duration":
        return _duration_seconds(text)
    return None


class _Encoder:
    """Shared Z3 variables, one per (scope, left operand)."""

    def __init__(self, scope: str = "") -> None:
        self.scope = scope
        self.reals: dict[str, z3.ArithRef] = {}
        self.strings: dict[str, z3.SeqRef] = {}
        self.axioms: list[z3.BoolRef] = []
        self._fresh = 0

    def real(self, operand: str) -> z3.ArithRef:
        if operand not in self.reals:
            var = z3.Real(f"{self.scope}{operand}")
            self.reals[operand] = var
            if operand in _NON_NEGATIVE:
                self.axioms.append(var >= 0)
            if operand == "percentage":
                self.axioms.append(var <= 100)
        return self.reals[operand]

    def string(self, operand: str) -> z3.SeqRef:
        if operand not in self.strings:
            self.strings[operand] = z3.String(f"{self.scope}{operand}:s")
        return self.strings[operand]

    def fresh(self) -> z3.BoolRef:
        self._fresh += 1
        return z3.Bool(f"{self.scope}free{self._fresh}")

    def atom(self, c: ConstraintIR) -> z3.BoolRef:
        op = c.operator
        number = _numeric(c.value_type, c.right_operand) if c.value_type in _NUMERIC_TYPES else None
        if number is not None:
            var, val = self.real(c.left_operand), z3.RealVal(repr(number))
            table = {
                "eq": var == val, "neq": var != val, "gt": var > val,
                "gteq": var >= val, "lt": var < val, "lteq": var <= val,
            }  # fmt: skip
            return table.get(op, self.fresh())
        if op in ("eq", "neq"):
            var, val = self.string(c.left_operand), z3.StringVal(c.right_operand)
            return var == val if op == "eq" else var != val
        return self.fresh()

    def node(self, n: ConstraintIR | LogicalConstraintIR) -> z3.BoolRef:
        if isinstance(n, ConstraintIR):
            return self.atom(n)
        parts = [self.node(child) for child in n.constraints]
        if n.operator in ("and", "andSequence"):
            return z3.And(*parts)
        if n.operator == "or":
            return z3.Or(*parts)
        # xone: exactly one holds
        return z3.PbEq([(p, 1) for p in parts], 1)

    def all(self, nodes: list) -> z3.BoolRef:
        return z3.And(*[self.node(n) for n in nodes]) if nodes else z3.BoolVal(True)


def satisfiable(*constraint_lists: list) -> bool:
    """True when the conjunction of every list's constraints can hold."""
    enc = _Encoder()
    formula = z3.And(*[enc.all(nodes) for nodes in constraint_lists])
    solver = z3.Solver()
    solver.set("timeout", 2000)
    solver.add(formula, *enc.axioms)
    # ``unknown`` (timeout) is treated as satisfiable: never invent a conflict.
    return solver.check() != z3.unsat


@dataclass
class Finding:
    code: str
    detail: str
    rules: list[int] = field(default_factory=list)
    path: str = ""


@dataclass
class ReasoningReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def conflicts(self) -> list[Finding]:
        return [f for f in self.findings if f.code != UNSATISFIABLE]

    @property
    def unsatisfiable(self) -> list[Finding]:
        return [f for f in self.findings if f.code == UNSATISFIABLE]

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "conflicts": [asdict(f) for f in self.conflicts],
            "unsatisfiable": [asdict(f) for f in self.unsatisfiable],
        }

    def messages(self) -> list[str]:
        return [f"reasoning: {f.code} {f.path}: {f.detail}" for f in self.findings]


def _same(a: str | None, b: str | None) -> bool:
    """Two party/asset slots can denote the same entity (``None`` = any)."""
    return a is None or b is None or a.casefold() == b.casefold()


def _assignee(rule: RuleIR, ir: PolicyIR) -> str | None:
    return rule.assignee or ir.assignee


def analyze(ir: PolicyIR) -> ReasoningReport:
    """Run every check on ``ir``; findings are empty for a consistent policy."""
    report = ReasoningReport()
    for i, rule in enumerate(ir.rules):
        all_constraints = list(rule.constraints) + list(rule.action_refinements)
        if all_constraints and not satisfiable(all_constraints):
            report.findings.append(
                Finding(UNSATISFIABLE, f"{rule.modality} {rule.action!r} can never apply",
                        [i], f"rules.{i}.constraints")
            )  # fmt: skip
        for j, duty in enumerate(rule.duties):
            if duty.constraints and not satisfiable(list(duty.constraints)):
                report.findings.append(
                    Finding(UNSATISFIABLE, f"duty {duty.action!r} can never be fulfilled",
                            [i], f"rules.{i}.duties.{j}.constraints")
                )  # fmt: skip

    def overlap(a: RuleIR, b: RuleIR) -> bool:
        return (
            _same(a.target, b.target)
            and _same(_assignee(a, ir), _assignee(b, ir))
            and satisfiable(
                list(a.constraints) + list(a.action_refinements),
                list(b.constraints) + list(b.action_refinements),
            )
        )

    for i, perm in enumerate(ir.rules):
        if perm.modality != "permission":
            continue
        for j, prohib in enumerate(ir.rules):
            if prohib.modality != "prohibition" or not includes(prohib.action, perm.action):
                continue
            if ir.conflict_strategy is not None or not overlap(perm, prohib):
                continue
            code = DEONTIC_CONFLICT if perm.action == prohib.action else HIERARCHY_CONFLICT
            report.findings.append(
                Finding(
                    code,
                    f"permission {perm.action!r} (rule {i}) overlaps prohibition "
                    f"{prohib.action!r} (rule {j}) with no conflict strategy",
                    [i, j],
                    f"rules.{i}/rules.{j}",
                )
            )
    obligations: list[tuple[int, str, RuleIR | Any, str | None, list]] = []
    for i, rule in enumerate(ir.rules):
        if rule.modality == "obligation":
            obligations.append((i, rule.action, rule, rule.target, list(rule.constraints)))
        for duty in rule.duties:
            obligations.append(
                (i, duty.action, duty, duty.target or rule.target, list(duty.constraints))
            )
    for i, action, who, target, constraints in obligations:
        assignee = getattr(who, "assignee", None) or ir.assignee
        for j, prohib in enumerate(ir.rules):
            if prohib.modality != "prohibition" or not includes(prohib.action, action):
                continue
            if not (_same(target, prohib.target) and _same(assignee, _assignee(prohib, ir))):
                continue
            if satisfiable(constraints, list(prohib.constraints)):
                report.findings.append(
                    Finding(
                        OBLIGATION_CONFLICT,
                        f"duty/obligation {action!r} (rule {i}) is prohibited by rule {j}",
                        [i, j],
                        f"rules.{i}/rules.{j}",
                    )
                )
    return report
