"""Contracts for the human card, evaluation evidence, and compliance bundle.

This package does not convert NL to ODRL, Cedar, or Rego. It only gates
whether a release of that (separate) system is documented and measured
well enough to ship.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RiskTier(str, Enum):
    LOW = "low"
    LIMITED = "limited"
    HIGH = "high"
    UNACCEPTABLE = "unacceptable"


class AutomationLevel(str, Enum):
    INFORMATION_ONLY = "information_only"
    DECISION_SUPPORT = "decision_support"
    HUMAN_IN_THE_LOOP = "human_in_the_loop"
    HUMAN_ON_THE_LOOP = "human_on_the_loop"
    AUTONOMOUS = "autonomous"


_AUTOMATION_RANK = {
    AutomationLevel.INFORMATION_ONLY: 0,
    AutomationLevel.DECISION_SUPPORT: 1,
    AutomationLevel.HUMAN_IN_THE_LOOP: 2,
    AutomationLevel.HUMAN_ON_THE_LOOP: 3,
    AutomationLevel.AUTONOMOUS: 4,
}


def automation_rank(level: AutomationLevel | str) -> int:
    return _AUTOMATION_RANK[AutomationLevel(level)]


class ReviewProcess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    sla_days: int = Field(gt=0, le=90)
    required_before_enforce: bool


class DecisionRights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    automation_level: AutomationLevel
    sole_determinant: bool
    human_override: bool
    attach_to_pep_requires_signoff: bool
    fail_closed_on_ambiguity: bool
    review: ReviewProcess


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_email: str
    model_risk_email: str
    ethics_escalation: str


class KnownFailureMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slice: str
    handling: str
    description: str


class RegulatoryBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eu_ai_act_role: str
    nist_ai_rmf_functions: list[str]
    odrl_profile: str
    authorization_targets: list[str]


class StaticCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str
    model_id: str
    owner_team: str
    risk_tier: RiskTier
    last_reviewed: date
    recertification_due: date
    pipeline: list[str]
    intended_use: str
    intended_users: list[str]
    limitations: str
    ethical_considerations: str
    out_of_scope: list[str]
    prohibited_uses: list[str]
    decision_rights: DecisionRights
    contact: Contact
    regulatory_basis: RegulatoryBasis
    known_failure_modes: list[KnownFailureMode] = Field(default_factory=list)
    evaluation_data_summary: str | None = None
    caveats_and_recommendations: str | None = None

    @field_validator(
        "model_name",
        "model_id",
        "owner_team",
        "intended_use",
        "limitations",
        "ethical_considerations",
        mode="before",
    )
    @classmethod
    def strip_nonempty(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("must not be empty")
        return value

    @field_validator("intended_users", "out_of_scope", "prohibited_uses", "pipeline")
    @classmethod
    def nonempty_lists(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if str(item).strip()]
        if not cleaned:
            raise ValueError("must contain at least one entry")
        return cleaned


class Lineage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    evaluated_on: date
    gold_set_hash: str
    model_artifact_hash: str
    eval_harness_git_sha: str


class Fidelity(BaseModel):
    """Conversion quality vs a frozen gold set. Not a self-attested PASS."""

    model_config = ConfigDict(extra="ignore")

    gold_n: int = Field(ge=0)
    semantic_match: float = Field(ge=0, le=1)
    odrl_constraint_preservation: float = Field(ge=0, le=1)
    over_permission_rate: float = Field(ge=0, le=1)
    under_permission_rate: float = Field(ge=0, le=1)
    cedar_roundtrip_agree: float = Field(ge=0, le=1)
    rego_roundtrip_agree: float = Field(ge=0, le=1)


class EvaluationSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    n: int = Field(ge=0)
    semantic_match: float = Field(ge=0, le=1)
    over_permission_rate: float = Field(ge=0, le=1)
    in_production: bool


class DynamicMetrics(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model_id: str
    version: str
    lineage: Lineage
    fidelity: Fidelity
    construct_coverage: dict[str, int]
    slices: list[EvaluationSlice] = Field(default_factory=list)


class BundleAttestations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    not_legal_advice: bool
    no_auto_pep_attach: bool
    fail_closed_on_ambiguity: bool
    gold_set_frozen: bool


class ComplianceBundleSpec(BaseModel):
    """Human-authored inventory of what a release must carry."""

    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    system_id: str
    owner_team: str
    required_artifacts: list[str]
    attestations: BundleAttestations
    exhibit_paths: list[str]


def parse_static(raw: dict[str, Any]) -> StaticCard:
    return StaticCard.model_validate(raw)


def parse_dynamic(raw: dict[str, Any]) -> DynamicMetrics:
    return DynamicMetrics.model_validate(raw)


def parse_bundle(raw: dict[str, Any]) -> ComplianceBundleSpec:
    return ComplianceBundleSpec.model_validate(raw)
