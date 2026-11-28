"""Typed policy IR: the contract between the model (meaning) and code (syntax).

The extractor LLM fills a :class:`PolicyIR`; :mod:`odrl_agent.compiler` turns a
valid IR into ODRL 2.2 JSON-LD deterministically. Unknown actions, operands,
operators and malformed literals are rejected here, at the schema layer,
never at SHACL time. The IR is flat and small on purpose: no Turtle or
JSON-LD syntax ever reaches the prompt.

``IR_VERSION`` changes whenever the schema changes; a snapshot test pins the
JSON Schema so churn is visible in review.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IR_VERSION = "1.2"

#: ODRL 2.2 vocabulary terms (core profile). Pinned by a test against
#: ``schemas/odrl-2.2.ttl`` and ``schemas/odrl-2.2.jsonld``.
ACTIONS = (
    "use", "grantUse", "aggregate", "annotate", "anonymize", "archive", "concurrentUse",
    "derive", "digitize", "display", "distribute", "execute", "extract", "give", "index",
    "install", "modify", "move", "play", "present", "print", "read", "reproduce", "sell",
    "stream", "textToSpeech", "transfer", "transform", "translate", "acceptTracking",
    "attribute", "compensate", "delete", "ensureExclusivity", "include", "inform",
    "nextPolicy", "obtainConsent", "reviewPolicy", "uninstall", "watermark",
)  # fmt: skip
LEFT_OPERANDS = (
    "absolutePosition", "absoluteSpatialPosition", "absoluteTemporalPosition", "absoluteSize",
    "count", "dateTime", "delayPeriod", "deliveryChannel", "elapsedTime", "event",
    "fileFormat", "industry", "language", "media", "meteredTime", "payAmount", "percentage",
    "product", "purpose", "recipient", "relativePosition", "relativeSpatialPosition",
    "relativeTemporalPosition", "relativeSize", "resolution", "spatial", "spatialCoordinates",
    "systemDevice", "timeInterval", "unitOfCount", "version", "virtualLocation",
)  # fmt: skip
OPERATORS = (
    "eq", "gt", "gteq", "lt", "lteq", "neq", "isA", "hasPart", "isPartOf", "isAllOf",
    "isAnyOf", "isNoneOf",
)  # fmt: skip
LOGICAL_OPERATORS = ("and", "or", "xone", "andSequence")
CONFLICT_STRATEGIES = ("perm", "prohibit", "invalid")
POLICY_TYPES = ("Set", "Offer", "Agreement")
VALUE_TYPES = ("string", "integer", "decimal", "boolean", "date", "dateTime", "duration", "iri")

Action = Literal[
    "use", "grantUse", "aggregate", "annotate", "anonymize", "archive", "concurrentUse",
    "derive", "digitize", "display", "distribute", "execute", "extract", "give", "index",
    "install", "modify", "move", "play", "present", "print", "read", "reproduce", "sell",
    "stream", "textToSpeech", "transfer", "transform", "translate", "acceptTracking",
    "attribute", "compensate", "delete", "ensureExclusivity", "include", "inform",
    "nextPolicy", "obtainConsent", "reviewPolicy", "uninstall", "watermark",
]  # fmt: skip
LeftOperand = Literal[
    "absolutePosition", "absoluteSpatialPosition", "absoluteTemporalPosition", "absoluteSize",
    "count", "dateTime", "delayPeriod", "deliveryChannel", "elapsedTime", "event",
    "fileFormat", "industry", "language", "media", "meteredTime", "payAmount", "percentage",
    "product", "purpose", "recipient", "relativePosition", "relativeSpatialPosition",
    "relativeTemporalPosition", "relativeSize", "resolution", "spatial", "spatialCoordinates",
    "systemDevice", "timeInterval", "unitOfCount", "version", "virtualLocation",
]  # fmt: skip
Operator = Literal[
    "eq", "gt", "gteq", "lt", "lteq", "neq", "isA", "hasPart", "isPartOf", "isAllOf",
    "isAnyOf", "isNoneOf",
]  # fmt: skip
LogicalOperator = Literal["and", "or", "xone", "andSequence"]
ValueType = Literal["string", "integer", "decimal", "boolean", "date", "dateTime", "duration", "iri"]

#: Value types each left operand may carry (ODRL 2.2 information model).
OPERAND_VALUE_TYPES: dict[str, tuple[str, ...]] = {
    "dateTime": ("date", "dateTime"),
    "delayPeriod": ("duration",),
    "elapsedTime": ("duration",),
    "meteredTime": ("duration",),
    "timeInterval": ("duration",),
    "count": ("integer",),
    "percentage": ("integer", "decimal"),
    "payAmount": ("integer", "decimal"),
    "absoluteSize": ("integer", "decimal"),
    "relativeSize": ("integer", "decimal"),
    "resolution": ("integer", "decimal"),
    "version": ("string", "decimal", "integer"),
    "spatialCoordinates": ("string",),
    "absoluteTemporalPosition": ("date", "dateTime"),
}
#: Operators that make sense only against ordered values.
ORDERING_OPERATORS = ("gt", "gteq", "lt", "lteq")
ORDERED_VALUE_TYPES = ("integer", "decimal", "date", "dateTime", "duration")

MAX_CONSTRAINT_DEPTH = 3
_DURATION = re.compile(
    r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+(\.\d+)?S)?)?$"
)
_INT = re.compile(r"^[+-]?\d+$")
_DECIMAL = re.compile(r"^[+-]?\d+(\.\d+)?$")
_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpanRef(_Model):
    """A verbatim quote from the source text that supports an IR element.

    The model supplies ``quote`` only; the pipeline's ground stage resolves
    ``start``/``end`` offsets and rejects quotes that are not in the source.
    """

    quote: str = Field(min_length=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


def check_value(value_type: str, raw: str) -> str | None:
    """Return an error message when ``raw`` is not a valid ``value_type`` literal."""
    if value_type == "integer":
        return None if _INT.match(raw) else "not an integer"
    if value_type == "decimal":
        return None if _DECIMAL.match(raw) else "not a decimal"
    if value_type == "boolean":
        return None if raw in ("true", "false") else "must be 'true' or 'false'"
    if value_type == "date":
        try:
            date.fromisoformat(raw)
        except ValueError:
            return "not an ISO-8601 date (YYYY-MM-DD)"
        return None
    if value_type == "dateTime":
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return "not an ISO-8601 dateTime"
        if "T" not in raw:
            return "dateTime needs a time part; use value_type 'date'"
        return None
    if value_type == "duration":
        return None if _DURATION.match(raw) else "not an ISO-8601 duration (e.g. P30D)"
    if value_type == "iri":
        return None if _IRI.match(raw) else "not an absolute IRI"
    if value_type == "string":
        return None if raw.strip() else "empty string"
    return f"unknown value_type {value_type!r}"


class ConstraintIR(_Model):
    """One atomic ODRL constraint: ``left_operand operator right_operand``."""

    kind: Literal["atomic"] = "atomic"
    left_operand: LeftOperand
    operator: Operator
    right_operand: str = Field(min_length=1, description="Value as text; typed by value_type.")
    value_type: ValueType = "string"
    right_concepts: list[str] = Field(default_factory=list)
    span: SpanRef | None = None

    @model_validator(mode="after")
    def _check(self) -> ConstraintIR:
        problem = check_value(self.value_type, self.right_operand)
        if problem:
            raise ValueError(
                f"right_operand {self.right_operand!r} invalid as {self.value_type}: {problem}"
            )
        allowed = OPERAND_VALUE_TYPES.get(self.left_operand)
        if allowed and self.value_type not in allowed:
            raise ValueError(
                f"left_operand {self.left_operand!r} takes value_type in {list(allowed)}, "
                f"got {self.value_type!r}"
            )
        if self.operator in ORDERING_OPERATORS and self.value_type not in ORDERED_VALUE_TYPES:
            raise ValueError(
                f"operator {self.operator!r} needs an ordered value_type, got {self.value_type!r}"
            )
        return self


class LogicalConstraintIR(_Model):
    """``and``/``or``/``xone``/``andSequence`` over two or more constraints."""

    kind: Literal["logical"] = "logical"
    operator: LogicalOperator
    constraints: list[ConstraintNode] = Field(min_length=2)
    span: SpanRef | None = None


ConstraintNode = Annotated[Union[ConstraintIR, LogicalConstraintIR], Field(discriminator="kind")]
LogicalConstraintIR.model_rebuild()


def constraint_depth(node: ConstraintIR | LogicalConstraintIR) -> int:
    """Nesting depth of a constraint tree (an atomic constraint is depth 1)."""
    if isinstance(node, LogicalConstraintIR):
        return 1 + max(constraint_depth(child) for child in node.constraints)
    return 1


def _check_depth(nodes: list) -> list:
    for node in nodes:
        if constraint_depth(node) > MAX_CONSTRAINT_DEPTH:
            raise ValueError(f"constraint nesting deeper than {MAX_CONSTRAINT_DEPTH}")
    return nodes


DutyRole = Literal["precondition", "obligation", "remedy", "consequence"]


class DutyIR(_Model):
    """A duty attached to a rule or parent duty (``odrl:duty``, ``odrl:remedy``, ``odrl:consequence``)."""

    action: Action
    role: DutyRole = "precondition"
    target: str | None = None
    assignee: str | None = None
    constraints: list[ConstraintNode] = Field(default_factory=list)
    deadline: ConstraintIR | None = None
    consequences: list[DutyIR] = Field(default_factory=list)
    source_spans: list[SpanRef] = Field(default_factory=list)

    _depth = field_validator("constraints")(_check_depth)

    @model_validator(mode="after")
    def _check(self) -> DutyIR:
        if self.consequences:
            if self.role not in ("precondition", "obligation"):
                raise ValueError(
                    "role='consequence' is valid only on a precondition duty or a policy-level obligation"
                )
            if any(child.role != "consequence" for child in self.consequences):
                raise ValueError("nested consequence duties must have role 'consequence'")
        return self


DutyIR.model_rebuild()


class RuleIR(_Model):
    """One permission, prohibition or obligation."""

    modality: Literal["permission", "prohibition", "obligation"]
    action: Action
    action_refinements: list[ConstraintNode] = Field(default_factory=list)
    target: str | None = None
    assignee: str | None = None
    assigner: str | None = None
    constraints: list[ConstraintNode] = Field(default_factory=list)
    duties: list[DutyIR] = Field(default_factory=list)
    source_spans: list[SpanRef] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    strong: bool = False

    _depth = field_validator("constraints", "action_refinements")(_check_depth)

    @model_validator(mode="after")
    def _check(self) -> RuleIR:
        if self.duties:
            if self.modality == "permission":
                if any(d.role != "precondition" for d in self.duties):
                    raise ValueError(
                        "permission duties must have role 'precondition' "
                        "(remedy is valid only on a prohibition)"
                    )
            elif self.modality == "prohibition":
                if any(d.role != "remedy" for d in self.duties):
                    raise ValueError(
                        "only a permission may carry precondition duties; "
                        "prohibition duties must have role 'remedy'"
                    )
            elif self.modality == "obligation":
                if any(d.role != "consequence" for d in self.duties):
                    raise ValueError("obligation duties must have role 'consequence'")
        if self.modality == "obligation" and not self.assignee:
            raise ValueError("an obligation needs an assignee (who must act)")
        for text in (self.target, self.assignee, self.assigner):
            if text is not None and not text.strip():
                raise ValueError("party/target names must not be blank")
        return self


AmbiguityKind = Literal[
    "missing_assignee",
    "missing_target",
    "missing_assigner",
    "vague_temporal",
    "vague_numeric",
    "contradictory_clauses",
    "unresolved_reference",
    "other",
]


class AmbiguityFlag(_Model):
    """Something the text leaves open. Flag it; never invent an answer."""

    kind: AmbiguityKind
    description: str = Field(min_length=1)
    span: SpanRef | None = None
    rule_index: int | None = Field(default=None, ge=0)


class AssumptionRecord(_Model):
    """A value the extractor filled in that the text does not state."""

    field: str = Field(min_length=1)
    assumed_value: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class PolicyIR(_Model):
    """The full intermediate representation of one natural-language policy."""

    analysis: str = Field(
        default="",
        description="Short free-text reasoning, written before the structured fields.",
    )
    ir_version: Literal["1.0", "1.1", "1.2"] = IR_VERSION
    policy_type: Literal["Set", "Offer", "Agreement"] = "Set"
    profile: str = "core"
    behaviour: Literal["closed", "open"] = "closed"
    action_closure: Literal["none", "permissions_only"] = "permissions_only"
    conflict_strategy: Literal["perm", "prohibit", "invalid"] | None = None
    uid: str | None = None
    assigner: str | None = None
    assignee: str | None = None
    rules: list[RuleIR] = Field(default_factory=list)
    ambiguities: list[AmbiguityFlag] = Field(default_factory=list)
    assumptions: list[AssumptionRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> PolicyIR:
        if self.profile != "core":
            raise ValueError("only the 'core' profile is available before Phase 5")
        if self.rules:
            has_assigner = bool(self.assigner) or all(r.assigner for r in self.rules)
            has_assignee = bool(self.assignee) or all(
                r.assignee for r in self.rules if r.modality != "obligation"
            )
            if self.policy_type in ("Offer", "Agreement") and not has_assigner:
                raise ValueError(f"{self.policy_type} needs an assigner")
            if self.policy_type == "Agreement" and not has_assignee:
                raise ValueError("Agreement needs an assignee")
        for flag in self.ambiguities:
            if flag.rule_index is not None and flag.rule_index >= len(self.rules):
                raise ValueError(f"ambiguity rule_index {flag.rule_index} out of range")
        return self

    @property
    def is_compilable(self) -> bool:
        """True when there is at least one rule to compile."""
        return bool(self.rules)


def ir_json_schema() -> dict:
    """JSON Schema of :class:`PolicyIR` (pinned by the snapshot test)."""
    return PolicyIR.model_json_schema()


def _unroll(node: Any, defs: dict[str, Any], stack: tuple[str, ...], limit: int) -> Any:
    """Inline ``$ref``s; a definition may nest at most ``limit`` times.

    Returns ``None`` for a branch that would exceed the limit; ``anyOf``/
    ``oneOf`` drop such branches, so the deepest logical constraint only
    admits atomic children.
    """
    if isinstance(node, list):
        return [_unroll(item, defs, stack, limit) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.removeprefix("#/$defs/")
        if stack.count(name) >= limit:
            return None
        resolved = _unroll(defs[name], defs, (*stack, name), limit)
        extra = {k: v for k, v in node.items() if k != "$ref"}
        return {**resolved, **extra} if resolved is not None else None
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("$defs", "discriminator"):
            continue
        if key in ("anyOf", "oneOf"):
            branches = [b for b in (_unroll(v, defs, stack, limit) for v in value) if b is not None]
            if not branches:
                return None
            out[key] = branches
            continue
        child = _unroll(value, defs, stack, limit)
        if child is None:
            if key == "items":
                return None
            continue
        out[key] = child
    return out


#: Logical-constraint levels the wire schema admits. api.meta.ai caps schema
#: nesting at 10 levels, which leaves room for one logical level (``and``/
#: ``or`` over atomic constraints). Deeper trees remain valid IR.
WIRE_LOGICAL_DEPTH = 1


def schema_depth(node: Any) -> int:
    """Nesting depth of a JSON Schema, counting object/array schema levels."""
    if isinstance(node, list):
        return max((schema_depth(n) for n in node), default=0)
    if not isinstance(node, dict):
        return 0
    here = 1 if node.get("type") in ("object", "array") else 0
    children = []
    for key in ("properties",):
        children.extend((node.get(key) or {}).values())
    for key in ("items", "additionalProperties"):
        if isinstance(node.get(key), dict):
            children.append(node[key])
    for key in ("anyOf", "oneOf", "allOf"):
        children.extend(node.get(key) or [])
    return here + max((schema_depth(c) for c in children), default=0)


def wire_schema(logical_depth: int = WIRE_LOGICAL_DEPTH) -> dict:
    """Non-recursive ``response_format`` schema sent to the model.

    Some OpenAI-compatible providers reject recursive or deeply nested JSON
    Schemas. This is :func:`ir_json_schema` with every ``$ref`` inlined and
    logical constraints unrolled to ``logical_depth`` levels; the reply is
    still validated against the full :class:`PolicyIR`.
    """
    schema = ir_json_schema()
    return _unroll(schema, schema.get("$defs", {}), (), logical_depth)
