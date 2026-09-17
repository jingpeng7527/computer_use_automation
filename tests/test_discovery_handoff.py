"""Discovery-side human handoff -- no LLM, no browser: a scripted
LLMProvider/SurfaceAdapter (the same pattern
test_discovery_compile_replay_with_fake_provider.py uses) plus a REAL
ControlBroker against a temp SQLite file, simulating the operator's
`cua ops claim` / `cua ops release --resolution ...` as a side effect of
the broker poll loop -- same technique
test_escalation_wiring.py/test_session_lost_is_reported_when_the_resumed_page_is_out_of_scope
already uses for the replay-side handoff.

What this proves, concretely:

- a genuine no-progress stall (the SAME observation on `no_progress_max_
  consecutive_steps` consecutive checks) raises a real discovery-phase
  intervention through the real broker, not a mock of one
- resolution="cleared_obstacle" hands control back to the LLM with a FRESH
  observation, and the run completes normally -- the resulting transcript
  still has ONLY LLM-driven steps in `.steps`; the human's own action
  (flipping the target's state) never becomes a StepLog
- resolution="workflow_advanced" aborts the run with success=False and a
  reason that says why, exactly like `compile_capability()`'s existing
  "cannot compile a failed discovery run" refusal already handles --
  proving no new artifact-shaped output is possible for this case without
  a second special case anywhere
- with no broker at all (the default), a stall still raises BoundExceeded
  exactly as it always did -- the existing, unmodified behaviour
"""

from __future__ import annotations

from cua.agent import run_discovery
from cua.agent.providers.base import ToolCall
from cua.escalation import ControlBroker
from cua.safety import BoundExceeded, load_policy
from cua.surface import BBox, InteractiveNode, Location, ResolutionResult

BASE_URL = "http://fake.example/search"
POLICY = load_policy().model_copy(
    update={
        "allowlist": load_policy().allowlist.model_copy(update={"origins": ["http://fake.example"]}),
        "execution_bounds": load_policy().execution_bounds.model_copy(
            update={"no_progress_max_consecutive_steps": 3, "max_discovery_handoffs": 1}
        ),
    }
)


def _node(node_id: str, role: str | None = None, name: str | None = None, text: str = ""):
    return InteractiveNode(node_id=node_id, role=role, name=name, text=text, bbox=BBox(x=0, y=0, w=1, h=1))


class _ScriptedProvider:
    model = "fake-scripted-model"

    def __init__(self, calls: list[ToolCall]) -> None:
        self._calls = list(calls)

    def decide(self, messages, tools) -> ToolCall:
        assert self._calls, "provider script exhausted -- loop asked for one decision too many"
        return self._calls.pop(0)


class _StallThenClearAdapter:
    """Shows the SAME stuck page (a button that does nothing) until
    `.cleared` is flipped externally -- standing in for the operator's own
    browser action during the handoff, which happens outside any tool the
    LLM calls and so never appears as a StepLog. Once cleared, a different
    page with a readable value is visible."""

    def __init__(self) -> None:
        self.cleared = False
        self.click_count = 0

    def observe(self):
        if not self.cleared:
            return [_node("n_btn", role="button", name="Retry")]
        return [
            _node("n_heading", role="heading", name="Recovered"),
            _node("n_out", role="textbox", name="Value", text="42"),
        ]

    def resolve(self, target):
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):
        if action.type == "click":
            self.click_count += 1
        return None

    def wait_for(self, condition, timeout_ms):
        return True

    def location(self):
        return Location(origin="http://fake.example", path="/recovered" if self.cleared else "/stuck")

    def screenshot(self, path):
        pass  # a real screenshot needs a real page; these tests only check the evidence structure


def _broker_that_auto_resolves(tmp_path, adapter: _StallThenClearAdapter, resolution: str, note: str) -> ControlBroker:
    """A real ControlBroker whose get_state() performs the operator's claim
    + release (and, for cleared_obstacle, the operator's own browser fix)
    the first time it observes PAUSED -- everything downstream of that is
    the real broker, the real phase/resolution validation, the real
    intervention.json write."""
    broker = ControlBroker(tmp_path / "control.db")
    original_get_state = broker.get_state
    simulated = {"done": False}

    def _get_state_and_resolve(run_id: str):
        row = original_get_state(run_id)
        if not simulated["done"] and row is not None and row.state == "PAUSED":
            if resolution == "cleared_obstacle":
                adapter.cleared = True
            broker.claim(run_id, holder="operator")
            broker.release(run_id, holder="operator", note=note, resolution=resolution)
            simulated["done"] = True
            row = original_get_state(run_id)
        return row

    broker.get_state = _get_state_and_resolve  # type: ignore[method-assign]
    return broker


