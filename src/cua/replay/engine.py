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

Recoverable entries retry the SAME step, bounded four ways -- a per-matcher
`max_retries`, the capability's own `recovery_budget.per_run`,
`policy.execution_bounds.recovery_budget_per_run` as the actual hard
ceiling the artifact's budget can only tighten and never loosen, and
depth=1 (a recovery action never re-enters this dispatch, so nesting is
structurally impossible, not merely disallowed).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from cua.escalation import (
    ControlBroker,
    KeepAliveThread,
    find_resume_point,
    raise_intervention,
    record_human_action,
)
from cua.safety import (
    BoundExceeded,
    ExecutionGuard,
    Policy,
    check_allowed,
    gate,
    redact_text,
    redact_value,
)
from cua.schema import (
    BusinessOutcomeResult,
    Capability,
    Click,
    EscalationRef,
    FailureDetail,
    Money,
    Navigate,
    OutputValue,
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
    guard: ExecutionGuard
    broker: ControlBroker | None = None
    recovery_used: int = 0
    match_retry_counts: dict[str, int] = field(default_factory=dict)


def replay(
    capability: Capability,
    params: dict[str, str],
    adapter: SurfaceAdapter,
    policy: Policy,
    *,
    run_id: str,
    evidence_dir: str = "",
    broker: ControlBroker | None = None,
    escalation_poll_s: float = 1.0,
    escalation_max_wait_s: float = 120.0,
) -> ReplayResult:
    """`broker` is optional so every existing caller (and every test) that
    doesn't pass one keeps the original behaviour: a stuck step returns
    immediately as "failed" or "escalated". When a broker IS given, a stuck
    step instead raises a real intervention and BLOCKS -- polling the same
    broker a separate `cua ops claim/release` invocation writes to -- until
    an operator releases control back, at which point find_resume_point
    decides whether to finish or continue from the next step. The browser
    is never closed and never reopened across this wait: it is the same
    live session throughout, which is the entire point.
    """
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
        guard=ExecutionGuard(policy),
        broker=broker,
    )

    if capability.status != "approved":
        # REPORT.md sec 7: "the smallest change with the largest safety
        # return." Unattended replay is the production path -- there is no
        # human watching to catch a draft artifact's mistakes -- so a
        # capability that hasn't been reviewed and promoted must never run
        # here regardless of what params or policy would otherwise allow.
        # Checked before param validation on purpose: whether THIS run's
        # inputs are valid is a question that only matters for a capability
        # eligible to run at all.
        return _finish(
            state,
            "failed",
            failure=FailureDetail(
                step_id="",
                step_intent="approval gate",
                kind="not_approved",
                expected="capability.status == 'approved'",
                observed=capability.status,
            ),
        )

    param_error = _validate_params(capability, params)
    if param_error is not None:
        return _finish(state, "failed", failure=param_error)

    steps = capability.steps
    i = 0
    while i < len(steps):
        step = steps[i]
        try:
            _run_step(state, step)
        except _Stop as stop:
            if broker is None or stop.result.status not in ("failed", "escalated"):
                return stop.result
            outcome, redacted_outputs, session_lost_detail = _escalate_and_wait(
                state,
                step,
                stop.result,
                broker,
                poll_s=escalation_poll_s,
                max_wait_s=escalation_max_wait_s,
            )
            if outcome == "success":
                return _finish(state, "success", outputs=redacted_outputs, redactions=[])
            if outcome == "resume_next":
                i += 1
                continue
            if outcome == "session_lost":
                assert session_lost_detail is not None
                return _finish(state, "failed", failure=session_lost_detail)
            return stop.result  # gave up: return the original, unresolved escalation/failure
        i += 1

    location = state.adapter.location()
    snapshot = state.adapter.observe()
    if _all_hold(capability.success_condition.all_of, snapshot, location):
        outputs, redacted = _redact_outputs(state, state.outputs)
        return _finish(state, "success", outputs=outputs, redactions=redacted)

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


_MONEY_PARSE_RE = re.compile(r"^\$?([\d,]+\.\d{2})\s+([A-Z]{3})$")


