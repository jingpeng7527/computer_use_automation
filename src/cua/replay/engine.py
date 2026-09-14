"""Deterministic replay: reads a Capability's step list and executes it in
order, with no model in the decision loop. Implements exactly the ordering
REPORT.md sec 3 / 系统设计.md sec 3.1 specify:

    act -> wait -> observe (ONE snapshot) -> match TERMINAL runtime_matches
    first -> then assert the step's checkpoint -> else match the
    remaining (non-terminal) runtime_matches on the SAME snapshot -> else
    FAILED (checkpoint_failed)

Terminal matches are checked before the checkpoint on purpose: a page
reading "no member records match" will never satisfy a checkpoint
expecting a balance heading, and waiting for that assertion to time out
would turn a business outcome into a slow, misreported failure.

Recoverable entries retry the SAME step, bounded three ways -- a
per-matcher `max_retries`, the capability's `recovery_budget.per_run`, and
depth=1 (a recovery action never re-enters this dispatch, so nesting is
structurally impossible, not merely disallowed).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from cua.safety import Policy, check_allowed, gate
from cua.schema import (
    BusinessOutcomeResult,
    Capability,
    Click,
    EscalationRef,
    FailureDetail,
    Navigate,
    Read,
    ReplayResult,
    RoleNameStrategy,
    RuntimeMatch,
    Select,
    Step,
    StepResult,
    Target,
    TypeText,
)
from cua.surface import Location, ResolutionResult, SurfaceAdapter, SurfaceSnapshot
from cua.surface.resolve import evaluate_condition, resolve_against_snapshot

from .values import resolve_ref


class _Stop(Exception):
    """Internal control-flow signal: replay is done, with this result."""

    def __init__(self, result: ReplayResult) -> None:
        self.result = result
        super().__init__(result.status)


@dataclass
class _RunState:
    capability: Capability
    params: dict[str, str]
    adapter: SurfaceAdapter
    policy: Policy
    run_id: str
    started_at: datetime
    evidence_dir: str
    ctx: dict[str, str]
    outputs: dict[str, str]
    step_results: list[StepResult]
    recovery_used: int = 0


def replay(
    capability: Capability,
    params: dict[str, str],
    adapter: SurfaceAdapter,
    policy: Policy,
    *,
    run_id: str,
    evidence_dir: str = "",
) -> ReplayResult:
    started_at = datetime.now(UTC)
    state = _RunState(
        capability=capability,
        params=params,
        adapter=adapter,
        policy=policy,
        run_id=run_id,
        started_at=started_at,
        evidence_dir=evidence_dir,
        ctx={},
        outputs={},
        step_results=[],
    )

    param_error = _validate_params(capability, params)
    if param_error is not None:
        return _finish(state, "failed", failure=param_error)

    try:
        for step in capability.steps:
            _run_step(state, step)
    except _Stop as stop:
        return stop.result

    location = state.adapter.location()
    snapshot = state.adapter.observe()
    if _all_hold(capability.success_condition.all_of, snapshot, location):
        return _finish(state, "success", outputs=dict(state.outputs))

    return _finish(
        state,
        "failed",
        failure=FailureDetail(
            step_id=capability.steps[-1].id if capability.steps else "",
            step_intent="success_condition",
            kind="checkpoint_failed",
            expected=capability.success_condition.description,
            observed=f"at {location.origin}{location.path}",
            retryable=False,
        ),
    )


def _validate_params(capability: Capability, params: dict[str, str]) -> FailureDetail | None:
    for p in capability.inputs:
        value = params.get(p.name)
        if value is None:
            if p.required:
                return FailureDetail(
                    step_id="",
                    step_intent="validate inputs",
                    kind="param_invalid",
                    expected=f"required input {p.name!r}",
                    observed="missing",
                )
            continue
        if p.pattern and not re.fullmatch(p.pattern, value):
            return FailureDetail(
                step_id="",
                step_intent="validate inputs",
                kind="param_invalid",
                expected=f"input {p.name!r} matching {p.pattern!r}",
                observed="<redacted>" if p.sensitivity != "none" else value,
            )
    return None


def _all_hold(conditions, snapshot: SurfaceSnapshot, location: Location) -> bool:
    return all(evaluate_condition(c, snapshot, location) for c in conditions)


def _eligible_matches(
    matches: list[RuntimeMatch], step_id: str, terminal: bool
) -> list[RuntimeMatch]:
    return [
        m
        for m in matches
        if m.terminal is terminal and (m.after_step is None or m.after_step == step_id)
    ]


def _resolve_target(state: _RunState, step: Step) -> ResolutionResult | None:
    if step.target is None:
        return None
    snapshot = state.adapter.observe()
    return resolve_against_snapshot(step.target, snapshot)


def _act(state: _RunState, step: Step, resolution: ResolutionResult | None) -> str | None:
    action = step.action
    if isinstance(action, Navigate):
        return state.adapter.act(action, value=action.url_template)
    if isinstance(action, Click):
        return state.adapter.act(action, resolution=resolution)
    if isinstance(action, TypeText):
        value = resolve_ref(action.value_from, state.params, state.ctx)
        return state.adapter.act(action, resolution=resolution, value=value)
    if isinstance(action, Select):
        value = resolve_ref(action.value_from, state.params, state.ctx)
        return state.adapter.act(action, resolution=resolution, value=value)
    if isinstance(action, Read):
        value = state.adapter.act(action, resolution=resolution) or ""
        if action.into.startswith("ctx."):
            state.ctx[action.into.removeprefix("ctx.")] = value
        else:
            state.outputs[action.into] = value
        return value
    return state.adapter.act(action, resolution=resolution)  # Wait


def _run_step(state: _RunState, step: Step) -> None:
    capability = state.capability
    start = time.monotonic()

    resolution = _resolve_target(state, step)
    target_missing = step.target is not None and (resolution is None or not resolution.resolved)

    # A target that can't be found is NOT an immediate hard failure -- it's
    # exactly what happens when discovery's happy-path steps get diverted to
    # a legitimate business-outcome page instead (there's no "View" link on
    # a "no member records match" page). So this falls through to the same
    # terminal-match check every other divergence goes through, below,
    # rather than stopping here. If a control action still can't proceed
    # once resolution.resolved is confirmed, it always has a resolution.
    wait_ok = True
    if not target_missing:
        destination = step.action.url_template if isinstance(step.action, Navigate) else None
        location = state.adapter.location()
        allow = check_allowed(
            step.action.type, state.policy, current_location=location, destination=destination
        )
        if not allow.allowed:
            _stop_failed(
                state, step, kind="policy_blocked", expected="allowlisted action", observed=allow.reason
            )

        risk = gate(step.action.type, state.policy, resolution=resolution, destination=destination)
        if not risk.allowed_unattended:
            _stop_escalated(state, step, reason=risk.reason)

        try:
            _act(state, step, resolution)
        except Exception as exc:  # noqa: BLE001 -- becomes a hard failure, not swallowed
            _stop_failed(state, step, kind="app_error", expected="action to succeed", observed=str(exc))

        if step.wait is not None:
            wait_ok = state.adapter.wait_for(step.wait.until, step.wait.timeout_ms)

    # ONE snapshot; the terminal-match check, the checkpoint, and (if that
    # fails) the remaining-match fallback all read this same observation.
    snapshot = state.adapter.observe()
    location = state.adapter.location()

    terminal = _eligible_matches(capability.runtime_matches, step.id, terminal=True)
    hit = next((m for m in terminal if evaluate_condition(m.detect, snapshot, location)), None)
    if hit is not None:
        _apply_match(state, step, hit, resolution, start)
        return

    if target_missing:
        _classify_or_fail(
            state,
            step,
            resolution,
            start,
            snapshot,
            location,
            kind="target_not_found",
            expected=step.target.strategies[0].rationale or "a resolvable target",
        )
        return

    checkpoint_ok = _all_hold(step.checkpoint.all_of, snapshot, location) if step.checkpoint else True
    if wait_ok and checkpoint_ok:
        state.step_results.append(
            StepResult(
                step_id=step.id,
                intent=step.intent,
                status="ok",
                locator_layer_hit=resolution.layer if resolution else None,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        )
        return

    kind = "timeout" if not wait_ok else "checkpoint_failed"
    _classify_or_fail(state, step, resolution, start, snapshot, location, kind=kind)


def _classify_or_fail(
    state: _RunState,
    step: Step,
    resolution: ResolutionResult | None,
    start: float,
    snapshot: SurfaceSnapshot,
    location: Location,
    *,
    kind: str,
    expected: str | None = None,
) -> None:
    remaining = _eligible_matches(state.capability.runtime_matches, step.id, terminal=False)
    hit = next((m for m in remaining if evaluate_condition(m.detect, snapshot, location)), None)
    if hit is not None:
        _apply_match(state, step, hit, resolution, start)
        return
    if expected is None:
        expected = step.checkpoint.description if step.checkpoint else "no declared state to check"
    _stop_failed(
        state,
        step,
        kind=kind,
        expected=expected,
        observed=f"at {location.origin}{location.path}",
    )


def _apply_match(
    state: _RunState,
    step: Step,
    match: RuntimeMatch,
    resolution: ResolutionResult | None,
    start: float,
) -> None:
    if match.category == "business_outcome":

        state.step_results.append(
            StepResult(
                step_id=step.id,
                intent=step.intent,
                status="outcome",
                locator_layer_hit=resolution.layer if resolution else None,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        )
        outcome_outputs = {k: state.outputs[k] for k in match.result_outputs if k in state.outputs}
        raise _Stop(
            _finish(
                state,
                "business_outcome",
                outcome=BusinessOutcomeResult(
                    code=match.result_code or "",
                    description=match.id,
                    detected_after_step=step.id,
                    outputs=outcome_outputs,
                ),
            )
        )

    if match.category == "hard_failure":
        _stop_failed(
            state, step, kind="app_error", expected="no hard-failure condition", observed=match.id
        )
        return

    # recoverable
    budget = state.capability.recovery_budget
    if state.recovery_used >= budget.per_run:
        _stop_failed(
            state, step, kind="recovery_exhausted", expected="recovery budget remaining", observed=match.id
        )
        return
    if match.recovery is None:
        _stop_failed(state, step, kind="app_error", expected="a recovery action", observed=match.id)
        return

    state.recovery_used += 1
    if match.recovery.do == "dismiss_dialog" and match.recovery.target_role:

        dismiss_target = Target(
            strategies=[
                RoleNameStrategy(role=match.recovery.target_role, name=match.recovery.target_name or "")
            ]
        )
        dismiss_resolution = resolve_against_snapshot(dismiss_target, state.adapter.observe())
        if dismiss_resolution.resolved:
            state.adapter.act(Click(), resolution=dismiss_resolution)
    elif match.recovery.do == "reload" and isinstance(step.action, Navigate):
        state.adapter.act(step.action, value=step.action.url_template)
    # "retry_step" / "wait": fall through and simply retry below

    state.step_results.append(
        StepResult(
            step_id=step.id,
            intent=step.intent,
            status="recovered",
            recoveries_applied=[match.id],
            locator_layer_hit=resolution.layer if resolution else None,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    )
    _run_step(state, step)  # retry the SAME step once, depth=1 (no recursion into recovery itself)


def _stop_failed(state: _RunState, step: Step, *, kind: str, expected: str, observed: str) -> None:
    raise _Stop(
        _finish(
            state,
            "failed",
            failure=FailureDetail(
                step_id=step.id, step_intent=step.intent, kind=kind, expected=expected, observed=observed
            ),
        )
    )


def _stop_escalated(state: _RunState, step: Step, *, reason: str) -> None:

    raise _Stop(
        _finish(
            state,
            "escalated",
            escalation=EscalationRef(
                intervention_id=f"{state.run_id}-{step.id}",
                reason=reason,
                raised_at_step=step.id,
                session_handle=state.run_id,
            ),
        )
    )


def _finish(state: _RunState, status: str, **payload) -> ReplayResult:
    return ReplayResult(
        capability_id=state.capability.capability_id,
        capability_version=state.capability.version,
        run_id=state.run_id,
        status=status,
        steps=state.step_results,
        started_at=state.started_at,
        ended_at=datetime.now(UTC),
        evidence_dir=state.evidence_dir,
        **payload,
    )
