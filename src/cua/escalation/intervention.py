"""Raises an intervention request: routes the context a human operator
needs to act on, without reconstructing the run from scratch (系统设计 sec
5.2 -- capability/goal, run id, step id, reason, expected vs observed,
session handle). Redaction applies to this payload exactly as it does to
replay evidence and logs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from cua.safety import Policy, redact_text

from .broker import ControlBroker


def raise_intervention(
    *,
    broker: ControlBroker,
    run_id: str,
    capability_id: str,
    goal: str,
    step_id: str,
    reason: str,
    expected: str,
    observed: str,
    evidence_dir: str,
    policy: Policy,
) -> str:
    intervention_id = f"{run_id}-intervention"
    payload = {
        "intervention_id": intervention_id,
        "run_id": run_id,
        "capability_id": capability_id,
        "goal": redact_text(goal, policy),
        "step_id": step_id,
        "reason": reason,
        "expected": expected,
        "observed": redact_text(observed, policy),
        "session_handle": run_id,  # how the operator's tooling finds THIS session, not a fresh one
        "raised_at": time.time(),
    }
    out_dir = Path(evidence_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "intervention.json").write_text(json.dumps(payload, indent=2))
    broker.mark_stuck(run_id, reason)
    return intervention_id
