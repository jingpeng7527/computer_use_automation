"""discover -> compile -> replay, end to end, with zero LLM and zero
browser: a scripted `LLMProvider` (the same `Protocol` GeminiProvider and
GroqProvider implement) plus a scripted `SurfaceAdapter`.

This is the pattern worth borrowing from a competing implementation's
LLMClient protocol injection -- not "support more model vendors", but
"discovery's control flow (loop.py) and the compiler (compile.py) are
testable without an API key or a browser at all". The question this
answers: given an observation and a scripted model action, does the
compiler produce an artifact that is (a) valid, (b) carries no leftover
copy of the concrete input value discovery was recorded with, and (c)
actually replays -- deterministically, against a fresh instance of the
same fake surface? `tests/test_no_recording_time_literals_in_artifacts.py`
checks (b) against the real committed artifacts; this test checks all
three together, hermetically, on every run.
"""

from __future__ import annotations

import json

from cua.agent import compile_capability, run_discovery
from cua.agent.providers.base import ToolCall
from cua.replay import replay
from cua.safety import load_policy
from cua.surface import BBox, InteractiveNode, Location, ResolutionResult

BASE_URL = "http://fake.example/search"
POLICY = load_policy().model_copy(
    update={"allowlist": load_policy().allowlist.model_copy(update={"origins": ["http://fake.example"]})}
)


def _node(node_id: str, role: str | None = None, name: str | None = None, text: str = ""):
    return InteractiveNode(node_id=node_id, role=role, name=name, text=text, bbox=BBox(x=0, y=0, w=1, h=1))


class _ScriptedProvider:
    """A minimal fake of the same `LLMProvider` protocol GeminiProvider and
    GroqProvider implement -- decide() just pops the next scripted call,
    ignoring the observation text entirely (this script never needs to
    branch on it)."""

    model = "fake-scripted-model"

    def __init__(self, calls: list[ToolCall]) -> None:
        self._calls = list(calls)

    def decide(self, messages, tools) -> ToolCall:
        assert self._calls, "provider script exhausted -- loop asked for one decision too many"
        return self._calls.pop(0)


class _TwoPageAdapter:
    """search (textbox "Member ID", button "Search") -> results (heading
    "Search Results", textbox "Balance") -- the two pages the scripted
    provider below drives through, entirely in memory."""

    def __init__(self) -> None:
        self._on_results = False

    def observe(self):
        if not self._on_results:
            return [
                _node("n_input", role="textbox", name="Member ID"),
                _node("n_search", role="button", name="Search"),
            ]
        return [
            _node("n_heading", role="heading", name="Search Results"),
            _node("n_balance", role="textbox", name="Balance", text="$999.00 USD"),
        ]

    def resolve(self, target):
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):
        if action.type == "navigate":
            return None
        if action.type == "type" and action.submit:
            self._on_results = True
            return None
        if action.type == "read":
            return "$999.00 USD"
        return None

    def wait_for(self, condition, timeout_ms):
        return True

    def location(self):
        return Location(origin="http://fake.example", path="/results" if self._on_results else "/search")

    def screenshot(self, path):
        pass


def _run_discovery_script() -> tuple:
    adapter = _TwoPageAdapter()
    provider = _ScriptedProvider(
        [
            ToolCall(
                name="type",
                args={"node_id": "n_input", "text": "55555", "param_name": "member_id", "submit": True},
            ),
            ToolCall(name="read", args={"node_id": "n_balance", "output_name": "balance"}),
            ToolCall(
                name="finish",
                args={"success": True, "reason": "done", "outputs": {"balance": "$999.00 USD"}},
            ),
        ]
    )
    transcript = run_discovery(
        goal="look up member 55555 and read their current savings balance",
        base_url=BASE_URL,
        adapter=adapter,
        provider=provider,
        policy=POLICY,
        model_name="fake-scripted-model",
        max_steps=5,
    )
    return transcript, adapter


