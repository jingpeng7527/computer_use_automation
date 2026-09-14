"""SurfaceAdapter: the seam between "how we perceive/act on a surface" and
"the recorded flow" (REPORT.md sec 4).

Everything above this seam speaks only in role / accessible name / label /
text -- nothing in this file names a CSS selector, a frame, or a DOM node.
Those live inside WebAdapter's own declared strategy kinds (schema.locator's
`css` and `bbox` layers); a future DesktopAdapter or VisionAdapter would
simply not advertise them. `wait_for` is its own method, not folded into
`resolve`, because a step's `wait` must be a declared, bounded, loggable
operation -- never a hidden sleep inside element lookup. `location()` is
deliberately not `current_url()`: the allowlist check and the drift
fingerprint both need to know where the session is, and a desktop adapter
could still answer that honestly without faking a URL.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from cua.schema import Action, Condition, Target


class BBox(BaseModel):
    x: float
    y: float
    w: float
    h: float

    def contains_point(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def vertical_overlap(self, top: float, bottom: float) -> bool:
        """True if this box's vertical extent shares any span with [top, bottom)
        -- used to decide "is this on the same visual row" for label_anchor."""
        return self.y < bottom and (self.y + self.h) > top


class InteractiveNode(BaseModel):
    """One element as perceived on the surface -- interactive or not. A
    plain-text table cell (a legacy app's only "label") has role=None and
    interactive=False but is still here: label-anchor resolution and
    role_name checkpoints both need to find it, not just the clickable
    things."""

    node_id: str  # stable for one snapshot; WebAdapter re-locates by it
    role: str | None = None
    name: str | None = None
    text: str = ""
    value: str | None = None  # current value, for input/select/textarea only
    tag: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)
    bbox: BBox
    interactive: bool = False


SurfaceSnapshot = list[InteractiveNode]


class Location(BaseModel):
    origin: str
    path: str


class LocatorAttempt(BaseModel):
    """One strategy tried during resolve() -- becomes the drift telemetry on
    StepResult.locator_layer_hit and the locator trace on a FailureDetail."""

    layer: int
    kind: str
    match_count: int
    matched: bool


class ResolutionResult(BaseModel):
    resolved: bool
    layer: int | None = None
    node: InteractiveNode | None = None
    attempts: list[LocatorAttempt] = Field(default_factory=list)


class SurfaceAdapter(Protocol):
    def observe(self) -> SurfaceSnapshot: ...

    def resolve(self, target: Target) -> ResolutionResult: ...

    def act(
        self,
        action: Action,
        resolution: ResolutionResult | None = None,
        value: str | None = None,
    ) -> str | None:
        """Perform `action`. `resolution` is required for anything that acts
        on a control (click/type/select/read); `value` carries the ONE
        piece of externally-resolved data the action needs, if any --
        the already-interpolated URL for navigate, or the already-resolved
        literal for type/select. The adapter never resolves an
        `{{input.x}}` / `{{ctx.x}}` reference itself; that is the
        executor's job (replay/agent loop), so the same adapter works
        whether the value came from a caller's param or a discovery-time
        model decision. Returns the read value for a `read` action, else
        None."""
        ...

    def wait_for(self, condition: Condition, timeout_ms: int) -> bool: ...

    def location(self) -> Location: ...
