"""Step and Action: the ordered, surface-agnostic flow.

Action verbs are abstract, not Playwright calls, so the same step list can
replay against a legacy-web engine or a desktop accessibility driver later
(the seam described in REPORT.md sec 4). `risk_level` is a DISCOVERED HINT
ONLY -- the executor always re-classifies risk itself from the action type
and target at replay time (系统设计.md P5: "权限不由数据自报"); nothing here
is authoritative for a safety gate.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .common import Checkpoint, Condition, parse_ref, validate_into
from .locator import Target

RiskLevel = Literal["SAFE_READ", "REVERSIBLE_WRITE", "IRREVERSIBLE"]


class Navigate(BaseModel):
    type: Literal["navigate"] = "navigate"
    url_template: str  # e.g. "{base_url}/members/search"


class Click(BaseModel):
    type: Literal["click"] = "click"


class TypeText(BaseModel):
    type: Literal["type"] = "type"
    # "{{input.member_id}}" (caller-supplied) or "{{ctx.txn_token}}" (extracted
    # earlier this same run) -- the namespace prefix says where the value came
    # from and, for ctx, means it is always treated as sensitive (see
    # safety/redaction.py in a later phase).
    value_from: str
    submit: bool = False
    clear_first: bool = True

    @field_validator("value_from")
    @classmethod
    def _value_from_is_a_reference(cls, v: str) -> str:
        parse_ref(v)  # raises ValueError if not "{{input.x}}" / "{{ctx.x}}"
        return v


class Select(BaseModel):
    type: Literal["select"] = "select"
    value_from: str
    by: Literal["label", "value", "index"] = "label"

    @field_validator("value_from")
    @classmethod
    def _value_from_is_a_reference(cls, v: str) -> str:
        parse_ref(v)
        return v


class Read(BaseModel):
    type: Literal["read"] = "read"
    into: str  # "ctx.txn_token" (feeds a later step) or an OutputSpec name
    attribute: str | None = None  # None -> visible text

    @field_validator("into")
    @classmethod
    def _into_shape(cls, v: str) -> str:
        return validate_into(v)


class Wait(BaseModel):
    """A step whose entire purpose is to wait -- distinct from `WaitSpec`
    below, which any step can carry as a pre-condition."""

    type: Literal["wait"] = "wait"


Action = Annotated[
    Navigate | Click | TypeText | Select | Read | Wait,
    Field(discriminator="type"),
]


class WaitSpec(BaseModel):
    """Explicit, bounded wait attached to any step -- never a bare sleep.
    Exhausting this without `until` holding routes to the recoverable
    branch at replay time, not straight to a hard failure."""

    until: Condition
    timeout_ms: int = 10_000


# Actions that act ON a specific control -- these can't resolve "which
# element" without a Target. Navigate (goes to a URL) and Wait (waits on a
# Condition, not an element handle) are the only two verbs that can leave
# Step.target unset.
_ACTS_ON_A_CONTROL = (Click, TypeText, Select, Read)


class Step(BaseModel):
    id: str  # stable; referenced by RuntimeMatch.after_step and by results
    intent: str  # human-readable line for reviewers and logs
    action: Action
    risk_level: RiskLevel
    target: Target | None = None  # None only for navigate / a bare wait
    wait: WaitSpec | None = None
    checkpoint: Checkpoint | None = None

    @model_validator(mode="after")
    def _control_actions_need_a_target(self) -> Step:
        if isinstance(self.action, _ACTS_ON_A_CONTROL) and self.target is None:
            raise ValueError(
                f"step {self.id!r}: a {self.action.type!r} action acts on a "
                f"control and must declare a target"
            )
        return self
