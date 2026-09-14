"""Resume: exactly two candidates, in order (系统设计 sec 5.5) -- never a
general "where am I" scan. Checkpoints collide by design: "on the member
search page" is true before step one, after a failed lookup, and after a
completed flow, so scanning every checkpoint in the artifact would match
several and have no way to choose. Bounding the search to the step that
was already known to be stuck, plus the capability's own terminal
condition, is what makes resume decidable at all.
"""

from __future__ import annotations

from typing import Literal

from cua.schema import Capability, Step
from cua.surface import SurfaceAdapter
from cua.surface.resolve import evaluate_condition

ResumePoint = Literal["success", "step", "none"]


def find_resume_point(capability: Capability, stuck_step: Step, adapter: SurfaceAdapter) -> ResumePoint:
    snapshot = adapter.observe()
    location = adapter.location()

    if all(evaluate_condition(c, snapshot, location) for c in capability.success_condition.all_of):
        # The operator finished the whole thing by hand -- do not re-run
        # anything after this; partially completed work is often not
        # idempotent (re-submitting a form twice, say).
        return "success"

    if stuck_step.checkpoint is not None and all(
        evaluate_condition(c, snapshot, location) for c in stuck_step.checkpoint.all_of
    ):
        # The operator completed exactly the step that was stuck -- resume
        # from the NEXT one, don't re-run this one.
        return "step"

    return "none"  # neither holds -- a second escalation, not a guess