def test_scripted_discovery_completes_without_any_real_provider_or_browser() -> None:
    transcript, _ = _run_discovery_script()
    assert transcript.success
    assert transcript.outputs["balance"] == "$999.00 USD"
    # entry navigate + type + read + finish
    assert [s.tool_call.name for s in transcript.steps] == ["navigate", "type", "read", "finish"]


def test_compiled_artifact_carries_no_literal_copy_of_the_recording_time_input() -> None:
    from cua.schema import AppProfile

    transcript, _ = _run_discovery_script()
    capability = compile_capability(
        transcript,
        capability_id="test.fake_provider_lookup",
        app_profile=AppProfile(product="fake-scripted-target", version="0"),
        discovery_run_id="fake-run",
        transcript_sha256="0" * 64,
    )
    dumped = json.dumps(capability.model_dump(mode="json"))
    assert "55555" not in dumped
    assert "{{input.member_id}}" in dumped
    assert capability.inputs and capability.inputs[0].name == "member_id"


def test_compiled_artifact_actually_replays_against_a_fresh_instance_of_the_same_fake_surface() -> None:
    from cua.schema import AppProfile

    transcript, _ = _run_discovery_script()
    capability = compile_capability(
        transcript,
        capability_id="test.fake_provider_lookup",
        app_profile=AppProfile(product="fake-scripted-target", version="0"),
        discovery_run_id="fake-run",
        transcript_sha256="0" * 64,
    ).model_copy(update={"status": "approved"})

    fresh_adapter = _TwoPageAdapter()  # a NEW instance -- no state shared with discovery's own
    result = replay(capability, {"member_id": "12321"}, fresh_adapter, POLICY, run_id="fake-provider-replay")

    from cua.schema import Money

    assert result.status == "success"
    # OutputSpec's "money" auto-detection (compile.py's _classify_output_type)
    # kicks in on the "$999.00 USD" shape -- replay returns a typed Money,
    # same as the real discovery/replay pair does, not the raw string.
    assert result.outputs == {"balance": Money(amount_minor=99900, currency="USD")}


# ---- select / wait: added to discovery's own tool list after the other
# four (agent/tools.py) -- replay and the schema always supported Select/
# Wait actions, but discovery could never DISCOVER a flow needing a
# dropdown choice or an explicit wait, only have one hand-authored into an
# overlay afterward. This is the same discover -> compile -> replay proof
# as above, for the two tools that used to be missing. ----


class _BranchSelectThenSlowBalanceAdapter:
    """A branch dropdown has to be chosen before "Continue" reveals
    anything -- exercises `select`. Continuing moves to a page that is
    genuinely slow: the balance heading only appears once something
    actually calls wait_for() with the right condition, exercising `wait`
    (not just a page that happens to already show the right thing)."""

    def __init__(self) -> None:
        self.continued = False
        self.ready = False
        self.select_calls = 0

    def observe(self):
        if not self.continued:
            return [
                _node("n_branch", role="combobox", name="Branch"),
                _node("n_continue", role="button", name="Continue"),
            ]
        if not self.ready:
            return [_node("n_loading", text="Loading, please wait...")]
        return [
            _node("n_heading", role="heading", name="Balance Ready"),
            _node("n_balance", role="textbox", name="Balance", text="$500.00 USD"),
        ]

    def resolve(self, target):
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):
        if action.type == "select":
            self.select_calls += 1
            return None
        if action.type == "click":
            self.continued = True
            return None
        if action.type == "read":
            return "$500.00 USD"
        return None

    def wait_for(self, condition, timeout_ms):
        # Stands in for a real page that finishes loading partway through a
        # real poll loop -- the point being tested is that something in the
        # discovery loop actually CALLS wait_for() (with the LLM's declared
        # condition) rather than the page happening to already be ready.
        self.ready = True
        return True

    def location(self):
        return Location(origin="http://fake.example", path="/balance" if self.continued else "/branch")

    def screenshot(self, path):
        pass


