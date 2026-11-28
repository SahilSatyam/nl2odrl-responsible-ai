"""Three-valued KB grounding and constraint reasoning (Phase 2, Task 2.1)."""
from __future__ import annotations

import socket
from pathlib import Path

import z3
from fastapi.testclient import TestClient
from scripted_llm import install, ir, rule, scripted_llm

from odrl_agent.app import create_app
from odrl_agent.extractor import Extractor
from odrl_agent.ir import PolicyIR
from odrl_agent.kb import DEFAULT_KBS, PURPOSE_KB, build_kb, ground_constraints, without_disjoint
from odrl_agent.pipeline import convert
from odrl_agent.reasoning import (
    DEONTIC_CONFLICT,
    UNKNOWN_CONSTRAINT,
    UNSATISFIABLE,
    analyze,
    check_satisfiable,
)

REPO = Path(__file__).resolve().parent.parent


def _c(lo: str, op: str, val: str, vt: str = "string") -> dict:
    return {
        "kind": "atomic",
        "left_operand": lo,
        "operator": op,
        "right_operand": val,
        "value_type": vt,
    }


def _policy(*rules: dict, **extra) -> PolicyIR:
    return PolicyIR.model_validate({"rules": list(rules), **extra})


def test_ground_constraints_fills_right_concepts_deterministically_without_network(monkeypatch) -> None:
    def _no_net(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", _no_net)
    monkeypatch.setattr(socket.socket, "connect", _no_net)

    src = _policy({
        "modality": "permission",
        "action": "read",
        "target": "dataset-7",
        "assignee": "team",
        "constraints": [_c("spatial", "isPartOf", "Europe"), _c("purpose", "isA", "commercial")],
    })
    grounded = ground_constraints(src)
    assert src.rules[0].constraints[0].right_concepts == []
    assert grounded.rules[0].constraints[0].right_operand == "Europe"
    assert grounded.rules[0].constraints[0].right_concepts == ["urn:nl2odrl:kb:spatial:europe"]
    assert grounded.rules[0].constraints[1].right_concepts == ["urn:nl2odrl:kb:purpose:commercial"]


def test_france_within_europe_is_compatible_while_canada_conflicts() -> None:
    france_in_eu = _policy({
        "modality": "permission",
        "action": "read",
        "target": "dataset-7",
        "assignee": "team",
        "constraints": [_c("spatial", "isPartOf", "Europe"), _c("spatial", "eq", "France")],
    })
    assert analyze(france_in_eu).ok

    france_vs_europe_overlap = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("spatial", "isPartOf", "Europe")],
        },
        {
            "modality": "prohibition",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("spatial", "isPartOf", "France")],
        },
    )
    assert [f.code for f in analyze(france_vs_europe_overlap).findings] == [DEONTIC_CONFLICT]

    canada_in_europe = _policy({
        "modality": "permission",
        "action": "read",
        "target": "dataset-7",
        "assignee": "team",
        "constraints": [_c("spatial", "isPartOf", "Europe"), _c("spatial", "isPartOf", "Canada")],
    })
    assert [f.code for f in analyze(canada_in_europe).findings] == [UNSATISFIABLE]


def test_french_vs_german_conflict_once_language_kb_exists() -> None:
    lang_kb = build_kb(
        operand="language",
        order_kind="taxonomic",
        disjoint=(("urn:nl2odrl:kb:lang:fr", "urn:nl2odrl:kb:lang:de"),),
        aliases={
            "urn:nl2odrl:kb:lang:fr": ("French", "fr"),
            "urn:nl2odrl:kb:lang:de": ("German", "de"),
        },
    )
    ir_obj = _policy({
        "modality": "permission",
        "action": "read",
        "target": "manuscripts",
        "assignee": "archive",
        "constraints": [_c("language", "isA", "German"), _c("language", "eq", "French")],
    })
    # Without a language KB row, `isA` is ungrounded -> Unknown.
    assert [f.code for f in analyze(ir_obj).findings] == [UNKNOWN_CONSTRAINT]
    # Once a language row exists, French vs German is a provable Conflict.
    kbs = {**DEFAULT_KBS, "language": lang_kb}
    assert [f.code for f in analyze(ir_obj, kbs=kbs).findings] == [UNSATISFIABLE]


