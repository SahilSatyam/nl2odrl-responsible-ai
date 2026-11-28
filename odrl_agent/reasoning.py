"""Symbolic conflict and satisfiability checks over a :class:`PolicyIR` (Phase 2.1).

Three deterministic analyses, Python + Z3 only (no OWL reasoner):

* **Unsatisfiable constraints** - each rule's (and duty's) constraint tree is
  encoded in Z3 with operand domain axioms (counts and durations are
  non-negative, percentages lie in 0..100) and KB-grounded denotations
  (``spatial``, ``purpose``, or caller-supplied KBs). ``dateTime > 2027-01-01
  AND dateTime < 2026-01-01`` or ``count < 0`` can never hold.
* **Three-valued KB verdicts** - when a KB-backed operand has an unmapped
  concept (``gamma(v) = bot``), an ungrounded set operator, a Z3 timeout
  (``unknown``), or a ``xone`` branch that is compatible without a provably
  disjoint sibling, the verdict is ``UNKNOWN_CONSTRAINT`` (held for review,
  never auto-published, never a false conflict).
* **Deontic conflicts** - a permission and a prohibition overlap when they can
  apply to the same request: same target and assignee (a missing one means
  "any"), the same action (exact equality), and jointly satisfiable
  constraints. Setting ``conflict_strategy`` is emitted on the ODRL document
  but does not suppress overlap findings. An obligation or duty to do an
  action that is explicitly prohibited on the same target/assignee is also a
  conflict (``OBLIGATION_PROHIBITED``).
"""
from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import z3

from odrl_agent.ir import ConstraintIR, ConstraintNode, LogicalConstraintIR, PolicyIR, RuleIR
from odrl_agent.kb import DEFAULT_KBS, SET_OPERATORS, OperandKB, ground_constraints

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
UNKNOWN_CONSTRAINT = "UNKNOWN_CONSTRAINT"
DEONTIC_CONFLICT = "DEONTIC_CONFLICT"
HIERARCHY_CONFLICT = "ACTION_HIERARCHY_CONFLICT"
OBLIGATION_CONFLICT = "OBLIGATION_PROHIBITED"

Verdict3 = Literal["sat", "unsat", "unknown"]


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


def _resolve_kb_concepts(c: ConstraintIR, kb: OperandKB) -> list[str] | None:
    if c.right_concepts and all(g in kb.concepts for g in c.right_concepts):
        return list(c.right_concepts)
    return kb.ground(c.right_operand, c.operator)


def _is_indeterminate_atom(
    c: ConstraintIR,
    kbs: Mapping[str, OperandKB],
    missing_operands: frozenset[str],
) -> bool:
    if c.left_operand in missing_operands:
        return False
    if c.left_operand in kbs:
        concepts = _resolve_kb_concepts(c, kbs[c.left_operand])
        if not concepts:
            return True
        return c.operator not in ("eq", "neq", *SET_OPERATORS)
    if _numeric(c.value_type, c.right_operand) is not None:
        return c.operator not in ("eq", "neq", "gt", "gteq", "lt", "lteq")
    return c.operator not in ("eq", "neq")


def _operands_in_node(node: ConstraintNode) -> set[str]:
    if isinstance(node, ConstraintIR):
        return {node.left_operand}
    out: set[str] = set()
    for child in node.constraints:
        out.update(_operands_in_node(child))
    return out


def _uses_kb_or_set(node: ConstraintNode, kbs: Mapping[str, OperandKB]) -> bool:
    if isinstance(node, ConstraintIR):
        return node.left_operand in kbs or node.operator in SET_OPERATORS
    return any(_uses_kb_or_set(child, kbs) for child in node.constraints)


def _needs_split(
    node: ConstraintNode,
    kbs: Mapping[str, OperandKB],
    missing_operands: frozenset[str],
) -> bool:
    if isinstance(node, ConstraintIR):
        return _is_indeterminate_atom(node, kbs, missing_operands)
    if node.operator == "xone":
        return True
    return any(_needs_split(child, kbs, missing_operands) for child in node.constraints)


