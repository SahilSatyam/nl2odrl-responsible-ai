"""Asymmetric and symmetric policy comparison tests (Phase 3.3)."""
from __future__ import annotations

import pytest

from odrl_agent.bench import evaluator
from odrl_agent.compare import compare
from odrl_agent.ir import PolicyIR


def _policy(
    *,
    policy_type: str = "Agreement",
    assigner: str | None = "steward",
    assignee: str | None = "team-a",
    rules: list[dict] | None = None,
) -> PolicyIR:
    data: dict = {"policy_type": policy_type, "rules": rules or []}
    if assigner is not None:
        data["assigner"] = assigner
    if assignee is not None:
        data["assignee"] = assignee
    return PolicyIR.model_validate(data)


def _c(lo: str, op: str, ro: str, vt: str = "string") -> dict:
    return {
        "kind": "atomic",
        "left_operand": lo,
        "operator": op,
        "right_operand": ro,
        "value_type": vt,
    }


def test_read_in_france_contained_in_read_in_europe() -> None:
    provider = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "constraints": [_c("spatial", "isPartOf", "Europe")],
    }])
    requester = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "constraints": [_c("spatial", "isPartOf", "France")],
    }])
    res = compare(provider, requester)
    assert res["asymmetric"] is True
    assert res["symmetric"] is False
    assert res["extra_permissions"] == []
    assert res["missing_obligations"] == []


def test_read_worldwide_conflicts_with_read_in_europe() -> None:
    provider = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "constraints": [_c("spatial", "isPartOf", "Europe")],
    }])
    requester = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "constraints": [_c("spatial", "isPartOf", "World")],
    }])
    res = compare(provider, requester)
    assert res["asymmetric"] is False
    assert res["symmetric"] is False
    assert len(res["extra_permissions"]) == 1
    assert res["extra_permissions"][0]["action"] == "read"


def test_provider_obligation_with_no_requester_duty_flagged() -> None:
    provider = _policy(rules=[
        {"modality": "permission", "action": "read", "target": "ds"},
        {"modality": "obligation", "action": "delete", "target": "ds", "assignee": "team-a"},
    ])
    requester = _policy(rules=[
        {"modality": "permission", "action": "read", "target": "ds"},
    ])
    res = compare(provider, requester)
    assert res["asymmetric"] is False
    assert res["extra_permissions"] == []
    assert len(res["missing_obligations"]) == 1
    assert res["missing_obligations"][0]["action"] == "delete"


def test_stricter_requester_obligation_satisfies_provider_obligation() -> None:
    provider = _policy(rules=[
        {"modality": "permission", "action": "read", "target": "ds"},
        {
            "modality": "obligation",
            "action": "delete",
            "target": "ds",
            "assignee": "team-a",
            "constraints": [_c("elapsedTime", "lteq", "P30D", "duration")],
        },
    ])
    requester = _policy(rules=[
        {"modality": "permission", "action": "read", "target": "ds"},
        {
            "modality": "obligation",
            "action": "delete",
            "target": "ds",
            "assignee": "team-a",
            "constraints": [_c("elapsedTime", "lteq", "P14D", "duration")],
        },
    ])
    res = compare(provider, requester)
    assert res["asymmetric"] is True
    assert res["symmetric"] is False
    assert res["missing_obligations"] == []


def test_identical_irs_are_symmetric_and_asymmetric() -> None:
    ir = _policy(rules=[
        {
            "modality": "permission",
            "action": "read",
            "target": "ds",
            "constraints": [_c("spatial", "isPartOf", "Europe"), _c("count", "lteq", "5", "integer")],
        },
        {"modality": "obligation", "action": "inform", "target": "steward", "assignee": "team-a"},
    ])
    res = compare(ir, ir)
    assert res == {
        "symmetric": True,
        "asymmetric": True,
        "missing_obligations": [],
        "extra_permissions": [],
    }


def test_offer_vs_agreement_allowed_asymmetric_only_and_never_calls_decide(monkeypatch) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("compare must never call evaluator.decide")

    monkeypatch.setattr(evaluator, "decide", _boom)

    offer = _policy(
        policy_type="Offer",
        assigner="steward",
        assignee=None,
        rules=[{
            "modality": "permission",
            "action": "use",
            "target": "ds",
            "constraints": [_c("spatial", "isPartOf", "Europe")],
        }],
    )
    agreement = _policy(
        policy_type="Agreement",
        assigner="steward",
        assignee="team-a",
        rules=[{
            "modality": "permission",
            "action": "read",
            "target": "ds",
            "constraints": [_c("spatial", "isPartOf", "France")],
        }],
    )
    res = compare(offer, agreement)
    assert res["asymmetric"] is True
    assert res["symmetric"] is False
    assert res["extra_permissions"] == []
    assert res["missing_obligations"] == []


def test_provider_prohibition_blocks_overlapping_set_purpose() -> None:
    provider = _policy(rules=[
        {"modality": "permission", "action": "read", "target": "ds"},
        {
            "modality": "prohibition",
            "action": "read",
            "target": "ds",
            "constraints": [_c("purpose", "isA", "commercial")],
        },
    ])
    ok_req = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "constraints": [_c("purpose", "isA", "research")],
    }])
    bad_req = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "constraints": [_c("purpose", "isA", "marketing")],
    }])
    assert compare(provider, ok_req)["asymmetric"] is True
    bad_res = compare(provider, bad_req)
    assert bad_res["asymmetric"] is False
    assert len(bad_res["extra_permissions"]) == 1


def test_missing_precondition_duty_on_permission_is_flagged() -> None:
    provider = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "duties": [{"action": "attribute", "role": "precondition", "target": "steward"}],
    }])
    requester = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
    }])
    res = compare(provider, requester)
    assert res["asymmetric"] is False
    assert len(res["missing_obligations"]) == 1
    assert res["missing_obligations"][0]["action"] == "attribute"
