"""Overlay: a tenant-specific delta applied to a base Capability.

Not resolved/applied until the multi-tenant stretch phase -- this module
defines the shape only for now (schema/store.py's ArtifactStore already
gives the "one base, per-tenant deltas" storage layout this assumes).

Three operations, not one, because real tenant divergence is not only
relabelling: institutions running the same product may add a required field
or omit a confirmation screen, and an overlay that can only rewrite a
target cannot express either.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .locator import Target


class ReplaceTarget(BaseModel):
    op: Literal["replace_target"] = "replace_target"
    step_id: str
    target: Target


class InsertAfter(BaseModel):
    op: Literal["insert_after"] = "insert_after"
    step_id: str
    step: dict  # a Step-shaped dict; validated against schema.steps.Step when the overlay is applied


class Skip(BaseModel):
    op: Literal["skip"] = "skip"
    step_id: str


Override = Annotated[
    ReplaceTarget | InsertAfter | Skip,
    Field(discriminator="op"),
]


class Overlay(BaseModel):
    extends: str  # "capability_id@version"
    tenant_id: str
    overrides: list[Override] = Field(default_factory=list)
