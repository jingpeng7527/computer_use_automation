"""Overlay: a tenant-specific delta applied to a base Capability.

Resolved by apply_overlay() (src/cua/overlay/resolver.py) -- this module
defines the shape; schema/store.py's ArtifactStore gives the "one base,
per-tenant deltas" storage layout this assumes.

Four operations, not one, because real tenant divergence is not only
relabelling: institutions running the same product may add a required
field, point an entry step at a different URL, or omit a confirmation
screen, and an overlay that can only rewrite a target cannot express any
of the last three.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .capability import ParamSpec
from .locator import Target


class ReplaceTarget(BaseModel):
    op: Literal["replace_target"] = "replace_target"
    step_id: str
    target: Target


class ReplaceValue(BaseModel):
    """Overrides a step's own literal action value -- e.g. a Navigate
    step's url_template. Distinct from ReplaceTarget: a target says how to
    FIND a control on the page; this says what literal value the step's
    own action carries (the entry route itself is the concrete case this
    exists for -- see the second tenant fixture in tests/evidence)."""

    op: Literal["replace_value"] = "replace_value"
    step_id: str
    value: str


class InsertAfter(BaseModel):
    op: Literal["insert_after"] = "insert_after"
    step_id: str
    step: dict  # a Step-shaped dict; validated against schema.steps.Step when the overlay is applied


class Skip(BaseModel):
    op: Literal["skip"] = "skip"
    step_id: str


Override = Annotated[
    ReplaceTarget | ReplaceValue | InsertAfter | Skip,
    Field(discriminator="op"),
]


class Overlay(BaseModel):
    extends: str  # "capability_id@version"
    tenant_id: str
    overrides: list[Override] = Field(default_factory=list)
    # New inputs this tenant's flow needs that the base doesn't declare --
    # e.g. a mandatory branch selector on an insert_after step. Kept
    # separate from `overrides` because it changes the capability's own
    # I/O contract, not just its steps.
    add_inputs: list[ParamSpec] = Field(default_factory=list)
    # Replaces AppProfile.base_url / allowed_route_patterns for this
    # tenant's resolved capability -- the artifact-declared scope
    # safety/allowlist.py intersects with policy.yaml's own allowlist.
    # None/empty means "keep the base's declared scope unchanged"; this is
    # itself still only ever a NARROWING of policy.yaml, never a grant.
    base_url: str | None = None
    allowed_route_patterns: list[str] | None = None
