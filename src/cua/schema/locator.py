"""Target: how a step finds the control it acts on.

Exactly four ranked layers, tried in order; the first strategy that resolves
to EXACTLY ONE element wins (assignment 3.2's "reasoning about robustness"
lives in each strategy's `rationale`). There is deliberately no confidence
score anywhere in this file (系统设计.md sec 1.6 rejects that alternative:
the weights would have no defensible origin, and a blended score can't tell
a reviewer that layer 1 stopped working). Reliability is measured instead --
replay records which layer resolved each target (see schema/result.py,
StepResult.locator_layer_hit) -- not asserted up front.

Ambiguity (more than one match) is treated as non-resolution: a strategy
matching several elements is skipped, exactly like a strategy matching zero.
"replace_target this-then-that on the page" positional targeting is
excluded entirely -- an added field elsewhere on the page would silently
shift it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


class _StrategyBase(BaseModel):
    # Free text only -- never read by the resolution algorithm. It exists so
    # a human reviewer can see *why* this layer is (or isn't) robust, without
    # reopening the rejected static-confidence-score alternative: nothing
    # here is consumed programmatically.
    rationale: str | None = None


class RoleNameStrategy(_StrategyBase):
    """Layer 1: accessibility role + accessible name. Portable across web
    and desktop; survives markup and layout changes."""

    layer: Literal[1] = 1
    type: Literal["role_name"] = "role_name"
    role: str
    name: str


class LabelAnchorStrategy(_StrategyBase):
    """Layer 2: find a stable nearby label/text, then walk structurally to
    the real target. The workhorse for legacy table layouts where the
    control itself has no accessible name."""

    layer: Literal[2] = 2
    type: Literal["label_anchor"] = "label_anchor"
    label: str
    relation: Literal["same_row_input", "same_row_cell", "next_cell", "following_field"]
    nth: int = 0


class CssStrategy(_StrategyBase):
    """Layer 3: web-only fallback. A generated id or hashed class does not
    count as robust here -- this is for a stable, human-authored selector."""

    layer: Literal[3] = 3
    type: Literal["css"] = "css"
    selector: str


class BboxStrategy(_StrategyBase):
    """Layer 4: last resort for canvas- or image-rendered surfaces."""

    layer: Literal[4] = 4
    type: Literal["bbox"] = "bbox"
    x: float
    y: float
    w: float
    h: float


LocatorStrategy = Annotated[
    RoleNameStrategy | LabelAnchorStrategy | CssStrategy | BboxStrategy,
    Field(discriminator="type"),
]


class Target(BaseModel):
    strategies: list[LocatorStrategy] = Field(min_length=1)

    @model_validator(mode="after")
    def _layers_strictly_increasing(self) -> Target:
        """The whole point of ranking is that replay tries them in a fixed,
        meaningful order (most-portable first) and can tell a reviewer which
        layer won. A list with layer 3 before layer 1, or two layer-1s,
        makes "first-wins" ambiguous and the drift signal meaningless."""
        layers = [s.layer for s in self.strategies]
        if layers != sorted(layers) or len(set(layers)) != len(layers):
            raise ValueError(
                f"Target.strategies layers must be strictly increasing with no "
                f"repeats, got {layers}"
            )
        return self
