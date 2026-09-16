"""Capability artifact schema + replay result contract.

    schema/common.py      Condition vocabulary, Money, Checkpoint
    schema/locator.py      Target -- the 4-layer, no-confidence locator model
    schema/steps.py         Step, Action, RiskLevel -- the ordered, surface-agnostic flow
    schema/capability.py    Capability -- the artifact itself, runtime_matches, recovery_budget
    schema/result.py        ReplayResult -- what replay hands back to the caller
    schema/overlay.py       Overlay -- tenant deltas over a base Capability (stretch)
    schema/store.py         ArtifactStore -- artifacts/<capability_id>/<version>.json
"""

from __future__ import annotations

from .capability import (
    AppProfile,
    Capability,
    OutputSpec,
    ParamSpec,
    ParamType,
    Provenance,
    RecoveryAction,
    RecoveryBudget,
    RuntimeMatch,
    Sensitivity,
)
from .common import (
    Checkpoint,
    Condition,
    Money,
    OutputValue,
    RoleName,
    TextContains,
    TextMatcher,
    UrlMatches,
    ValueEquals,
    parse_ref,
    validate_into,
)
from .locator import (
    BboxStrategy,
    CssStrategy,
    LabelAnchorStrategy,
    LocatorStrategy,
    RoleNameStrategy,
    Target,
)
from .overlay import InsertAfter, Overlay, Override, ReplaceTarget, ReplaceValue, Skip
from .result import (
    BusinessOutcomeResult,
    EscalationRef,
    FailureDetail,
    FailureKind,
    ReplayResult,
    StepResult,
)
from .steps import Action, Click, Navigate, Read, RiskLevel, Select, Step, TypeText, Wait, WaitSpec
from .store import ArtifactStore

__all__ = [
    "Action",
    "AppProfile",
    "ArtifactStore",
    "BboxStrategy",
    "BusinessOutcomeResult",
    "Capability",
    "Checkpoint",
    "Click",
    "Condition",
    "CssStrategy",
    "EscalationRef",
    "FailureDetail",
    "FailureKind",
    "InsertAfter",
    "LabelAnchorStrategy",
    "LocatorStrategy",
    "Money",
    "Navigate",
    "OutputSpec",
    "OutputValue",
    "Overlay",
    "Override",
    "ParamSpec",
    "ParamType",
    "Provenance",
    "Read",
    "RecoveryAction",
    "RecoveryBudget",
    "ReplaceTarget",
    "ReplaceValue",
    "ReplayResult",
    "RiskLevel",
    "RoleName",
    "RoleNameStrategy",
    "RuntimeMatch",
    "Select",
    "Sensitivity",
    "Skip",
    "Step",
    "StepResult",
    "Target",
    "TextContains",
    "TextMatcher",
    "TypeText",
    "UrlMatches",
    "ValueEquals",
    "Wait",
    "WaitSpec",
    "parse_ref",
    "validate_into",
]
