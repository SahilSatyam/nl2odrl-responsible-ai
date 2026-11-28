"""Tests for Phase 3.1 (OSES fragments + SCR repair) and Phase 3.2 (reference inlining)."""
from __future__ import annotations

import json

from scripted_llm import ir, rule, scripted_llm

from odrl_agent import extractor
from odrl_agent.extractor import Extractor, inline_references
from odrl_agent.pipeline import convert


def test_oses_fragments_loaded_without_raw_ttl() -> None:
    prompt = extractor.SYSTEM_PROMPT
    assert "OSES Syntax and Grounding Guide" in prompt
    assert "OSES Duty Roles and Achievement Semantics" in prompt
    assert "Example: Agreement" in prompt
    assert "Example: Offer" in prompt
    assert "Example: Set" in prompt
    assert "isPartOf" in prompt and "isA" in prompt
    # Never paste raw ontology TTL into the prompt.
    assert "owl:Class" not in prompt and "rdfs:subClassOf" not in prompt


def test_oses_european_union_and_remedy_scripted_extraction() -> None:
    text = (
        "Data Steward permits Research Lab to read clinical-ds within the European Union. "
        "Research Lab must not distribute clinical-ds; if violated must compensate Data Steward."
    )
    reply = ir(
        rule(
            action="read",
            target="clinical-ds",
            assignee="Research Lab",
            quote="Data Steward permits Research Lab to read clinical-ds within the European Union.",
            constraints=[{
                "kind": "atomic",
                "left_operand": "spatial",
                "operator": "isPartOf",
                "right_operand": "European Union",
                "value_type": "string",
                "right_concepts": [],
            }],
        ),
        rule(
            modality="prohibition",
            action="distribute",
            target="clinical-ds",
            assignee="Research Lab",
            quote="Research Lab must not distribute clinical-ds",
            duties=[{
                "action": "compensate",
                "role": "remedy",
                "target": "Data Steward",
                "source_spans": [{"quote": "if violated must compensate Data Steward."}],
            }],
        ),
        policy_type="Agreement",
        assigner="Data Steward",
        assignee="Research Lab",
    )
    requests: list = []
    replies = [
        {
            "clauses": [
                "Data Steward permits Research Lab to read clinical-ds within the European Union.",
                "Research Lab must not distribute clinical-ds; if violated must compensate Data Steward.",
            ]
        },
        reply,
    ]
    draft, doc = convert(text, Extractor(scripted_llm(replies, requests)))
    assert draft.status == "ready"
    assert draft.assumptions == []
    assert draft.ir["rules"][0]["constraints"][0]["left_operand"] == "spatial"
    assert draft.ir["rules"][0]["constraints"][0]["operator"] == "isPartOf"
    assert draft.ir["rules"][0]["constraints"][0]["right_concepts"] == [
        "urn:nl2odrl:kb:spatial:eu"
    ]
    assert draft.ir["rules"][1]["duties"][0]["role"] == "remedy"
    assert doc is not None and "remedy" in doc["prohibition"][0]


def test_scr_map_provides_hints_and_never_forwards_raw_shacl_prose() -> None:
    requests: list = []
    llm = scripted_llm([ir(rule())], requests)
    Extractor(llm).repair(
        "Allow research-team to use dataset-7.",
        previous=ir(rule()),
        violations=[
            "rules.0.constraints.0.left_operand: Input should be 'spatial'...",
            "reasoning: DEONTIC_CONFLICT rules.0/rules.1: overlaps prohibition",
            "shacl: RAW SHACL FOCUS NODE DIAGNOSTIC SHOULD NOT LEAK",
        ],
    )
    repair_msg = requests[0]["messages"][2]["content"]
    assert "RAW SHACL FOCUS NODE DIAGNOSTIC" not in repair_msg
    payload = json.loads(repair_msg.split("Repair request:\n", 1)[1])
    hints = "\n".join(payload["scr_hints"])
    assert "SCR[LEFT_OPERAND]" in hints
    assert "SCR[DEONTIC_CONFLICT]" in hints
    assert "SCR[ODRL_STRUCTURE]" in hints


def test_deterministic_reference_inlining_cites_both_sections() -> None:
    text = "Section 2 forbids distribution. Section 5 applies Section 2 to dataset D"
    rew = inline_references(text)
    assert rew.path == "deterministic"
    assert len(rew.inlined) == 1
    assert rew.inlined[0]["ref"] == "Section 2"
    assert "Section 2 forbids distribution." in rew.working_text
    assert "Section 5 applies Section 2 to dataset D" in rew.working_text

    reply = ir(
        {
            "modality": "prohibition",
            "action": "distribute",
            "target": "dataset D",
            "assignee": None,
            "source_spans": [
                {"quote": "Section 2 forbids distribution."},
                {"quote": "Section 5 applies Section 2 to dataset D"},
            ],
        },
        policy_type="Set",
        assigner=None,
    )
    requests: list = []
    llm = scripted_llm([reply], requests)
    draft, doc = convert(text, Extractor(llm))
    assert llm.usage.calls == 1
    assert doc is not None and draft.status == "ready"
    spans = [s["quote"] for s in draft.ir["rules"][0]["source_spans"]]
    assert "Section 2 forbids distribution." in spans
    assert "Section 5 applies Section 2 to dataset D" in spans
    assert "[Inlined Section 2:" in requests[0]["messages"][1]["content"]


def test_missing_referent_sets_unresolved_reference_with_no_extra_call() -> None:
    text = "see Section 9"
    rew = inline_references(text)
    assert rew.path == "unresolved"
    assert rew.unresolved == ["Section 9"]

    requests: list = []
    llm = scripted_llm([{"analysis": "missing ref", "rules": [], "ambiguities": []}], requests)
    draft, doc = convert(text, Extractor(llm))
    assert llm.usage.calls == 1  # extract only; no rewrite call and no backstop slot calls
    assert doc is None
    assert draft.status == "no_rules"
    assert draft.ir["rules"] == []
    assert [a["kind"] for a in draft.ambiguities] == ["unresolved_reference"]


def test_implicit_dangling_pointer_uses_rewrite_and_drops_non_substrings() -> None:
    text = "Alice may read report-1. The foregoing applies to Alice for report-2."
    rew = inline_references(text)
    assert rew.path == "dangling_implicit"

    replies = [
        {"clauses": ["Alice may read report-1.", "Invented non-substring clause."]},
        ir(
            rule(action="read", target="report-1", assignee="Alice", quote="Alice may read report-1."),
            rule(action="read", target="report-2", assignee="Alice", quote="The foregoing applies to Alice for report-2."),
        ),
    ]
    requests: list = []
    llm = scripted_llm(replies, requests)
    draft, doc = convert(text, Extractor(llm))
    assert llm.usage.calls == 2  # rewrite + extract
    assert doc is not None
    extract_user = requests[1]["messages"][1]["content"]
    assert "Alice may read report-1." in extract_user
    assert "Invented non-substring clause" not in extract_user