class _Encoder:
    """Shared Z3 variables, one per (scope, left operand)."""

    def __init__(
        self,
        scope: str = "",
        kbs: Mapping[str, OperandKB] | None = None,
        missing_operands: frozenset[str] = frozenset(),
    ) -> None:
        self.scope = scope
        self.kbs = DEFAULT_KBS if kbs is None else kbs
        self.missing_operands = missing_operands
        self.reals: dict[str, z3.ArithRef] = {}
        self.strings: dict[str, z3.SeqRef] = {}
        self.kb_envs: dict[str, tuple[z3.FuncDeclRef, z3.ExprRef, dict[str, z3.ExprRef]]] = {}
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

    def kb_env(self, operand: str) -> tuple[z3.FuncDeclRef, z3.ExprRef, dict[str, z3.ExprRef]]:
        if operand not in self.kb_envs:
            kb = self.kbs[operand]
            sort = z3.DeclareSort(f"{self.scope}KB_{operand}")
            le = z3.Function(f"{self.scope}{operand}__le", sort, sort, z3.BoolSort())
            x = z3.Const(f"{self.scope}{operand}__x", sort)
            concepts = sorted(kb.concepts)
            k_map = {c: z3.Const(f"{self.scope}{operand}__c{idx}", sort) for idx, c in enumerate(concepts)}
            if len(k_map) >= 2:
                self.axioms.append(z3.Distinct(*k_map.values()))
            self.axioms.append(le(x, x))
            for a in concepts:
                self.axioms.append(le(k_map[a], k_map[a]))
                self.axioms.append(
                    z3.Implies(
                        x == k_map[a],
                        z3.And(*[le(x, k_map[b]) == ((a, b) in kb.le) for b in concepts]),
                    )
                )
                self.axioms.append(
                    z3.Implies(
                        x == k_map[a],
                        z3.And(*[le(k_map[b], x) == ((b, a) in kb.le) for b in concepts]),
                    )
                )
            for a, b in kb.le:
                self.axioms.append(le(k_map[a], k_map[b]))
                if a != b:
                    self.axioms.append(z3.Implies(le(x, k_map[a]), le(x, k_map[b])))
                    self.axioms.append(z3.Implies(le(k_map[b], x), le(k_map[a], x)))
            for a, b in kb.disjoint:
                if a <= b:
                    self.axioms.append(z3.Not(z3.And(le(x, k_map[a]), le(x, k_map[b]))))
            self.kb_envs[operand] = (le, x, k_map)
        return self.kb_envs[operand]

    def fresh(self) -> z3.BoolRef:
        self._fresh += 1
        return z3.Bool(f"{self.scope}free{self._fresh}")

    def atom(self, c: ConstraintIR) -> z3.BoolRef:
        if c.left_operand in self.missing_operands:
            return z3.BoolVal(False)
        op = c.operator
        if c.left_operand in self.kbs:
            kb = self.kbs[c.left_operand]
            concepts = _resolve_kb_concepts(c, kb)
            if not concepts:
                return self.fresh()
            le, x, k_map = self.kb_env(c.left_operand)
            if op == "eq":
                return x == k_map[concepts[0]]
            if op == "neq":
                return x != k_map[concepts[0]]
            if op in ("isA", "isPartOf"):
                return le(x, k_map[concepts[0]])
            if op == "hasPart":
                return le(k_map[concepts[0]], x)
            if op == "isAnyOf":
                return z3.Or(*[le(x, k_map[g]) for g in concepts])
            if op == "isAllOf":
                return z3.And(*[le(x, k_map[g]) for g in concepts])
            if op == "isNoneOf":
                return z3.And(*[z3.Not(le(x, k_map[g])) for g in concepts])
            return self.fresh()

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

    def node(self, n: ConstraintNode) -> z3.BoolRef:
        if isinstance(n, ConstraintIR):
            return self.atom(n)
        parts = [self.node(child) for child in n.constraints]
        if n.operator in ("and", "andSequence"):
            return z3.And(*parts)
        if n.operator == "or":
            return z3.Or(*parts)
        return z3.PbEq([(p, 1) for p in parts], 1)

    def all(self, nodes: list[ConstraintNode]) -> z3.BoolRef:
        return z3.And(*[self.node(n) for n in nodes]) if nodes else z3.BoolVal(True)


