"""Compiler + SHACL: IR -> JSON-LD -> rdflib -> SHACL for every modality.

The property test draws 300 random valid IRs from a seeded generator (no
extra dependency) and asserts each compiles to a document that passes the
ODRL 2.2 shapes by construction, re-parses to an isomorphic graph, and is
byte-stable across recompiles.
"""
from __future__ import annotations

import json
import random
import socket

import pytest
from rdflib.compare import isomorphic

from odrl_agent.compiler import (
    ASSET_PREFIX,
    PARTY_PREFIX,
    CompileError,
    canonical_json,
    compile_doc,
    compile_ir,
    mint_iri,
)
from odrl_agent.ir import ACTIONS, OPERATORS, PolicyIR
from odrl_agent.odrl import ODRL_CONTEXT, jsonld_to_graph, validate_odrl

_VALUES = {
    "count": [("integer", "5"), ("integer", "100")],
    "dateTime": [("date", "2027-01-01"), ("dateTime", "2026-06-30T12:00:00Z")],
    "elapsedTime": [("duration", "P30D"), ("duration", "PT12H")],
    "percentage": [("decimal", "12.5")],
    "purpose": [("string", "research"), ("iri", "https://w3id.org/dpv#ResearchAndDevelopment")],
    "spatial": [("iri", "https://www.wikidata.org/wiki/Q458"), ("string", "EU")],
    "language": [("string", "en")],
}
_ORDERED = {"integer", "decimal", "date", "dateTime", "duration"}
_PARTIES = ["research-team", "data steward", "urn:party:acme", "Équipe Données", "https://ex.org/p/1"]
_ASSETS = ["dataset-7", "urn:data:dataset-7", "model weights", "https://ex.org/a/9"]


def _atomic(rng: random.Random) -> dict:
    lo = rng.choice(sorted(_VALUES))
    vt, ro = rng.choice(_VALUES[lo])
    ops = list(OPERATORS) if vt in _ORDERED else ["eq", "neq", "isAnyOf", "isA"]
    return {"kind": "atomic", "left_operand": lo, "operator": rng.choice(ops),
            "right_operand": ro, "value_type": vt}


def _constraint(rng: random.Random, depth: int = 0) -> dict:
    if depth < 2 and rng.random() < 0.3:
        op = rng.choice(["and", "or", "xone", "andSequence"])
        return {"kind": "logical", "operator": op,
                "constraints": [_constraint(rng, depth + 1) for _ in range(rng.randint(2, 3))]}
    return _atomic(rng)


def _rule(rng: random.Random, modality: str) -> dict:
    rule: dict = {
        "modality": modality,
        "action": rng.choice(ACTIONS),
        "target": rng.choice(_ASSETS),
        "source_spans": [{"quote": "x"}],
    }
    if modality == "obligation" or rng.random() < 0.8:
        rule["assignee"] = rng.choice(_PARTIES)
    if rng.random() < 0.5:
        rule["constraints"] = [_constraint(rng) for _ in range(rng.randint(1, 2))]
    if rng.random() < 0.25:
        rule["action_refinements"] = [_atomic(rng)]
    if modality == "permission" and rng.random() < 0.4:
        rule["duties"] = [{"action": rng.choice(["attribute", "compensate", "inform", "delete"]),
                           "constraints": [_atomic(rng)] if rng.random() < 0.5 else []}]
    if modality == "prohibition" and rng.random() < 0.3:
        rule["duties"] = [{"action": rng.choice(["compensate", "inform", "delete"]),
                           "role": "remedy"}]
    return rule


def random_ir(rng: random.Random) -> PolicyIR:
    modalities = [rng.choice(["permission", "prohibition", "obligation"])
                  for _ in range(rng.randint(1, 4))]
    raw: dict = {
        "policy_type": rng.choice(["Set", "Offer", "Agreement"]),
        "rules": [_rule(rng, m) for m in modalities],
        "assigner": rng.choice(_PARTIES),
        "assignee": rng.choice(_PARTIES),
    }
    if rng.random() < 0.5:
        raw["conflict_strategy"] = rng.choice(["perm", "prohibit", "invalid"])
    return PolicyIR.model_validate(raw)