def _typed_value(raw: str, output_type: str) -> OutputValue:
    """Converts the raw string a Read step captured into the type its
    OutputSpec actually declares -- a "money" output stays a string only
    until this boundary, never all the way out to the caller. Falls back
    to the raw string if it doesn't parse; a replay should report an
    honest string over a fabricated number."""
    if output_type == "money":
        m = _MONEY_PARSE_RE.match(raw.strip())
        if m:
            # Decimal, not float: a float can't represent every two-decimal
            # dollar amount exactly, and at large magnitudes that shows up
            # as real cents lost (verified: "$99999999999999.99" rounds to
            # 9999999999999998 minor units via float*100, one cent short of
            # the correct 9999999999999999). REPORT.md promises money is
            # "never a float" -- true of the final type, but not previously
            # true of the arithmetic that produced it.
            cents = (Decimal(m.group(1).replace(",", "")) * 100).to_integral_value()
            return Money(amount_minor=int(cents), currency=m.group(2))
    elif output_type == "integer":
        try:
            return int(raw.strip())
        except ValueError:
            pass
    elif output_type == "boolean":
        return raw.strip().lower() in ("true", "1", "yes")
    return raw


def _redact_outputs(
    state: _RunState, outputs: dict[str, str]
) -> tuple[dict[str, OutputValue], list[str]]:
    """Masks any output whose OutputSpec declares a sensitivity, at the one
    boundary this matters -- what actually gets written into the result
    (and from there, evidence). Internal state (`state.outputs`) stays raw
    so a later step can still legitimately use the value; only what leaves
    via ReplayResult is masked. A value that WASN'T masked is also typed
    here, per its OutputSpec -- a masked string ("<pii:5 digits>") is left
    alone rather than fed back through a money/int parser."""
    spec_by_name = {o.name: o for o in state.capability.outputs}
    redacted: dict[str, OutputValue] = {}
    redacted_fields: list[str] = []
    for name, value in outputs.items():
        spec = spec_by_name.get(name)
        sensitivity = spec.sensitivity if spec else "none"
        masked = redact_value(value, sensitivity=sensitivity, field_name=name, policy=state.policy)
        if masked != value:
            redacted_fields.append(name)
            redacted[name] = masked
        else:
            redacted[name] = _typed_value(value, spec.type) if spec else value
    return redacted, redacted_fields


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

    # Bounds are the executor's to enforce, never the artifact's -- an
    # artifact with a very long step list, or a recoverable condition that
    # keeps recurring, must still terminate. This also covers the recursive
    # retry call at the bottom of _apply_match: each retry is another call
    # here, so it counts against the same step/wall-clock ceiling discovery
    # already enforces on itself.
    try:
        state.guard.check_step()
    except BoundExceeded as exc:
        _stop_failed(
            state, step, kind="bounds_exceeded", expected="within execution bounds", observed=exc.reason
        )

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
            step.action.type,
            state.policy,
            current_location=location,
            destination=destination,
            app_profile=state.capability.app_profile,
        )
        if not allow.allowed:
            _stop_failed(
                state, step, kind="policy_blocked", expected="allowlisted action", observed=allow.reason
            )

        risk = gate(step.action.type, state.policy, resolution=resolution, destination=destination)
        if not risk.allowed_unattended:
            if risk.escalate:
                _stop_escalated(state, step, reason=risk.reason)
            else:
                # irreversible_policy: refuse means never -- not "ask a human
                # to authorise it anyway". That's what require_confirm is
                # for. A hard, non-escalating failure is the only response
                # that actually matches "refuse".
                _stop_failed(
                    state, step, kind="policy_blocked", expected="not IRREVERSIBLE", observed=risk.reason
                )

        # REPORT.md sec 5: "Holding a token is not enough; automation asks
        # the broker whether it is still the holder immediately before each
        # action." Checked here, not once at the top of replay(), because an
        # operator can claim control at any point during a run, not only
        # after automation has already declared itself stuck -- a stale
        # PAUSED row from a reused run_id, or a claim racing a step that
        # hasn't hit trouble yet, both look identical from here: control is
        # no longer automation's, so it must stop after this check rather
        # than complete one more action first.
        if state.broker is not None and not state.broker.is_automations_turn(state.run_id):
            _stop_escalated(state, step, reason="control was claimed by an operator mid-run")

        try:
            _act(state, step, resolution)
        except Exception as exc:  # noqa: BLE001 -- becomes a hard failure, not swallowed
            _stop_failed(state, step, kind="app_error", expected="action to succeed", observed=str(exc))

        if step.wait is not None:
            # The artifact's own timeout is a ceiling the executor enforces,
            # not a number it trusts outright -- an artifact author (or a
            # discovery run) declaring an unreasonably long wait must not be
            # able to stall a replay past what policy allows.
            timeout_ms = min(step.wait.timeout_ms, state.policy.execution_bounds.per_wait_timeout_ms)
            wait_ok = state.adapter.wait_for(step.wait.until, timeout_ms)

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
        raw_outcome_outputs = {k: state.outputs[k] for k in match.result_outputs if k in state.outputs}
        outcome_outputs, redacted = _redact_outputs(state, raw_outcome_outputs)
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
                redactions=redacted,
            )
        )

    if match.category == "hard_failure":
        _stop_failed(
            state, step, kind="app_error", expected="no hard-failure condition", observed=match.id
        )
        return

    # recoverable -- bounded three ways, per REPORT.md sec 3: a per-matcher
    # max_retries (this condition specifically keeps recurring and isn't
    # getting better), the capability's own recovery_budget.per_run (too
    # many DIFFERENT conditions fired this run), AND
    # policy.execution_bounds.recovery_budget_per_run -- the actual hard
    # ceiling policy.yaml documents this as. The artifact's own budget can
    # only tighten that ceiling, never loosen it: checking only the
    # artifact's declared value would let a mis-declared (or malicious)
    # artifact set recovery_budget.per_run arbitrarily high and retry well
    # past what policy allows, caught only by max_steps -- a different
    # bound doing a job this one is supposed to do itself.
    match_retries = state.match_retry_counts.get(match.id, 0)
    if match_retries >= match.max_retries:
        _stop_failed(
            state,
            step,
            kind="recovery_exhausted",
            expected=f"{match.id!r} within its max_retries ({match.max_retries})",
            observed=match.id,
        )
        return

    if state.recovery_used >= state.capability.recovery_budget.per_run:
        _stop_failed(
            state, step, kind="recovery_exhausted", expected="recovery budget remaining", observed=match.id
        )
        return
    if not state.guard.use_recovery():
        _stop_failed(
            state,
            step,
            kind="recovery_exhausted",
            expected="within policy.execution_bounds.recovery_budget_per_run",
            observed=match.id,
        )
        return
    if match.recovery is None:
        _stop_failed(state, step, kind="app_error", expected="a recovery action", observed=match.id)
        return

    # Structural, not a trust in step.risk_level (a self-reported hint --
    # see the schema-level guard's comment in capability.py for why that
    # field is never what a safety decision keys on): Click/TypeText/Select
    # are never retry-eligible, determined by the action's own
    # discriminated type, not a label that could be wrong. This is the
    # backstop for an after_step=None matcher, which
    # Capability._referential_integrity can't check statically since it
    # could land on any step.
    #
    # IRREVERSIBLE needs no separate check here: "retry_step" falls through
    # to a recursive _run_step() call below, which re-resolves the target
    # and re-runs the SAME gate() every other execution goes through --
    # gate() already ran once on this exact resolution earlier in THIS
    # _run_step call (control could not have reached this far otherwise),
    # and runs again, fresh, on the retry. An IRREVERSIBLE step is refused
    # or escalated there, not retried past it -- adding a second
    # classify_risk() call here would just re-check state gate() already
    # checked, using the same (by now stale) resolution.
    if match.recovery.do == "retry_step" and isinstance(step.action, (Click, TypeText, Select)):
        _stop_failed(
            state,
            step,
            kind="recovery_refused",
            expected="a retry-eligible action (read / wait / navigate)",
            observed=f"{step.action.type!r} step {step.id!r} declared do='retry_step'",
        )
        return

    state.recovery_used += 1
    state.match_retry_counts[match.id] = match_retries + 1
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
    # Redaction at the persistence boundary: `observed` is about to be
    # written into a FailureDetail (and from there, evidence), so it gets
    # the same regex pass as everything else that leaves the process here.
    # The screenshot is the "richer signal on failure" evidence (sec 3.5)
    # alongside the structured log -- best-effort, treated as sensitive
    # (kept under the run's own evidence dir, never inlined into a log line).
    screenshot_ref = None
    if state.evidence_dir:
        screenshot_ref = f"{state.evidence_dir}/{step.id}-failure.png"
        state.adapter.screenshot(screenshot_ref)
    raise _Stop(
        _finish(
            state,
            "failed",
            failure=FailureDetail(
                step_id=step.id,
                step_intent=step.intent,
                kind=kind,
                expected=expected,
                observed=redact_text(observed, state.policy),
                screenshot_ref=screenshot_ref,
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


def _escalate_and_wait(
    state: _RunState,
    step: Step,
    stop_result: ReplayResult,
    broker: ControlBroker,
    *,
    poll_s: float,
    max_wait_s: float,
) -> tuple[str, dict[str, OutputValue], FailureDetail | None]:
    """Raises the intervention, then blocks -- polling the SAME broker a
    separate `cua ops claim/release` invocation writes to -- until control
    comes back as RESUMING, or `max_wait_s` elapses. The adapter/browser is
    never touched here except to observe: whatever fix happens, happens on
    the live session directly, outside this function. Returns one of
    ("success", outputs, None), ("resume_next", {}, None),
    ("give_up", {}, None), or ("session_lost", {}, FailureDetail)."""
    screenshot_ref = None
    if stop_result.failure is not None:
        reason, expected, observed = (
            stop_result.failure.kind,
            stop_result.failure.expected,
            stop_result.failure.observed,
        )
        screenshot_ref = stop_result.failure.screenshot_ref
    else:
        assert stop_result.escalation is not None
        reason, expected, observed = stop_result.escalation.reason, "human authorisation required", ""

    before_location = state.adapter.location()
    before_url = f"{before_location.origin}{before_location.path}"

    raise_intervention(
        broker=broker,
        run_id=state.run_id,
        capability_id=state.capability.capability_id,
        goal=state.capability.provenance.goal,
        step_id=step.id,
        reason=reason,
        expected=expected,
        observed=observed,
        evidence_dir=state.evidence_dir,
        policy=state.policy,
        screenshot_ref=screenshot_ref,
        completed_steps=[s.step_id for s in state.step_results],
    )

    # REPORT.md sec 5: while PAUSED and unclaimed, ping a SAFE_READ route so
    # the target app's idle-session timer doesn't expire during the
    # handoff. Out-of-band (its own HTTP client, not the paused page) so it
    # never disturbs the stuck state the operator needs to see. Started
    # here, not earlier, because there is nothing to keep alive before an
    # intervention has actually been raised; stopped unconditionally below
    # so a lingering thread never outlives this wait.
    keepalive = KeepAliveThread(
        base_url=state.adapter.location().origin,
        policy=state.policy,
        db_path=broker.db_path,
        run_id=state.run_id,
    )
    keepalive.start()
    try:
        waited = 0.0
        while waited < max_wait_s:
            row = broker.get_state(state.run_id)
            if row is not None and row.state == "RESUMING":
                after_screenshot_ref = None
                if state.evidence_dir:
                    after_screenshot_ref = f"{state.evidence_dir}/{step.id}-after-handoff.png"
                    state.adapter.screenshot(after_screenshot_ref)
                resume_point = find_resume_point(state.capability, step, state.adapter)
                after_location = state.adapter.location()
                if state.evidence_dir:
                    record_human_action(
                        evidence_dir=state.evidence_dir,
                        run_id=state.run_id,
                        step_id=step.id,
                        control=row,
                        resume_decision=resume_point,
                        before_url=before_url,
                        before_screenshot_ref=screenshot_ref,
                        after_url=f"{after_location.origin}{after_location.path}",
                        after_screenshot_ref=after_screenshot_ref,
                        policy=state.policy,
                    )
                broker.mark_resumed(state.run_id)
                if resume_point == "success":
                    # 系统设计 sec 5.5: the operator finished the work by hand
                    # -- report whatever outputs were captured before the
                    # stop, and run nothing further (partially completed
                    # work is often not idempotent).
                    outputs, _ = _redact_outputs(state, state.outputs)
                    return "success", outputs, None
                if resume_point == "step":
                    return "resume_next", {}, None
                # Neither candidate held. Before treating this as "the fix
                # didn't work, try again" (a second escalation), check
                # whether there is still a session to resume at all: if the
                # resumed page has drifted outside the allowlisted scope
                # entirely (a login redirect, a different origin), that is
                # session loss, not an unconvincing fix, and pretending
                # otherwise would resume "into" a session that was never
                # the one the run started in.
                location = state.adapter.location()
                if not check_allowed(
                    "read", state.policy, current_location=location, app_profile=state.capability.app_profile
                ).allowed:
                    return (
                        "session_lost",
                        {},
                        FailureDetail(
                            step_id=step.id,
                            step_intent=step.intent,
                            kind="session_lost",
                            expected="the resumed session to still be within the allowlisted app scope",
                            observed=redact_text(
                                f"now at {location.origin}{location.path}", state.policy
                            ),
                        ),
                    )
                return "give_up", {}, None  # a second escalation, not a guess
            time.sleep(poll_s)
            waited += poll_s
        return "give_up", {}, None
    finally:
        keepalive.stop()


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