#: Z3's default context is process-global and not thread-safe: concurrent
#: expression building crashes with "ASSERTION VIOLATION ... unreachable" or an
#: access violation (seen with 4 benchmark workers, and possible under the
#: threaded service). Every Z3 object is created inside ``check_satisfiable``,
#: so one lock covers it, provided the objects are also *freed* under the lock:
#: their destructors call ``Z3_dec_ref`` on the same global context.
_Z3_LOCK = threading.Lock()


def _z3_check(
    nodes: list[ConstraintNode],
    kbs: Mapping[str, OperandKB],
    missing_operands: frozenset[str],
) -> Verdict3:
    enc = _Encoder(kbs=kbs, missing_operands=missing_operands)
    formula = enc.all(nodes)
    solver = z3.Solver()
    solver.set("timeout", 2000)
    solver.add(formula, *enc.axioms)
    res = solver.check()
    if res == z3.unsat:
        return "unsat"
    if res == z3.sat:
        return "sat"
    return "unknown"


def _flatten_conj(nodes: list[ConstraintNode]) -> list[ConstraintNode]:
    out: list[ConstraintNode] = []
    for node in nodes:
        if isinstance(node, LogicalConstraintIR) and node.operator in ("and", "andSequence"):
            out.extend(_flatten_conj(list(node.constraints)))
        else:
            out.append(node)
    return out


def _eval_conj(
    nodes: list[ConstraintNode],
    kbs: Mapping[str, OperandKB],
    missing_operands: frozenset[str],
) -> Verdict3:
    conj = _flatten_conj(nodes)
    if not conj:
        return "sat"

    # 1) Compose xone outside the solver (2602 Def 6 / EPR).
    for idx, node in enumerate(conj):
        if isinstance(node, LogicalConstraintIR) and node.operator == "xone":
            rest = conj[:idx] + conj[idx + 1 :]
            raw_verdict = _z3_check(conj, kbs, missing_operands)
            if raw_verdict == "unsat":
                return "unsat"
            if raw_verdict == "unknown":
                return "unknown"

            branch_verdicts = [_eval_conj(rest + [b], kbs, missing_operands) for b in node.constraints]
            if all(v == "unsat" for v in branch_verdicts):
                return "unsat"
            if any(v == "unknown" for v in branch_verdicts):
                return "unknown"
            sat_indices = [i for i, v in enumerate(branch_verdicts) if v == "sat"]
            if not sat_indices:
                return "unsat"
            if len(sat_indices) == 1:
                return "sat"

            if _uses_kb_or_set(node, kbs):
                xone_kb_ops = _operands_in_node(node) & (set(kbs) | {
                    c.left_operand
                    for b in node.constraints
                    for c in _atoms_of(b)
                    if c.operator in SET_OPERATORS
                })
                rest_ops = set().union(*(_operands_in_node(r) for r in rest)) if rest else set()
                if rest_ops & xone_kb_ops:
                    return "unknown"
                for a_pos in range(len(sat_indices)):
                    for b_pos in range(a_pos + 1, len(sat_indices)):
                        b_a = node.constraints[sat_indices[a_pos]]
                        b_b = node.constraints[sat_indices[b_pos]]
                        if _eval_conj(rest + [b_a, b_b], kbs, missing_operands) != "unsat":
                            return "unknown"
            return "sat"

    # 2) Expand disjunctions that contain indeterminate leaves or xone (K3).
    for idx, node in enumerate(conj):
        if isinstance(node, LogicalConstraintIR) and node.operator == "or":
            if _needs_split(node, kbs, missing_operands):
                rest = conj[:idx] + conj[idx + 1 :]
                verdicts = [_eval_conj(rest + [b], kbs, missing_operands) for b in node.constraints]
                if any(v == "sat" for v in verdicts):
                    return "sat"
                if all(v == "unsat" for v in verdicts):
                    return "unsat"
                return "unknown"

    # 3) Flat conjunction of concrete nodes + any indeterminate atoms.
    concrete: list[ConstraintNode] = []
    has_indeterminate = False
    for node in conj:
        if isinstance(node, ConstraintIR) and _is_indeterminate_atom(node, kbs, missing_operands):
            has_indeterminate = True
        else:
            concrete.append(node)

    z3_verdict = _z3_check(concrete, kbs, missing_operands) if concrete else "sat"
    if z3_verdict == "unsat":
        return "unsat"
    if z3_verdict == "unknown":
        return "unknown"
    return "unknown" if has_indeterminate else "sat"