@pytest.mark.parametrize("seed", range(300))
def test_random_ir_passes_shacl_by_construction(seed: int) -> None:
    ir = random_ir(random.Random(seed))
    doc, graph = compile_ir(ir)
    valid, errors = validate_odrl(doc)
    assert valid, (errors, json.dumps(doc, indent=1))
    # Round trip: JSON text -> parse -> isomorphic graph (blank-node insensitive).
    reparsed = jsonld_to_graph(json.loads(json.dumps(doc)))
    assert isomorphic(graph, reparsed)
    # Deterministic: recompiling yields byte-identical canonical JSON.
    assert canonical_json(compile_doc(ir)) == canonical_json(doc)


def _ir(*rules, **extra) -> PolicyIR:
    return PolicyIR.model_validate({"rules": list(rules), **extra})


def _perm(**kw):
    return {"modality": "permission", "action": "use", "target": "dataset-7", **kw}


def test_prohibition_remedy_and_consequence_compile_and_shacl_validate() -> None:
    import rdflib

    ir = _ir(
        _perm(assignee="team", duties=[{
            "action": "attribute",
            "role": "precondition",
            "deadline": {"kind": "atomic", "left_operand": "delayPeriod", "operator": "lteq",
                         "right_operand": "P7D", "value_type": "duration"},
            "consequences": [{"action": "inform", "role": "consequence"}],
        }]),
        {
            "modality": "prohibition",
            "action": "distribute",
            "target": "dataset-7",
            "duties": [{"action": "compensate", "role": "remedy"}],
        },
        {
            "modality": "obligation",
            "action": "delete",
            "target": "dataset-7",
            "assignee": "team",
            "duties": [{"action": "compensate", "role": "consequence"}],
        },
    )
    doc, graph = compile_ir(ir)
    assert validate_odrl(doc) == (True, [])
    assert "remedy" not in doc["permission"][0]
    assert "remedy" in doc["prohibition"][0]
    assert "consequence" in doc["obligation"][0]
    assert "consequence" in doc["permission"][0]["duty"][0]
    odrl = rdflib.Namespace("http://www.w3.org/ns/odrl/2/")
    assert len(list(graph.triples((None, odrl.remedy, None)))) == 1
    assert len(list(graph.triples((None, odrl.duty, None)))) == 1
    assert len(list(graph.triples((None, odrl.consequence, None)))) == 2


def test_every_modality_compiles_and_validates() -> None:
    ir = _ir(
        _perm(assignee="team", duties=[{"action": "attribute"}]),
        {"modality": "prohibition", "action": "distribute", "target": "dataset-7"},
        {"modality": "obligation", "action": "delete", "target": "dataset-7", "assignee": "team"},
    )
    doc, _ = compile_ir(ir)
    assert {"permission", "prohibition", "obligation"} <= set(doc)
    assert doc["permission"][0]["duty"][0]["type"] == "Duty"
    assert validate_odrl(doc) == (True, [])


def test_iris_and_typed_literals() -> None:
    ir = _ir(_perm(assignee="research-team", constraints=[
        {"kind": "atomic", "left_operand": "dateTime", "operator": "lteq",
         "right_operand": "2027-01-01", "value_type": "date"}]))
    doc, graph = compile_ir(ir)
    rule = doc["permission"][0]
    assert rule["target"] == ASSET_PREFIX + "dataset-7"
    assert rule["assignee"] == PARTY_PREFIX + "research-team"
    assert rule["constraint"][0]["rightOperand"]["@type"].endswith("#date")
    literal_types = {str(o.datatype) for o in graph.objects() if getattr(o, "datatype", None)}
    assert "http://www.w3.org/2001/XMLSchema#date" in literal_types
    action_objects = {str(o) for o in graph.objects() if str(o).endswith("/use")}
    assert action_objects == {"http://www.w3.org/ns/odrl/2/use"}


def test_mint_iri_is_stable_and_keeps_lookalikes_distinct() -> None:
    assert mint_iri("Research Team", PARTY_PREFIX) == mint_iri("research   team", PARTY_PREFIX)
    assert mint_iri("urn:party:x", PARTY_PREFIX) == "urn:party:x"
    assert mint_iri("pаypal", PARTY_PREFIX) != mint_iri("paypal", PARTY_PREFIX)  # Cyrillic а


