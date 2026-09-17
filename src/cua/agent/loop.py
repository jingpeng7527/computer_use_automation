"""The discovery loop: observe -> decide -> act, one tool call per step.

Happy-path only for this phase -- deriving runtime_matches from a
deliberately-bad-input second run (the hardening pass) is a later phase;
an artifact compiled straight from here legitimately declares no business
outcomes yet, and that's correct, not incomplete.

Every step is gated the same way replay will eventually gate it: allowlist
first, then risk tier, with IRREVERSIBLE refused outright and never
auto-executed during discovery -- there is no "discovery override".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from cua.escalation import ControlBroker, KeepAliveThread, raise_intervention
from cua.safety import BoundExceeded, ExecutionGuard, Policy, check_allowed, gate
from cua.schema import Click, Navigate, Read, TypeText
from cua.surface import InteractiveNode, ResolutionResult, SurfaceAdapter, SurfaceSnapshot

from .providers.base import LLMProvider, ToolCall
from .tools import TOOLS

SYSTEM_PROMPT = (
    "You are operating a web application, one step at a time, to accomplish a goal. "
    "Each turn you are shown the current URL and every visible interactive or textual "
    "element, each carrying a node_id. Call exactly one tool per turn, always referring "
    "to elements by node_id -- never by a CSS selector or a guessed description. "
    "When the goal is fully accomplished, call finish with every requested value in "
    "`outputs`. If it cannot be accomplished (e.g. a required control is refused, or "
    "nothing on the page matches what's needed), call finish with success=false and "
    "explain why in `reason`. Do not repeat an action that was just refused or that "
    "produced an error without trying something different."
)


@dataclass
class StepLog:
    step_index: int
    tool_call: ToolCall
    node: InteractiveNode | None
    snapshot: SurfaceSnapshot
    location_path: str
    result: str  # "ok" | "refused" | "error"
    detail: str = ""
    # Which model actually answered this step -- None for the entry
    # navigate (step_index=-1), which is the CLI's own action and never
    # calls decide(). Recorded per step, not once for the whole run, so
    # evidence stays honest if the primary fails partway through and later
    # steps fall back to the secondary provider.
    provider_model: str | None = None


@dataclass
class DiscoveryIntervention:
    """A human took the wheel during discovery to clear something the LLM
    couldn't get past -- evidence, never an artifact input. The before/
    after pair and the operator's own note are exactly what
    escalation/human_action.py already records for a REPLAY handoff;
    `resolution` is the one field that has no equivalent there, because
    discovery has no compiled checkpoint a resolution could instead be
    derived from (see ControlBroker.release()'s docstring) -- it is a
    structured control command the operator issued, recorded as exactly
    that, never promoted to a "fact" the way
    human_action.human_performed_pending_action is."""

    trigger: str  # "no_progress" -- the only discovery-side escalation trigger today
    before_url: str
    before_screenshot_ref: str | None
    resolution: str  # "cleared_obstacle" | "workflow_advanced"
    after_url: str
    after_screenshot_ref: str | None
    operator_note: str | None
    same_session: bool = True
    raised_at: float = 0.0
    resolved_at: float = 0.0


@dataclass
class DiscoveryTranscript:
    goal: str
    base_url: str
    model: str
    steps: list[StepLog] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    success: bool = False
    reason: str = ""
    interventions: list[DiscoveryIntervention] = field(default_factory=list)


def _render_observation(snapshot: SurfaceSnapshot, location_str: str) -> str:
    """Trimmed, text-only view of the page: node_id + role + name + text for
    every interactive or textual node. No screenshots, no raw bbox/attrs --
    the model gets exactly enough to pick a node_id, nothing more (this is
    also where redaction of anything sensitive would happen before it ever
    left the process, once the safety module grows that -- Phase H)."""
    lines = [f"Current location: {location_str}", "", "Visible elements:"]
    found = False
    for n in snapshot:
        if not (n.interactive or n.text):
            continue
        found = True
        bits = [f"[{n.node_id}]"]
        if n.role:
            bits.append(f"role={n.role}")
        if n.name:
            bits.append(f"name={n.name!r}")
        if n.text and n.text != n.name:
            bits.append(f"text={n.text!r}")
        lines.append("  " + " ".join(bits))
    if not found:
        lines.append("  (nothing interactive or textual is visible)")
    return "\n".join(lines)


def _handle_discovery_stall(
    *,
    adapter: SurfaceAdapter,
    broker: ControlBroker,
    policy: Policy,
    run_id: str,
    evidence_dir: str,
    requested_capability_id: str | None,
    goal: str,
    reason: str,
    completed_step_ids: list[str],
    escalation_poll_s: float,
    escalation_max_wait_s: float,
) -> DiscoveryIntervention | None:
    """Raises a discovery-phase intervention and blocks -- polling the same
    broker a separate `cua ops claim/release --resolution ...` writes to --
    until the operator releases with a structured resolution or the wait
    times out. Returns None on timeout (caller treats that as a hard
    failure, same as an unhandled BoundExceeded); otherwise the completed
    DiscoveryIntervention record, ready to append to the transcript.

    Mirrors replay/engine.py's _escalate_and_wait shape (raise, start a
    KeepAliveThread, poll, stop it unconditionally) deliberately -- same
    problem, same answer -- but there is no find_resume_point here: a
    discovery run has no compiled checkpoint yet to derive a resume point
    from, so what happens next is exactly what the operator declared via
    `resolution`, nothing inferred.
    """
    before_location = adapter.location()
    before_url = f"{before_location.origin}{before_location.path}"
    before_screenshot_ref = f"{evidence_dir}/{run_id}-stall-before.png"
    adapter.screenshot(before_screenshot_ref)

    raise_intervention(
        broker=broker,
        run_id=run_id,
        capability_id=None,
        goal=goal,
        step_id="discovery",
        reason=reason,
        expected="the LLM makes forward progress toward the goal",
        observed=f"no progress for several consecutive steps at {before_url}",
        evidence_dir=evidence_dir,
        policy=policy,
        screenshot_ref=before_screenshot_ref,
        completed_steps=completed_step_ids,
        phase="discovery",
        requested_capability_id=requested_capability_id,
    )
    raised_at = time.time()

    keepalive = KeepAliveThread(
        base_url=before_location.origin, policy=policy, db_path=broker.db_path, run_id=run_id
    )
    keepalive.start()
    try:
        row = None
        waited = 0.0
        while waited < escalation_max_wait_s:
            candidate = broker.get_state(run_id)
            if candidate is not None and candidate.state == "RESUMING":
                row = candidate
                broker.mark_resumed(run_id)
                break
            time.sleep(escalation_poll_s)
            waited += escalation_poll_s
    finally:
        keepalive.stop()

    if row is None:
        return None

    assert row.resolution is not None  # broker.release() enforces this for phase="discovery"
    after_location = adapter.location()
    after_url = f"{after_location.origin}{after_location.path}"
    after_screenshot_ref = f"{evidence_dir}/{run_id}-stall-after.png"
    adapter.screenshot(after_screenshot_ref)

    return DiscoveryIntervention(
        trigger="no_progress",
        before_url=before_url,
        before_screenshot_ref=before_screenshot_ref,
        resolution=row.resolution,
        after_url=after_url,
        after_screenshot_ref=after_screenshot_ref,
        operator_note=row.release_note,
        raised_at=raised_at,
        resolved_at=time.time(),
    )


def run_discovery(
    goal: str,
    base_url: str,
    adapter: SurfaceAdapter,
    provider: LLMProvider,
    policy: Policy,
    *,
    model_name: str = "unknown",
    max_steps: int = 20,
    broker: ControlBroker | None = None,
    run_id: str | None = None,
    evidence_dir: str = "",
    requested_capability_id: str | None = None,
    escalation_poll_s: float = 1.0,
    escalation_max_wait_s: float = 120.0,
) -> DiscoveryTranscript:
    """`broker` is optional so every existing caller (and every test) that
    doesn't pass one keeps the original behaviour: a no-progress stall
    raises BoundExceeded and the run ends. When a broker IS given (and
    `run_id` alongside it -- both are required together), a stall instead
    raises a discovery-phase intervention and blocks for an operator, same
    shape as replay's own handoff. `max_steps` exceeded never hands off,
    even with a broker: handing off there would buy no further steps,
    since a handoff resets only the no-progress counter, never max_steps or
    the wall-clock ceiling (ExecutionGuard has no reset path for either)."""
    transcript = DiscoveryTranscript(goal=goal, base_url=base_url, model=model_name)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Goal: {goal}"},
    ]

    # The entry point is the operator's own CLI argument, not a model
    # decision, but it is still a system-boundary input -- a typo or a
    # malicious --target is exactly what the allowlist exists to catch, and
    # "it came from the CLI, not the model" is not a reason to skip the same
    # gate every subsequent navigation goes through. It still has to be
    # LOGGED as a real step: replay starts from a blank page, so if this
    # navigation isn't in the compiled artifact, step one has nothing to
    # resolve against.
    entry_allow = check_allowed("navigate", policy, destination=base_url)
    if not entry_allow.allowed:
        raise ValueError(f"--target {base_url!r} is refused: {entry_allow.reason}")
    adapter.act(Navigate(url_template=""), value=base_url)
    transcript.steps.append(
        StepLog(
            step_index=-1,
            tool_call=ToolCall(name="navigate", args={"url": base_url}),
            node=None,
            snapshot=adapter.observe(),
            location_path=adapter.location().path,
            result="ok",
            detail="entry point",
        )
    )

    guard = ExecutionGuard(policy)

    step_index = 0
    while step_index < max_steps:
        guard.check_step()  # wall-clock ceiling; step count is also bounded by max_steps above

        snapshot = adapter.observe()
        location = adapter.location()
        location_str = f"{location.origin}{location.path}"
        observation = _render_observation(snapshot, location_str)
        try:
            guard.check_progress(observation)  # raises BoundExceeded on N stale steps in a row
        except BoundExceeded as exc:
            if broker is None or run_id is None or not guard.use_discovery_handoff():
                raise
            intervention = _handle_discovery_stall(
                adapter=adapter,
                broker=broker,
                policy=policy,
                run_id=run_id,
                evidence_dir=evidence_dir,
                requested_capability_id=requested_capability_id,
                goal=goal,
                reason=exc.reason,
                completed_step_ids=[str(s.step_index) for s in transcript.steps],
                escalation_poll_s=escalation_poll_s,
                escalation_max_wait_s=escalation_max_wait_s,
            )
            if intervention is None:
                raise  # gave up waiting for an operator -- same failure as an unhandled stall
            transcript.interventions.append(intervention)
            if intervention.resolution == "workflow_advanced":
                # The operator did some or all of the actual task by hand.
                # None of that is in transcript.steps -- StepLog only ever
                # records an LLM tool call -- so whatever the LLM recorded
                # before this point is an artifact with the steps that
                # mattered missing from it. Aborting with success=False
                # here reuses compile_capability()'s own existing "cannot
                # compile a failed discovery run" refusal (agent/compile.py)
                # rather than inventing a second way to say "no artifact":
                # ABORTED_NO_ARTIFACT IS transcript.success=False, not a
                # new state.
                transcript.success = False
                transcript.reason = (
                    "aborted: operator resolved a discovery handoff with "
                    "resolution=workflow_advanced -- the operator's own actions "
                    "advanced the goal outside the LLM's recorded steps, so no "
                    "artifact can be compiled from this run"
                )
                return transcript
            # resolution == "cleared_obstacle": hand control back to the LLM
            # with a FRESH observation -- reset_progress() seeds
            # _last_observation with it directly, and the next
            # check_progress() call (next loop iteration) compares against
            # this, not against the stale one that just triggered the stall.
            fresh_snapshot = adapter.observe()
            fresh_location = adapter.location()
            fresh_observation = _render_observation(
                fresh_snapshot, f"{fresh_location.origin}{fresh_location.path}"
            )
            guard.reset_progress(fresh_observation)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Human control was returned. Re-inspect the current screen and "
                        "continue only from what is visible now."
                    ),
                }
            )
            continue  # does not consume a step_index -- no LLM decision happened this pass

        messages.append({"role": "user", "content": observation})
        call = provider.decide(messages, TOOLS)
        # Which model actually answered -- FallbackProvider updates this
        # every call, so evidence stays honest about a mid-run fallback
        # rather than crediting the configured primary for every step.
        used_model = getattr(provider, "last_used_model", model_name)
        messages.append({"role": "assistant", "content": f"Called {call.name}({call.args})"})

        if call.name == "finish":
            transcript.success = bool(call.args.get("success", False))
            transcript.reason = str(call.args.get("reason", ""))
            outputs = call.args.get("outputs") or {}
            transcript.outputs.update({k: str(v) for k, v in outputs.items()})
            transcript.steps.append(
                StepLog(step_index, call, None, snapshot, location.path, "ok", "finished", used_model)
            )
            return transcript

        node = None
        if "node_id" in call.args:
            node = next((n for n in snapshot if n.node_id == call.args["node_id"]), None)
        resolution = ResolutionResult(resolved=node is not None, layer=None, node=node, attempts=[])

        destination = call.args.get("url") if call.name == "navigate" else None
        allow = check_allowed(call.name, policy, current_location=location, destination=destination)
        if not allow.allowed:
            transcript.steps.append(
                StepLog(
                    step_index, call, node, snapshot, location.path, "refused", allow.reason, used_model
                )
            )
            messages.append(
                {"role": "user", "content": f"Refused: {allow.reason}. Choose a different action."}
            )
            step_index += 1
            continue

        risk = gate(call.name, policy, resolution=resolution, destination=destination)
        if not risk.allowed_unattended:
            transcript.steps.append(
                StepLog(
                    step_index, call, node, snapshot, location.path, "refused", risk.reason, used_model
                )
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Refused: {risk.reason}. This action cannot be taken during "
                        f"discovery. Choose a different action, or finish with success=false."
                    ),
                }
            )
            step_index += 1
            continue

        if call.name in ("click", "type", "read") and node is None:
            transcript.steps.append(
                StepLog(
                    step_index,
                    call,
                    None,
                    snapshot,
                    location.path,
                    "error",
                    f"no visible element with node_id={call.args.get('node_id')!r}",
                    used_model,
                )
            )
            messages.append(
                {
                    "role": "user",
                    "content": "That node_id is not currently visible. Pick one from the list.",
                }
            )
            step_index += 1
            continue

        try:
            if call.name == "navigate":
                adapter.act(Navigate(url_template=""), value=call.args["url"])
            elif call.name == "click":
                adapter.act(Click(), resolution=resolution)
            elif call.name == "type":
                adapter.act(
                    TypeText(
                        value_from=f"{{{{input.{call.args['param_name']}}}}}",
                        submit=bool(call.args.get("submit", False)),
                    ),
                    resolution=resolution,
                    value=call.args["text"],
                )
            elif call.name == "read":
                value = adapter.act(Read(into=call.args["output_name"]), resolution=resolution)
                transcript.outputs[call.args["output_name"]] = value or ""
            else:
                raise ValueError(f"model called an unknown tool: {call.name!r}")
            transcript.steps.append(
                StepLog(step_index, call, node, snapshot, location.path, "ok", "", used_model)
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced to the model, not swallowed
            transcript.steps.append(
                StepLog(step_index, call, node, snapshot, location.path, "error", str(exc), used_model)
            )
            messages.append({"role": "user", "content": f"Action failed: {exc}. Try something else."})
        step_index += 1

    raise BoundExceeded(f"max_steps ({max_steps}) exceeded without calling finish")
