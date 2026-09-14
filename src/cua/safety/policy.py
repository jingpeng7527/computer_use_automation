"""Loads config/policy.yaml into typed models.

Both the discovery loop and the replay engine load the SAME file through
this module -- there is exactly one place a policy decision gets made, and
it is never the artifact (系统设计 P5: "权限不由数据自报"). An artifact's
Step.risk_level is a discovered hint only; policy.yaml plus this loader are
what actually gate an action at run time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class AllowlistConfig(BaseModel):
    origins: list[str] = Field(default_factory=list)
    path_prefixes: list[str] = Field(default_factory=list)
    action_types: list[str] = Field(default_factory=list)


class RiskTierRule(BaseModel):
    action_types: list[str] = Field(default_factory=list)


class IrreversibleRule(BaseModel):
    name_contains: list[str] = Field(default_factory=list)
    path_patterns: list[str] = Field(default_factory=list)


class RiskTiers(BaseModel):
    safe_read: RiskTierRule = Field(default_factory=RiskTierRule)
    reversible_write: RiskTierRule = Field(default_factory=RiskTierRule)
    irreversible: IrreversibleRule = Field(default_factory=IrreversibleRule)


class RedactionConfig(BaseModel):
    patterns: dict[str, str] = Field(default_factory=dict)
    sensitive_field_names: list[str] = Field(default_factory=list)


class ExecutionBounds(BaseModel):
    max_steps: int = 25
    wall_clock_ceiling_s: float = 300
    per_wait_timeout_ms: int = 10_000
    recovery_budget_per_run: int = 5
    no_progress_max_consecutive_steps: int = 3


class KeepAlive(BaseModel):
    route: str = "/"
    interval_s: float = 60


class Policy(BaseModel):
    allowlist: AllowlistConfig
    risk_tiers: RiskTiers
    irreversible_policy: Literal["refuse", "require_confirm"] = "refuse"
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    execution_bounds: ExecutionBounds = Field(default_factory=ExecutionBounds)
    keep_alive: KeepAlive = Field(default_factory=KeepAlive)


def load_policy(path: Path | str = "config/policy.yaml") -> Policy:
    data = yaml.safe_load(Path(path).read_text())
    return Policy.model_validate(data)
