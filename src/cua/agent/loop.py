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

from dataclasses import dataclass, field

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
class DiscoveryTranscript:
    goal: str
    base_url: str
    model: str
    steps: list[StepLog] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    success: bool = False
    reason: str = ""


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


def run_discovery(
    goal: str,
    base_url: str,
    adapter: SurfaceAdapter,
    provider: LLMProvider,
    policy: Policy,
    *,
    model_name: str = "unknown",
    max_steps: int = 20,
) -> DiscoveryTranscript:
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

    for step_index in range(max_steps):
        guard.check_step()  # wall-clock ceiling; step count is also bounded by max_steps above

        snapshot = adapter.observe()
        location = adapter.location()
        location_str = f"{location.origin}{location.path}"
        observation = _render_observation(snapshot, location_str)
        guard.check_progress(observation)  # raises BoundExceeded on N stale steps in a row

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

    raise BoundExceeded(f"max_steps ({max_steps}) exceeded without calling finish")