def test_unmapped_bavaria_is_unknown_and_holds_draft_for_review(monkeypatch, tmp_path) -> None:
    text = "Allow research-team to read dataset-7 within Bavaria."
    reply = ir(
        rule(
            action="read",
            target="dataset-7",
            assignee="research-team",
            quote=text,
            constraints=[_c("spatial", "isPartOf", "bavaria")],
        )
    )
    draft, doc = convert(text, Extractor(scripted_llm([reply])))
    assert doc is not None
    assert draft.status == "needs_review"
    assert draft.auto_publish is False
    assert draft.reasoning["conflicts"] == []
    assert draft.reasoning["unsatisfiable"] == []
    assert len(draft.reasoning["unknown"]) == 1
    assert draft.reasoning["unknown"][0]["code"] == UNKNOWN_CONSTRAINT

    # In publish flow, UNKNOWN_CONSTRAINT is held as needs_review (not conflict), so human can approve.
    install(monkeypatch, [reply] * 3)
    monkeypatch.chdir(REPO)
    client = TestClient(create_app(REPO, out_dir=tmp_path))
    rk = client.post("/api/convert", json={"nl_text": text, "policy_variant": "fixed", "seed": 42}).json()["run_key"]
    client.app.state.registry.wait(rk, timeout=30.0)
    resp = client.post(
        f"/api/runs/{rk}/publish",
        json={"decision": "approve", "reason": "Bavaria is within Germany"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["published"] is True


def test_xone_commercial_noncommercial_requires_disjoint_axiom() -> None:
    xone_ir = _policy({
        "modality": "permission",
        "action": "read",
        "target": "dataset-7",
        "assignee": "team",
        "constraints": [
            {
                "kind": "logical",
                "operator": "xone",
                "constraints": [
                    _c("purpose", "isA", "commercial"),
                    _c("purpose", "isA", "nonCommercial"),
                ],
            }
        ],
    })
    # Default PURPOSE_KB has commercial _||_ nonCommercial -> Compatible.
    assert analyze(xone_ir).ok

    # Without disjointness (2602 Table 6 / ODRL086) -> UNKNOWN_CONSTRAINT.
    no_disj_kbs = {**DEFAULT_KBS, "purpose": without_disjoint(PURPOSE_KB)}
    report = analyze(xone_ir, kbs=no_disj_kbs)
    assert [f.code for f in report.findings] == [UNKNOWN_CONSTRAINT]
    assert report.conflicts == []

    # Paired with a compatible constraint (research <= nonCommercial) without _||_ -> still Unknown.
    paired_ir = _policy({
        "modality": "permission",
        "action": "read",
        "target": "dataset-7",
        "assignee": "team",
        "constraints": [
            xone_ir.rules[0].constraints[0].model_dump(mode="json"),
            _c("purpose", "isA", "research"),
        ],
    })
    assert analyze(paired_ir).ok
    assert [f.code for f in analyze(paired_ir, kbs=no_disj_kbs).findings] == [UNKNOWN_CONSTRAINT]


def test_z3_timeout_maps_to_unknown_not_satisfiable(monkeypatch) -> None:
    monkeypatch.setattr(z3.Solver, "check", lambda self: z3.unknown)
    ir_obj = _policy({
        "modality": "permission",
        "action": "read",
        "target": "dataset-7",
        "assignee": "team",
        "constraints": [_c("count", "lteq", "5", "integer")],
    })
    assert check_satisfiable(ir_obj.rules[0].constraints) == "unknown"
    report = analyze(ir_obj)
    assert [f.code for f in report.findings] == [UNKNOWN_CONSTRAINT]
    assert report.conflicts == []
    assert report.unsatisfiable == []


def test_contradiction_alongside_unmapped_concept_remains_unsatisfiable() -> None:
    ir_obj = _policy({
        "modality": "permission",
        "action": "read",
        "target": "dataset-7",
        "assignee": "team",
        "constraints": [
            _c("spatial", "isPartOf", "bavaria"),
            _c("dateTime", "gt", "2027-01-01", "date"),
            _c("dateTime", "lt", "2026-01-01", "date"),
        ],
    })
    assert [f.code for f in analyze(ir_obj).findings] == [UNSATISFIABLE]