def test_policy_level_parties_are_hoisted_not_repeated() -> None:
    doc, _ = compile_ir(_ir(_perm(assignee="team"), assignee="team", assigner="steward",
                            policy_type="Agreement"))
    assert doc["assignee"] == PARTY_PREFIX + "team"
    assert "assignee" not in doc["permission"][0]
    assert validate_odrl(doc) == (True, [])


def test_uid_is_content_derived_and_stable() -> None:
    a, _ = compile_ir(_ir(_perm()))
    b, _ = compile_ir(_ir(_perm(), analysis="different reasoning text"))
    c, _ = compile_ir(_ir(_perm(action="read")))
    assert a["uid"] == b["uid"] != c["uid"]


def test_rule_without_target_is_a_compile_error() -> None:
    with pytest.raises(CompileError, match="no target"):
        compile_ir(_ir({"modality": "permission", "action": "use"}))
    with pytest.raises(CompileError, match="no rules"):
        compile_ir(_ir())


def test_compile_and_validate_never_touch_the_network(monkeypatch) -> None:
    def _no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    doc, _ = compile_ir(_ir(_perm()))
    assert validate_odrl(doc)[0] is True


# ---- validator negatives (third-party documents) --------------------------------

BASE = {"@context": ODRL_CONTEXT, "type": "Set", "uid": "urn:p:1"}


@pytest.mark.parametrize(
    "doc, needle",
    [
        ("nope", "must be an object"),
        ({**BASE, "@context": "https://evil.example/ctx"}, "@context must be"),
        ({**BASE, "type": "Ticket", "permission": [{"action": "use", "target": "urn:x"}]},
         "type must be one of"),
        ({"@context": ODRL_CONTEXT, "type": "Set",
          "permission": [{"action": "use", "target": "urn:x"}]}, "IRI-identified"),
        (BASE, "at least one rule"),
        ({**BASE, "permission": [{"action": "fly", "target": "urn:x"}]}, "action"),
        ({**BASE, "permission": [{"action": "use"}]}, "target"),
        ({**BASE, "prohibition": [{"action": "use", "target": "urn:x",
                                   "duty": [{"action": "inform"}]}]}, "cannot carry a duty"),
        ({**BASE, "permission": [{"action": "use", "target": "urn:x",
                                  "constraint": [{"leftOperand": "colour", "operator": "eq",
                                                  "rightOperand": "red"}]}]}, "constraint"),
        ({**BASE, "permission": [{"action": "use", "target": "urn:x",
                                  "constraint": [{"type": "LogicalConstraint", "or": [
                                      {"leftOperand": "count", "operator": "lt",
                                       "rightOperand": 3}]}]}]}, "constraint"),
        ({**BASE, "permission": [{"action": "use", "target": "urn:x",
                                  "duty": [{"action": "teleport"}]}]}, "duty"),
        ({**BASE, "obligation": [{"action": "delete", "target": "urn:x"}]}, "assignee"),
        ({**BASE, "type": "Offer", "permission": [{"action": "use", "target": "urn:x"}]},
         "assigner"),
        ({**BASE, "type": "Agreement", "assigner": "urn:a",
          "permission": [{"action": "use", "target": "urn:x"}]}, "Agreement needs an assignee"),
        ({**BASE, "conflict": "maybe", "permission": [{"action": "use", "target": "urn:x"}]},
         "conflict"),
    ],
)
def test_validator_rejects(doc, needle) -> None:
    valid, errors = validate_odrl(doc)
    assert not valid
    assert any(needle in e for e in errors), errors


def test_untyped_third_party_constraints_still_validate() -> None:
    doc = {**BASE, "permission": [{"action": "use", "target": "urn:x", "constraint": [
        {"leftOperand": "dateTime", "operator": "lteq", "rightOperand": "2027-01-01"}]}]}
    assert validate_odrl(doc) == (True, [])


def test_relative_references_resolve_to_a_fixed_base() -> None:
    doc = {**BASE, "permission": [{"action": "use", "target": "urn:x", "assignee": "team"}]}
    graph = jsonld_to_graph(doc)
    assert any(str(o) == "https://relative.nl2odrl.invalid/team" for o in graph.objects())
    assert not any(str(o).startswith("file:") for o in graph.objects())