def test_discovery_handoff_cleared_obstacle_resumes_and_completes(tmp_path) -> None:
    adapter = _StallThenClearAdapter()
    provider = _ScriptedProvider(
        [
            ToolCall(name="click", args={"node_id": "n_btn"}),
            ToolCall(name="click", args={"node_id": "n_btn"}),
            ToolCall(name="click", args={"node_id": "n_btn"}),
            # consumed only AFTER the handoff resumes, against the recovered page:
            ToolCall(name="read", args={"node_id": "n_out", "output_name": "value"}),
            ToolCall(name="finish", args={"success": True, "reason": "done", "outputs": {"value": "42"}}),
        ]
    )
    broker = _broker_that_auto_resolves(tmp_path, adapter, "cleared_obstacle", "dismissed the retry banner")

    transcript = run_discovery(
        goal="read the recovered value",
        base_url=BASE_URL,
        adapter=adapter,
        provider=provider,
        policy=POLICY,
        model_name="fake-scripted-model",
        max_steps=10,
        broker=broker,
        run_id="disco-handoff-test",
        evidence_dir=str(tmp_path / "evidence"),
        requested_capability_id="acme_core.test_capability",
    )

    assert transcript.success
    assert transcript.outputs["value"] == "42"
    # Only LLM-driven tool calls ever become a StepLog -- the operator's own
    # click (adapter.cleared = True) is nowhere in this list.
    assert [s.tool_call.name for s in transcript.steps] == [
        "navigate",
        "click",
        "click",
        "click",
        "read",
        "finish",
    ]

    assert len(transcript.interventions) == 1
    intervention = transcript.interventions[0]
    assert intervention.trigger == "no_progress"
    assert intervention.resolution == "cleared_obstacle"
    assert intervention.operator_note == "dismissed the retry banner"
    assert intervention.before_url.endswith("/stuck")
    assert intervention.after_url.endswith("/recovered")

    from cua.agent import compile_capability
    from cua.schema import AppProfile

    capability = compile_capability(
        transcript,
        capability_id="acme_core.test_capability",
        app_profile=AppProfile(product="fake", version="0"),
        discovery_run_id="disco-handoff-test",
        transcript_sha256="0" * 64,
    )
    assert capability.provenance.discovery_handoffs == 1
    assert capability.status == "draft"  # never approved just for having handed off once


def test_discovery_handoff_workflow_advanced_aborts_with_no_artifact(tmp_path) -> None:
    adapter = _StallThenClearAdapter()
    provider = _ScriptedProvider(
        [
            ToolCall(name="click", args={"node_id": "n_btn"}),
            ToolCall(name="click", args={"node_id": "n_btn"}),
            ToolCall(name="click", args={"node_id": "n_btn"}),
            # never reached -- the run aborts before another decide() call
            ToolCall(name="finish", args={"success": True, "reason": "should not get here"}),
        ]
    )
    broker = _broker_that_auto_resolves(
        tmp_path, adapter, "workflow_advanced", "I just finished the lookup myself, easier that way"
    )

    transcript = run_discovery(
        goal="read the recovered value",
        base_url=BASE_URL,
        adapter=adapter,
        provider=provider,
        policy=POLICY,
        model_name="fake-scripted-model",
        max_steps=10,
        broker=broker,
        run_id="disco-handoff-abort-test",
        evidence_dir=str(tmp_path / "evidence"),
        requested_capability_id="acme_core.test_capability",
    )

    assert transcript.success is False
    assert "workflow_advanced" in transcript.reason
    assert len(transcript.interventions) == 1
    assert transcript.interventions[0].resolution == "workflow_advanced"

    # The existing, unmodified refusal in compile_capability() is what
    # actually prevents an artifact -- no new exception type was needed.
    from cua.agent import compile_capability
    from cua.schema import AppProfile
    import pytest

    with pytest.raises(ValueError, match="cannot compile a failed discovery run"):
        compile_capability(
            transcript,
            capability_id="acme_core.test_capability",
            app_profile=AppProfile(product="fake", version="0"),
            discovery_run_id="disco-handoff-abort-test",
            transcript_sha256="0" * 64,
        )


def test_discovery_without_a_broker_stalls_exactly_as_before(tmp_path) -> None:
    """Regression: every existing caller of run_discovery() that doesn't
    pass a broker (the CLI's default before this feature existed, and
    every discovery test predating this file) must keep raising
    BoundExceeded on a stall, unchanged."""
    adapter = _StallThenClearAdapter()
    provider = _ScriptedProvider(
        [
            ToolCall(name="click", args={"node_id": "n_btn"}),
            ToolCall(name="click", args={"node_id": "n_btn"}),
            ToolCall(name="click", args={"node_id": "n_btn"}),
        ]
    )

    try:
        run_discovery(
            goal="read the recovered value",
            base_url=BASE_URL,
            adapter=adapter,
            provider=provider,
            policy=POLICY,
            model_name="fake-scripted-model",
            max_steps=10,
            # no broker, no run_id -- the pre-existing call shape
        )
    except BoundExceeded as exc:
        assert "no progress" in str(exc)
    else:
        raise AssertionError("expected a stall with no broker to raise BoundExceeded")