def test_scripted_discovery_uses_select_and_wait_and_compiles_replayably() -> None:
    adapter = _BranchSelectThenSlowBalanceAdapter()
    provider = _ScriptedProvider(
        [
            ToolCall(
                name="select",
                args={"node_id": "n_branch", "value": "Downtown", "param_name": "branch"},
            ),
            ToolCall(name="click", args={"node_id": "n_continue"}),
            ToolCall(name="wait", args={"until_text": "Balance Ready", "timeout_ms": 5000}),
            ToolCall(name="read", args={"node_id": "n_balance", "output_name": "balance"}),
            ToolCall(
                name="finish",
                args={"success": True, "reason": "done", "outputs": {"balance": "$500.00 USD"}},
            ),
        ]
    )

    transcript = run_discovery(
        goal="pick the Downtown branch and read the balance",
        base_url=BASE_URL,
        adapter=adapter,
        provider=provider,
        policy=POLICY,
        model_name="fake-scripted-model",
        max_steps=10,
    )

    assert transcript.success
    assert transcript.outputs["balance"] == "$500.00 USD"
    assert adapter.select_calls == 1
    assert [s.tool_call.name for s in transcript.steps] == [
        "navigate",
        "select",
        "click",
        "wait",
        "read",
        "finish",
    ]

    from cua.schema import AppProfile, Select, Wait

    capability = compile_capability(
        transcript,
        capability_id="test.select_wait",
        app_profile=AppProfile(product="fake-scripted-target", version="0"),
        discovery_run_id="fake-run",
        transcript_sha256="0" * 64,
    )
    dumped = json.dumps(capability.model_dump(mode="json"))
    # Bound by reference, exactly like `type` -- never the literal chosen.
    assert "Downtown" not in dumped
    assert "{{input.branch}}" in dumped
    assert any(p.name == "branch" for p in capability.inputs)

    select_step = next(s for s in capability.steps if isinstance(s.action, Select))
    assert select_step.target is not None  # select is a control-acting action -- schema requires one

    wait_step = next(s for s in capability.steps if isinstance(s.action, Wait))
    assert wait_step.wait is not None
    assert wait_step.wait.until.text.value == "Balance Ready"
    assert wait_step.target is None  # Wait acts on no single control

    # Actually replays -- deterministically, against a FRESH instance of the
    # same fake surface, no LLM involved this time.
    from cua.schema import Money

    fresh_adapter = _BranchSelectThenSlowBalanceAdapter()
    approved = capability.model_copy(update={"status": "approved"})
    result = replay(approved, {"branch": "Uptown"}, fresh_adapter, POLICY, run_id="select-wait-replay")

    assert result.status == "success"
    assert result.outputs == {"balance": Money(amount_minor=50000, currency="USD")}
    assert fresh_adapter.select_calls == 1  # replay actually selected -- not skipped


def test_wait_timing_out_during_discovery_is_an_error_not_a_crash() -> None:
    """A wait_for() that returns False (genuinely never became true) must
    surface as an "error" StepLog the model can react to -- not raise, not
    silently swallow, and not misreport as "ok"."""

    class _NeverReadyAdapter(_BranchSelectThenSlowBalanceAdapter):
        def wait_for(self, condition, timeout_ms):
            return False  # the condition never holds, unlike the base class

    adapter = _NeverReadyAdapter()
    provider = _ScriptedProvider(
        [
            ToolCall(
                name="select",
                args={"node_id": "n_branch", "value": "Downtown", "param_name": "branch"},
            ),
            ToolCall(name="click", args={"node_id": "n_continue"}),
            ToolCall(name="wait", args={"until_text": "Balance Ready", "timeout_ms": 1}),
            ToolCall(
                name="finish",
                args={"success": False, "reason": "gave up after the wait timed out"},
            ),
        ]
    )

    transcript = run_discovery(
        goal="pick the Downtown branch and read the balance",
        base_url=BASE_URL,
        adapter=adapter,
        provider=provider,
        policy=POLICY,
        model_name="fake-scripted-model",
        max_steps=10,
    )

    wait_log = next(s for s in transcript.steps if s.tool_call.name == "wait")
    assert wait_log.result == "error"
    assert "timed out" in wait_log.detail
    assert transcript.success is False  # the script's own finish(success=False), reached cleanly