def _atoms_of(node: ConstraintNode) -> list[ConstraintIR]:
    if isinstance(node, ConstraintIR):
        return [node]
    out: list[ConstraintIR] = []
    for child in node.constraints:
        out.extend(_atoms_of(child))
    return out


def check_satisfiable(
    *constraint_lists: list,
    kbs: Mapping[str, OperandKB] | None = None,
    missing_operands: frozenset[str] = frozenset(),
) -> Verdict3:
    """Three-valued satisfiability check: ``'sat'``, ``'unsat'``, or ``'unknown'``."""
    active_kbs = DEFAULT_KBS if kbs is None else kbs
    combined: list[ConstraintNode] = [node for lst in constraint_lists for node in lst]
    if not combined:
        return "sat"
    with _Z3_LOCK:
        return _eval_conj(combined, active_kbs, missing_operands)


def satisfiable(
    *constraint_lists: list,
    kbs: Mapping[str, OperandKB] | None = None,
) -> bool:
    """True when the conjunction of every list's constraints is not provably unsat."""
    return check_satisfiable(*constraint_lists, kbs=kbs) != "unsat"


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
        return [
            f for f in self.findings if f.code not in (UNSATISFIABLE, UNKNOWN_CONSTRAINT)
        ]

    @property
    def unsatisfiable(self) -> list[Finding]:
        return [f for f in self.findings if f.code == UNSATISFIABLE]

    @property
    def unknown(self) -> list[Finding]:
        return [f for f in self.findings if f.code == UNKNOWN_CONSTRAINT]

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "conflicts": [asdict(f) for f in self.conflicts],
            "unsatisfiable": [asdict(f) for f in self.unsatisfiable],
            "unknown": [asdict(f) for f in self.unknown],
        }

    def messages(self) -> list[str]:
        return [f"reasoning: {f.code} {f.path}: {f.detail}" for f in self.findings]

    def blocking_messages(self) -> list[str]:
        return [
            f"reasoning: {f.code} {f.path}: {f.detail}"
            for f in self.findings
            if f.code != UNKNOWN_CONSTRAINT
        ]


def _same(a: str | None, b: str | None) -> bool:
    """Two party/asset slots can denote the same entity (``None`` = any)."""
    return a is None or b is None or a.casefold() == b.casefold()


def _assignee(rule: RuleIR, ir: PolicyIR) -> str | None:
    return rule.assignee or ir.assignee


def _duty_constraints(duty: Any) -> list:
    out = list(duty.constraints)
    if getattr(duty, "deadline", None) is not None:
        out.append(duty.deadline)
    return out


