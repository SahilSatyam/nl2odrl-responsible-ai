from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class IdentityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_model_id_match: bool = True
    require_artifact_hashes: list[str] = Field(default_factory=list)


class LifecyclePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_days_since_review: int
    recertification_must_be_future: bool = True


class DecisionRightsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    high_risk_max_automation: str
    forbid_sole_automated_decision: bool = True
    require_human_override: bool = True
    require_pep_signoff: bool = True
    require_fail_closed_on_ambiguity: bool = True


class FidelityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_gold_n: int
    min_semantic_match: float
    min_constraint_preservation: float
    max_over_permission_rate: float
    max_under_permission_rate: float
    min_cedar_agree: float
    min_rego_agree: float
    required_constructs: list[str]
    min_construct_n: int


class SlicePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_known_failure_modes_for_out_of_prod_slices: bool = True
    max_over_permission_if_in_production: float
    min_semantic_match_if_in_production: float


class GovernancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    policy_id: str
    owner: str
    frameworks: list[str]
    required_static_fields: list[str]
    required_pipeline_stages: list[str]
    identity: IdentityPolicy
    lifecycle: LifecyclePolicy
    decision_rights: DecisionRightsPolicy
    fidelity: FidelityPolicy
    slices: SlicePolicy
    strict_warnings_fail_ci: bool = False


def load_policy(path: str | Path) -> GovernancePolicy:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    return GovernancePolicy.model_validate(raw)
