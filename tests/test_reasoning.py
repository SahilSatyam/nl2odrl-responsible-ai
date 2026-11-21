"""Conflict and satisfiability engine: every conflict detected, no false alarms."""
from __future__ import annotations

import pytest

from odrl_agent.ir import PolicyIR
from odrl_agent.reasoning import (
    DEONTIC_CONFLICT,
    HIERARCHY_CONFLICT,
    OBLIGATION_CONFLICT,
    UNSATISFIABLE,
    analyze,
    ancestors,
    includes,
    satisfiable,
)


def c(lo: str, op: str, value: str, vt: str) -> dict:
    return {"kind": "atomic", "left_operand": lo, "operator": op, "right_operand": value,
            "value_type": vt}  # fmt: skip


def rule(modality: str, action: str, **kw) -> dict:
    out = {"modality": modality, "action": action, "target": "dataset-7",
           "assignee": "research-team"}  # fmt: skip
    out.update(kw)
    return out


def codes(*rules: dict, **policy) -> list[str]:
    ir = PolicyIR.model_validate({"rules": list(rules), **policy})
    return sorted(f.code for f in analyze(ir).findings)


def test_hierarchy_is_loaded_from_ontology() -> None:
    assert "use" in ancestors("distribute")
    assert includes("use", "print")
    assert not includes("distribute", "use")


UNSAT = [
    [c("dateTime", "gt", "2027-01-01T00:00:00Z", "dateTime"),
     c("dateTime", "lt", "2026-01-01T00:00:00Z", "dateTime")],
    [c("dateTime", "gteq", "2027-01-01", "date"), c("dateTime", "lteq", "2026-12-31", "date")],
    [c("count", "lt", "0", "integer")],
    [c("count", "eq", "3", "integer"), c("count", "eq", "5", "integer")],
    [c("elapsedTime", "lt", "P0D", "duration")],
    [c("percentage", "gt", "100", "decimal")],
    [c("elapsedTime", "gt", "P1Y", "duration"), c("elapsedTime", "lt", "P30D", "duration")],
    [c("purpose", "eq", "research", "string"), c("purpose", "eq", "marketing", "string")],
    [{"kind": "logical", "operator": "and", "constraints": [
        c("count", "gt", "10", "integer"), c("count", "lt", "5", "integer")]}],
    [{"kind": "logical", "operator": "xone", "constraints": [
        c("count", "gt", "1", "integer"), c("count", "gt", "2", "integer")]},
     c("count", "gt", "5", "integer")],
]  # fmt: skip


@pytest.mark.parametrize("constraints", UNSAT)
def test_unsatisfiable_constraints_are_detected(constraints: list) -> None:
    assert codes(rule("permission", "read", constraints=constraints)) == [UNSATISFIABLE]


SAT = [
    [c("dateTime", "gt", "2026-01-01T00:00:00Z", "dateTime"),
     c("dateTime", "lt", "2027-01-01T00:00:00Z", "dateTime")],
    [c("count", "lteq", "0", "integer")],
    [c("purpose", "eq", "research", "string"), c("purpose", "neq", "marketing", "string")],
    [{"kind": "logical", "operator": "or", "constraints": [
        c("count", "gt", "10", "integer"), c("count", "lt", "5", "integer")]}],
    [c("spatial", "isAnyOf", "EU", "string"), c("spatial", "isNoneOf", "EU", "string")],
]  # fmt: skip


@pytest.mark.parametrize("constraints", SAT)
def test_satisfiable_constraints_pass(constraints: list) -> None:
    assert codes(rule("permission", "read", constraints=constraints)) == []


def test_unsatisfiable_duty_is_detected() -> None:
    duty = {"action": "compensate", "constraints": [c("payAmount", "lt", "0", "decimal")]}
    assert codes(rule("permission", "read", duties=[duty])) == [UNSATISFIABLE]


def test_same_action_permission_and_prohibition_conflict() -> None:
    assert codes(rule("permission", "read"), rule("prohibition", "read")) == [DEONTIC_CONFLICT]


def test_broad_prohibition_against_narrow_permission_conflicts() -> None:
    found = codes(rule("permission", "distribute"), rule("prohibition", "use"))
    assert found == [HIERARCHY_CONFLICT]


def test_narrow_prohibition_under_broad_permission_is_an_exception_not_a_conflict() -> None:
    assert codes(rule("permission", "use"), rule("prohibition", "distribute")) == []


def test_prohibition_without_assignee_applies_to_everyone() -> None:
    prohib = rule("prohibition", "read")
    del prohib["assignee"]
    assert codes(rule("permission", "read"), prohib) == [DEONTIC_CONFLICT]


@pytest.mark.parametrize("strategy", ["perm", "prohibit", "invalid"])
def test_conflict_strategy_resolves_permission_prohibition(strategy: str) -> None:
    rules = (rule("permission", "read"), rule("prohibition", "read"))
    assert codes(*rules, conflict_strategy=strategy) == []


def test_disjoint_parties_targets_or_windows_do_not_conflict() -> None:
    assert codes(rule("permission", "read"), rule("prohibition", "read", assignee="other")) == []
    assert codes(rule("permission", "read"), rule("prohibition", "read", target="other")) == []
    before = [c("dateTime", "lt", "2026-01-01T00:00:00Z", "dateTime")]
    after = [c("dateTime", "gt", "2027-01-01T00:00:00Z", "dateTime")]
    assert codes(
        rule("permission", "read", constraints=before),
        rule("prohibition", "read", constraints=after),
    ) == []


def test_overlapping_windows_conflict() -> None:
    w1 = [c("dateTime", "lt", "2027-01-01T00:00:00Z", "dateTime")]
    w2 = [c("dateTime", "gt", "2026-01-01T00:00:00Z", "dateTime")]
    found = codes(rule("permission", "read", constraints=w1),
                  rule("prohibition", "read", constraints=w2))  # fmt: skip
    assert found == [DEONTIC_CONFLICT]


def test_duty_to_do_prohibited_action_conflicts_even_with_strategy() -> None:
    perm = rule("permission", "read", duties=[{"action": "attribute"}])
    found = codes(perm, rule("prohibition", "attribute"), conflict_strategy="perm")
    assert found == [OBLIGATION_CONFLICT]


def test_obligation_vs_prohibition_conflicts() -> None:
    found = codes(rule("obligation", "delete"), rule("prohibition", "delete"))
    assert found == [OBLIGATION_CONFLICT]


def test_consistent_policy_is_ok() -> None:
    ir = PolicyIR.model_validate({"rules": [rule("permission", "read"),
                                            rule("prohibition", "distribute")]})  # fmt: skip
    assert analyze(ir).ok


def test_satisfiable_with_no_constraints() -> None:
    assert satisfiable([])
