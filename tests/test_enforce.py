"""Runtime enforcement adapter tests (Phase 3.4, ODRE Algorithm 1)."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from odrl_agent import enforce as enforce_mod
from odrl_agent.compiler import compile_doc
from odrl_agent.enforce import EnforcementError, enforce
from odrl_agent.ir import PolicyIR


def _policy(
    *,
    policy_type: str = "Set",
    rules: list[dict],
    assigner: str | None = None,
    assignee: str | None = None,
) -> PolicyIR:
    data: dict = {"policy_type": policy_type, "rules": rules}
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


@pytest.mark.parametrize(
    ("op", "good", "bad"),
    [
        ("lt", 4, 5),
        ("lteq", 5, 6),
        ("eq", 5, 4),
        ("neq", 4, 5),
        ("gt", 6, 5),
        ("gteq", 5, 4),
    ],
)
def test_six_numeric_constant_operators(op: str, good: int, bad: int) -> None:
    ir = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "constraints": [_c("count", op, "5", "integer")],
    }])
    doc = compile_doc(ir)
    assert enforce(doc, ir, {"count": good}) == [("read", "read")]
    assert enforce(doc, ir, {"count": bad}) == []


@pytest.mark.parametrize(
    ("op", "good", "bad"),
    [
        ("lt", "2026-06-01T09:00:00Z", "2026-06-01T12:00:00Z"),
        ("lteq", "2026-06-01T12:00:00Z", "2026-06-01T12:00:01Z"),
        ("eq", "2026-06-01T12:00:00Z", "2026-06-01T13:00:00Z"),
        ("neq", "2026-06-01T13:00:00Z", "2026-06-01T12:00:00Z"),
        ("gt", "2026-06-01T13:00:00Z", "2026-06-01T12:00:00Z"),
        ("gteq", "2026-06-01T12:00:00Z", "2026-06-01T11:59:59Z"),
    ],
)
def test_six_datetime_operators(op: str, good: str, bad: str) -> None:
    ir = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "constraints": [_c("dateTime", op, "2026-06-01T12:00:00Z", "dateTime")],
    }])
    doc = compile_doc(ir)
    assert enforce(doc, ir, {"dateTime": good}) == [("read", "read")]
    assert enforce(doc, ir, {"dateTime": bad}) == []


def test_is_part_of_allows_mapped_subconcept_and_denies_unmapped_or_disjoint() -> None:
    ir = _policy(rules=[{
        "modality": "permission",
        "action": "read",
        "target": "ds",
        "constraints": [_c("spatial", "isPartOf", "Europe")],
    }])
    doc = compile_doc(ir)
    assert enforce(doc, ir, {"spatial": "France"}) == [("read", "read")]
    assert enforce(doc, ir, {"spatial": "Japan"}) == []
    # 2602 Def. 11: unmapped runtime concept does not satisfy (deny)
    assert enforce(doc, ir, {"spatial": "bavaria"}) == []


def test_rejects_tampered_doc_custom_uid_and_offers() -> None:
    ir = _policy(rules=[{"modality": "permission", "action": "read", "target": "ds"}])
    doc = compile_doc(ir)

    tampered = copy.deepcopy(doc)
    tampered["permission"][0]["constraint"] = [{"type": "Constraint", "leftOperand": "count",
                                                "operator": "lt", "rightOperand": "99"}]
    with pytest.raises(EnforcementError, match="hash"):
        enforce(tampered, ir, {})

    bad_uid_ir = PolicyIR.model_validate({
        "uid": "https://example.org/custom",
        "rules": [{"modality": "permission", "action": "read", "target": "ds"}],
    })
    bad_uid_doc = compile_doc(bad_uid_ir)
    with pytest.raises(EnforcementError, match="urn:nl2odrl:policy:"):
        enforce(bad_uid_doc, bad_uid_ir, {})

    offer_ir = _policy(
        policy_type="Offer",
        assigner="steward",
        rules=[{"modality": "permission", "action": "read", "target": "ds"}],
    )
    offer_doc = compile_doc(offer_ir)
    with pytest.raises(EnforcementError, match="Offer"):
        enforce(offer_doc, offer_ir, {})


def test_supported_handler_unknown_action_and_pending_duties() -> None:
    called: list[str] = []
    ir = _policy(
        policy_type="Agreement",
        assigner="steward",
        assignee="team-a",
        rules=[
            {
                "modality": "permission",
                "action": "read",
                "target": "ds",
                "duties": [{"action": "attribute", "role": "precondition", "target": "steward"}],
            },
            {
                "modality": "permission",
                "action": "display",
                "target": "ds",
            },
        ],
    )
    doc = compile_doc(ir)
    decisions = enforce(
        doc,
        ir,
        {},
        handlers={
            "read": lambda _ctx: (called.append("read") or "payload"),
            "attribute": lambda _ctx: (called.append("attribute") or "ran"),
        },
    )
    assert decisions == [
        ("read", "payload"),
        ("attribute", "pending_precondition"),
        ("display", "display"),
    ]
    assert called == ["read"]


def test_prohibition_blocks_matching_permission() -> None:
    ir = _policy(rules=[
        {"modality": "permission", "action": "distribute", "target": "ds"},
        {
            "modality": "prohibition",
            "action": "distribute",
            "target": "ds",
            "constraints": [_c("purpose", "isA", "commercial")],
        },
    ])
    doc = compile_doc(ir)
    assert enforce(doc, ir, {"purpose": "research"}) == [("distribute", "distribute")]
    assert enforce(doc, ir, {"purpose": "marketing"}) == []


def test_enforce_module_is_pure_no_extractor_or_jinja() -> None:
    import ast

    src = Path(enforce_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        low = mod.lower()
        assert "extractor" not in low
        assert "jinja" not in low
        assert "httpx" not in low
