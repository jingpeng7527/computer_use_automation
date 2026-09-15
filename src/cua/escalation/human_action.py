"""Records what a human operator actually did during a handoff, as a
structured evidence field alongside `intervention.json` -- not just that
control changed hands, but the concrete before/after state and whether the
thing the automation was stuck on actually got done.

Borrowed as a *principle* from a competing implementation's operator-action
evidence, deliberately without its synchronous blocking operator callback:
this project's operator interface stays the SQLite broker plus same-session
resume (`ControlBroker`, `find_resume_point`) -- see REPORT.md sec 5 for why
that's the production-shaped choice. `human_performed_pending_action` is
derived from `find_resume_point`'s own mechanical check (did the stuck
step's checkpoint end up satisfied?), never from the operator's self-report;
`release_note` is kept alongside it as the operator's own account, clearly
separate from the derived fact.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from cua.safety import Policy, redact_text

from .resume import ResumePoint

if TYPE_CHECKING:
    from .broker import ControlRow


def record_human_action(
    *,
    evidence_dir: str,
    run_id: str,
    step_id: str,
    control: ControlRow | None,
    resume_decision: ResumePoint,
    before_url: str,
    before_screenshot_ref: str | None,
    after_url: str,
    after_screenshot_ref: str | None,
    policy: Policy,
) -> dict:
    record = {
        "run_id": run_id,
        "step_id": step_id,
        "claimed_by": control.holder if control else None,
        "claimed_at": control.claimed_at if control else None,
        "released_at": control.updated_at if control else None,
        "operator_note": control.release_note if control and control.release_note else None,
        "before": {
            "url": redact_text(before_url, policy),
            "screenshot_ref": before_screenshot_ref,
        },
        "after": {
            "url": redact_text(after_url, policy),
            "screenshot_ref": after_screenshot_ref,
        },
        # The mechanically-derived fact (from find_resume_point re-checking
        # the actual page, not from anyone's say-so): did the operator
        # complete the step the automation was stuck on, or the whole
        # capability, before releasing control back?
        "resume_decision": resume_decision,
        "human_performed_pending_action": resume_decision in ("success", "step"),
        "recorded_at": time.time(),
    }
    out_dir = Path(evidence_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "human_action.json").write_text(json.dumps(record, indent=2))
    return record
