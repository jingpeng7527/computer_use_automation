"""ReplayResult: what `cua replay` hands back to its caller (an AI agent, in
production, with no human watching).

The four-way `status` is the central contract decision: a caller branches on
it alone and never needs to parse a stack trace or guess whether a response
is an error.

    success           goal reached; success_condition verified; outputs filled
    business_outcome  a DECLARED RuntimeMatch fired -- not an error; caller
                       switches on outcome.code
    failed            hard failure; failure names the step and expected-vs-observed
    escalated         handed to a human; escalation carries a handle to the
                       SAME live session, not a fresh one
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .common import OutputValue

FailureKind = Literal[
    "target_not_found",  # every locator layer was exhausted
    "ambiguous_target",  # a layer matched more than one element
    "checkpoint_failed",  # reached the step, but the resulting state was wrong
    "timeout",  # a wait condition never became true
    "unexpected_dialog",  # an interstitial appeared with no matching runtime_match
    "navigation_error",  # the page failed to load
    "app_error",  # the target application rendered its own error page
    "policy_blocked",  # the action was outside the allowlist, or IRREVERSIBLE and refused
    "recovery_exhausted",  # a runtime_match retried up to max_retries and gave up
    "param_invalid",  # an input failed ParamSpec.pattern before replay started
]


class StepResult(BaseModel):
    step_id: str
    intent: str
    # "outcome" is distinct from "failed": it means a terminal runtime_match
    # fired at this step -- the run stopped here on purpose (see
    # ReplayResult.status="business_outcome"), not because anything broke.
    # Conflating the two in this per-step trace would misreport a correct
    # "no such member" run as a broken one to whoever reads the evidence.
    status: Literal["ok", "recovered", "skipped", "outcome", "failed"]
    # Which of the 4 locator layers actually resolved this step's target.
    # None when the step has no target (navigate/bare wait). Recorded across
    # many runs, this is the drift signal: steady demotion from layer 1 to a
    # deeper layer means the app is changing underneath the artifact.
    locator_layer_hit: int | None = None
    recoveries_applied: list[str] = Field(default_factory=list)
    duration_ms: int
    # Typed, not downgraded to a string -- a money output stays a Money
    # object here too. Sensitive values are redacted per OutputSpec.sensitivity
    # by replacing them with a masked *string* placeholder before this is
    # populated (e.g. "<pii:5 digits>"), which OutputValue's `str` arm covers.
    extracted: dict[str, OutputValue] = Field(default_factory=dict)


class FailureDetail(BaseModel):
    step_id: str
    step_intent: str
    kind: FailureKind
    expected: str  # from the checkpoint/target description
    observed: str  # what was actually there (redacted per policy)
    screenshot_ref: str | None = None
    retryable: bool = False


class BusinessOutcomeResult(BaseModel):
    code: str
    description: str
    detected_after_step: str
    outputs: dict[str, OutputValue] = Field(default_factory=dict)  # partial, per OutputSpec


class EscalationRef(BaseModel):
    intervention_id: str
    reason: str
    raised_at_step: str
    session_handle: str  # how the operator attaches to the SAME live session
    resolved: bool = False
    operator_actions: list[str] = Field(default_factory=list)


class ReplayResult(BaseModel):
    capability_id: str
    capability_version: int
    run_id: str
    status: Literal["success", "business_outcome", "failed", "escalated"]

    outputs: dict[str, OutputValue] | None = None
    outcome: BusinessOutcomeResult | None = None
    failure: FailureDetail | None = None
    escalation: EscalationRef | None = None

    steps: list[StepResult] = Field(default_factory=list)
    started_at: datetime
    ended_at: datetime
    evidence_dir: str
    redactions: list[str] = Field(default_factory=list)  # which fields/patterns were masked

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> ReplayResult:
        """Makes the four-way contract mechanically enforced, not just a
        convention: the payload field must match `status`, and no other
        payload field may be set."""
        payload_by_status = {
            "success": self.outputs,
            "business_outcome": self.outcome,
            "failed": self.failure,
            "escalated": self.escalation,
        }
        expected = payload_by_status[self.status]
        if expected is None:
            raise ValueError(f"status={self.status!r} requires its matching payload field")
        others = {k: v for k, v in payload_by_status.items() if k != self.status}
        set_others = [k for k, v in others.items() if v is not None]
        if set_others:
            raise ValueError(f"status={self.status!r} but also set: {set_others}")
        return self