def analyze(ir: PolicyIR, kbs: Mapping[str, OperandKB] | None = None) -> ReasoningReport:
    """Run every check on ``ir``; findings are empty for a consistent policy."""
    active_kbs = DEFAULT_KBS if kbs is None else kbs
    grounded_ir = ground_constraints(ir, kbs=active_kbs)
    report = ReasoningReport()
    unknown_rules: set[int] = set()

    def check_duties_sat(rule_idx: int, duties: list, prefix: str) -> None:
        for j, duty in enumerate(duties):
            d_constraints = _duty_constraints(duty)
            if d_constraints:
                verdict = check_satisfiable(d_constraints, kbs=active_kbs)
                if verdict == "unsat":
                    report.findings.append(
                        Finding(UNSATISFIABLE, f"duty {duty.action!r} can never be fulfilled",
                                [rule_idx], f"{prefix}.{j}.constraints")
                    )  # fmt: skip
                elif verdict == "unknown":
                    report.findings.append(
                        Finding(UNKNOWN_CONSTRAINT,
                                f"duty {duty.action!r} has ungrounded or undecided constraints",
                                [rule_idx], f"{prefix}.{j}.constraints")
                    )  # fmt: skip
            if getattr(duty, "consequences", None):
                check_duties_sat(rule_idx, duty.consequences, f"{prefix}.{j}.consequences")

    for i, rule in enumerate(grounded_ir.rules):
        all_constraints = list(rule.constraints) + list(rule.action_refinements)
        if all_constraints:
            verdict = check_satisfiable(all_constraints, kbs=active_kbs)
            if verdict == "unsat":
                report.findings.append(
                    Finding(UNSATISFIABLE, f"{rule.modality} {rule.action!r} can never apply",
                            [i], f"rules.{i}.constraints")
                )  # fmt: skip
            elif verdict == "unknown":
                unknown_rules.add(i)
                report.findings.append(
                    Finding(UNKNOWN_CONSTRAINT,
                            f"{rule.modality} {rule.action!r} has ungrounded or undecided constraints",
                            [i], f"rules.{i}.constraints")
                )  # fmt: skip
        check_duties_sat(i, rule.duties, f"rules.{i}.duties")

    def overlap_verdict(a: RuleIR, b: RuleIR) -> Verdict3:
        if not (_same(a.target, b.target) and _same(_assignee(a, grounded_ir), _assignee(b, grounded_ir))):
            return "unsat"
        return check_satisfiable(
            list(a.constraints) + list(a.action_refinements),
            list(b.constraints) + list(b.action_refinements),
            kbs=active_kbs,
        )

    for i, perm in enumerate(grounded_ir.rules):
        if perm.modality != "permission":
            continue
        for j, prohib in enumerate(grounded_ir.rules):
            if prohib.modality != "prohibition" or prohib.action != perm.action:
                continue
            verdict = overlap_verdict(perm, prohib)
            if verdict == "sat":
                report.findings.append(
                    Finding(
                        DEONTIC_CONFLICT,
                        f"permission {perm.action!r} (rule {i}) overlaps prohibition "
                        f"{prohib.action!r} (rule {j})",
                        [i, j],
                        f"rules.{i}/rules.{j}",
                    )
                )
            elif verdict == "unknown" and i not in unknown_rules and j not in unknown_rules:
                report.findings.append(
                    Finding(
                        UNKNOWN_CONSTRAINT,
                        f"overlap of permission {perm.action!r} (rule {i}) and prohibition "
                        f"{prohib.action!r} (rule {j}) is undecided",
                        [i, j],
                        f"rules.{i}/rules.{j}",
                    )
                )

    obligations: list[tuple[int, str, RuleIR | Any, str | None, list]] = []

    def collect_duties(rule_idx: int, rule: RuleIR, duties: list) -> None:
        for duty in duties:
            obligations.append(
                (rule_idx, duty.action, duty, duty.target or rule.target, _duty_constraints(duty))
            )
            if getattr(duty, "consequences", None):
                collect_duties(rule_idx, rule, duty.consequences)

    for i, rule in enumerate(grounded_ir.rules):
        if rule.modality == "obligation":
            obligations.append((i, rule.action, rule, rule.target, list(rule.constraints)))
        collect_duties(i, rule, rule.duties)
    for i, action, who, target, constraints in obligations:
        assignee = getattr(who, "assignee", None) or grounded_ir.assignee
        for j, prohib in enumerate(grounded_ir.rules):
            if prohib.modality != "prohibition" or prohib.action != action:
                continue
            if not (_same(target, prohib.target) and _same(assignee, _assignee(prohib, grounded_ir))):
                continue
            verdict = check_satisfiable(
                constraints,
                list(prohib.constraints) + list(prohib.action_refinements),
                kbs=active_kbs,
            )
            if verdict == "sat":
                report.findings.append(
                    Finding(
                        OBLIGATION_CONFLICT,
                        f"duty/obligation {action!r} (rule {i}) is prohibited by rule {j}",
                        [i, j],
                        f"rules.{i}/rules.{j}",
                    )
                )
            elif verdict == "unknown" and i not in unknown_rules and j not in unknown_rules:
                report.findings.append(
                    Finding(
                        UNKNOWN_CONSTRAINT,
                        f"duty/obligation {action!r} (rule {i}) vs prohibition (rule {j}) is undecided",
                        [i, j],
                        f"rules.{i}/rules.{j}",
                    )
                )
    return report
