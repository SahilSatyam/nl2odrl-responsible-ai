"""PolicyIR schema: vocabulary pinned to the ontology, rejection at the schema layer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import rdflib
from pydantic import ValidationError

from odrl_agent.ir import (
    ACTIONS,
    IR_VERSION,
    LEFT_OPERANDS,
    OPERATORS,
    PolicyIR,
    check_value,
    ir_json_schema,
)
from odrl_agent.odrl import LOCAL_CONTEXT, ontology_terms

REPO = Path(__file__).resolve().parent.parent
SHAPES = REPO / "schemas" / "odrl-2.2-shapes.ttl"


def _atomic(lo="count", op="lt", ro="5", vt="integer"):
    return {"kind": "atomic", "left_operand": lo, "operator": op, "right_operand": ro,
            "value_type": vt}


def _ir(**rule):
    base = {"modality": "permission", "action": "use", "target": "dataset-7",
            "assignee": "team", "source_spans": [{"quote": "use"}]}
    base.update(rule)
    return {"rules": [base]}


def test_vocabulary_is_in_odrl_ontology_and_context() -> None:
    assert set(ACTIONS) <= ontology_terms("Action")
    assert set(LEFT_OPERANDS) <= ontology_terms("LeftOperand")
    assert set(OPERATORS) == ontology_terms("Operator")
    for term in (*ACTIONS, *LEFT_OPERANDS, *OPERATORS):
        assert term in LOCAL_CONTEXT, term


def test_shapes_vocabulary_lists_match_ir() -> None:
    shapes = rdflib.Graph().parse(str(SHAPES), format="turtle")
    odrl = "http://www.w3.org/ns/odrl/2/"
    listed = {str(o)[len(odrl):] for o in shapes.objects() if str(o).startswith(odrl)}
    assert set(ACTIONS) <= listed
    assert set(LEFT_OPERANDS) <= listed
    assert set(OPERATORS) <= listed


def test_valid_ir_round_trips_through_json() -> None:
    raw = _ir(constraints=[_atomic(), {"kind": "logical", "operator": "or",
                                       "constraints": [_atomic(), _atomic("purpose", "eq", "x",
                                                                          "string")]}],
              duties=[{"action": "attribute", "target": "steward"}])
    ir = PolicyIR.model_validate(raw)
    again = PolicyIR.model_validate_json(ir.model_dump_json())
    assert again == ir and ir.ir_version == IR_VERSION


@pytest.mark.parametrize(
    "raw, needle",
    [
        (_ir(action="teleport"), "action"),
        (_ir(constraints=[_atomic(lo="colour")]), "left_operand"),
        (_ir(constraints=[_atomic(op="approx")]), "operator"),
        (_ir(constraints=[_atomic(ro="five")]), "not an integer"),
        (_ir(constraints=[_atomic("dateTime", "lt", "2027-13-01", "date")]), "ISO-8601 date"),
        (_ir(constraints=[_atomic("elapsedTime", "lt", "30 days", "duration")]), "duration"),
        (_ir(constraints=[_atomic("dateTime", "lt", "5", "integer")]), "takes value_type"),
        (_ir(constraints=[_atomic("purpose", "gt", "research", "string")]), "ordered value_type"),
        (_ir(constraints=[{"kind": "logical", "operator": "or", "constraints": [_atomic()]}]),
         "at least 2"),
        (_ir(modality="prohibition", duties=[{"action": "inform"}]), "only a permission"),
        (_ir(modality="obligation", assignee=None), "obligation needs an assignee"),
        (_ir(target="   "), "must not be blank"),
        (_ir(unknown_field=1), "Extra inputs"),
        ({"policy_type": "Offer", **_ir()}, "Offer needs an assigner"),
        ({"profile": "dpv", **_ir()}, "core"),
        ({"ambiguities": [{"kind": "vague_temporal", "description": "soon", "rule_index": 3}],
          **_ir()}, "out of range"),
    ],
)
def test_invalid_ir_rejected_at_schema_layer(raw, needle) -> None:
    with pytest.raises(ValidationError) as exc:
        PolicyIR.model_validate(raw)
    assert needle in str(exc.value)


def test_constraint_nesting_is_bounded() -> None:
    node = _atomic()
    for _ in range(3):
        node = {"kind": "logical", "operator": "and", "constraints": [node, _atomic()]}
    with pytest.raises(ValidationError, match="nesting"):
        PolicyIR.model_validate(_ir(constraints=[node]))


def test_ambiguity_is_data_not_failure() -> None:
    ir = PolicyIR.model_validate({
        "rules": [],
        "ambiguities": [{"kind": "missing_assignee", "description": "who may use it?"}],
    })
    assert not ir.is_compilable and ir.ambiguities[0].kind == "missing_assignee"


@pytest.mark.parametrize(
    "vt, good, bad",
    [
        ("integer", "-3", "3.5"),
        ("decimal", "3.25", "3,25"),
        ("boolean", "true", "yes"),
        ("date", "2027-01-01", "01/01/2027"),
        ("dateTime", "2027-01-01T10:00:00Z", "2027-01-01"),
        ("duration", "P1Y2M3DT4H", "P"),
        ("iri", "https://example.org/x", "not an iri"),
    ],
)
def test_check_value(vt, good, bad) -> None:
    assert check_value(vt, good) is None
    assert check_value(vt, bad) is not None


def test_ir_schema_snapshot() -> None:
    """Schema churn must be deliberate: a change here needs an IR_VERSION decision."""
    schema = ir_json_schema()
    digest = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    snapshot = REPO / "tests" / "fixtures" / "policy_ir.schema.json"
    stored = json.loads(snapshot.read_text(encoding="utf-8"))
    assert stored["ir_version"] == IR_VERSION
    assert stored["sha256"] == digest, "PolicyIR schema changed: update the snapshot and IR_VERSION"


def _depth_of_logical(node, depth=0):
    """Deepest nesting of logical constraint objects in a JSON Schema."""
    if isinstance(node, list):
        return max((_depth_of_logical(n, depth) for n in node), default=depth)
    if not isinstance(node, dict):
        return depth
    here = depth + (1 if node.get("properties", {}).get("kind", {}).get("const") == "logical"
                    else 0)
    return max((_depth_of_logical(v, here) for v in node.values()), default=here)


def test_wire_schema_is_not_recursive_and_fits_provider_depth_limit() -> None:
    """api.meta.ai rejects recursive schemas and schemas nested over 10 levels (HTTP 400)."""
    from odrl_agent.ir import WIRE_LOGICAL_DEPTH, schema_depth, wire_schema

    text = json.dumps(wire_schema())
    assert "$ref" not in text and "$defs" not in text and "discriminator" not in text
    assert _depth_of_logical(wire_schema()) == WIRE_LOGICAL_DEPTH == 1
    assert schema_depth(wire_schema()) <= 10
    assert schema_depth(wire_schema(2)) > 10  # measured: the provider rejects this one


def test_wire_schema_accepts_one_logical_level_ir_accepts_more() -> None:
    import jsonschema

    from odrl_agent.ir import wire_schema

    flat = {
        "analysis": "x", "policy_type": "Set",
        "rules": [{
            "modality": "permission", "action": "read", "target": "a", "assignee": "p",
            "source_spans": [{"quote": "p may read a"}],
            "constraints": [{"kind": "logical", "operator": "or",
                             "constraints": [_atomic(), _atomic(ro="7")]}],
        }],
    }
    PolicyIR.model_validate(flat)
    jsonschema.validate(flat, wire_schema())
    nested = json.loads(json.dumps(flat))
    nested["rules"][0]["constraints"][0]["constraints"][1] = {
        "kind": "logical", "operator": "and", "constraints": [_atomic(), _atomic()]}
    PolicyIR.model_validate(nested)  # still valid IR (e.g. built by hand or repaired)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(nested, wire_schema())
    jsonschema.validate(nested, wire_schema(2))


def test_extractor_sends_the_wire_schema() -> None:
    from scripted_llm import ir, rule, scripted_llm

    from odrl_agent.extractor import Extractor
    from odrl_agent.ir import wire_schema

    requests: list = []
    Extractor(scripted_llm([ir(rule())], requests)).extract("t", None, complex_input=False)
    sent = requests[0]["response_format"]["json_schema"]["schema"]
    assert sent == wire_schema()
