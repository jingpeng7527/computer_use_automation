"""Three things REPORT.md sec 5 claims about the handoff that weren't
actually wired into replay/engine.py: the executor re-checking control
before every action (not just once, while waiting on an escalation), the
keep-alive thread ever being started, and session loss being a declared
outcome rather than a silent "give up and try the same escalation again".
No browser -- a real ControlBroker (SQLite, tmp_path) plus a scripted
adapter.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cua.escalation import ControlBroker
from cua.replay import engine as engine_module
from cua.replay import replay
from cua.safety import load_policy
from cua.schema import (
    AppProfile,
    Capability,
    Checkpoint,
    Click,
    OutputSpec,
    Provenance,
    Read,
    RoleName,
    RoleNameStrategy,
    Step,
    Target,
)
from cua.surface import BBox, InteractiveNode, Location, ResolutionResult

POLICY = load_policy()
HERE = Location(origin="http://127.0.0.1:8800", path="/members/search")
OFF_SCOPE = Location(origin="http://evil.example.com", path="/login")


def _node(text: str = "", role: str | None = None, name: str | None = None, node_id: str = "n0"):
    return InteractiveNode(node_id=node_id, role=role, name=name, text=text, bbox=BBox(x=0, y=0, w=1, h=1))


def _broker(tmp_path) -> ControlBroker:
    return ControlBroker(tmp_path / "control.db")


def _two_step_capability() -> Capability:
    return Capability(
        capability_id="test.escalation_wiring",
        status="approved",
        title="Escalation wiring test",
        summary="A minimal two-step capability existing only to test control re-checking.",
        app_profile=AppProfile(product="test", version="0"),
        outputs=[OutputSpec(name="x", type="string", description="test output", source_step_id="s1")],
        steps=[
            Step(
                id="s0",
                intent="click something harmless",
                action=Click(),
                risk_level="SAFE_READ",
                target=Target(strategies=[RoleNameStrategy(role="button", name="Search")]),
            ),
            Step(
                id="s1",
                intent="read something",
                action=Read(into="x"),
                risk_level="SAFE_READ",
                target=Target(strategies=[RoleNameStrategy(role="textbox", name="Balance")]),
            ),
        ],
        success_condition=Checkpoint(
            description="unreachable in this test", all_of=[RoleName(role="heading", name="Member Detail")]
        ),
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model="test",
            goal="test",
            discovery_run_id="test",
            transcript_sha256="0" * 64,
        ),
    )


class _AlwaysOkAdapter:
    """Never gets stuck on its own -- every target resolves, every action
    succeeds. The only way this run stops is the broker re-check."""

    def __init__(self, location: Location = HERE):
        self._location = location
        self.actions: list[str] = []

    def observe(self):
        return [
            _node(role="button", name="Search", node_id="n_btn"),
            _node(role="textbox", name="Balance", node_id="n_bal"),
        ]

    def resolve(self, target):
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):
        self.actions.append(action.type)
        return "$100.00 USD" if action.type == "read" else None

    def wait_for(self, condition, timeout_ms):
        return True

    def location(self):
        return self._location

    def screenshot(self, path):
        pass


def test_control_is_rechecked_before_every_action_not_just_once(tmp_path) -> None:
    """An operator claims control BEFORE the run ever gets stuck on its
    own -- simulating a claim racing an in-flight run, or a stale row left
    over from a reused run_id. The run must stop after its very next
    action-gate check, not plough through both steps because nothing on
    the page itself ever looked wrong."""
    broker = _broker(tmp_path)
    run_id = "recheck-test"
    broker.mark_stuck(run_id, "pre-existing claim for this test")
    assert broker.claim(run_id, holder="someone-else")

    adapter = _AlwaysOkAdapter()
    replay(_two_step_capability(), {}, adapter, POLICY, run_id=run_id, broker=None)

    # Without a broker passed to replay(), state.broker is None and the
    # re-check is a no-op -- confirms the baseline runs both steps
    # regardless of what this test's broker/run_id say (the capability's
    # own success_condition is intentionally unreachable here; only
    # whether both actions ran is the point of this half of the test).
    assert adapter.actions == ["click", "read"]

    # Now the real case: broker IS passed, and it already says someone
    # else holds control before the run starts. Nobody ever resolves the
    # resulting intervention, so this gives up quickly instead of blocking
    # on the default 120s wait -- the point here is WHEN it stops, not the
    # handoff resolution path (covered by the session_lost test below).
    adapter2 = _AlwaysOkAdapter()
    result2 = replay(
        _two_step_capability(),
        {},
        adapter2,
        POLICY,
        run_id=run_id,
        evidence_dir=str(tmp_path),  # raise_intervention writes here -- never the cwd
        broker=broker,
        escalation_poll_s=0.01,
        escalation_max_wait_s=0.05,
    )

    assert result2.status == "escalated"
    assert adapter2.actions == []  # stopped before the FIRST action, not after one


def test_session_lost_is_reported_when_the_resumed_page_is_out_of_scope(tmp_path, monkeypatch) -> None:
    """The operator "releases" control, but the page that comes back is
    outside the allowlisted app scope entirely (a login redirect, a
    different origin) -- neither resume candidate can hold, and this must
    be reported as session_lost, not silently retried as an ordinary
    give-up."""
    broker = _broker(tmp_path)
    run_id = "session-lost-test"

    class _StuckThenOffScopeAdapter(_AlwaysOkAdapter):
        def __init__(self):
            super().__init__(HERE)
            self._stuck_once = False

        def act(self, action, resolution=None, value=None):
            if action.type == "click" and not self._stuck_once:
                self._stuck_once = True
                raise RuntimeError("simulated app crash")
            return super().act(action, resolution=resolution, value=value)

        def location(self):
            # Once "stuck", report a location outside the allowlist --
            # standing in for a login redirect during the handoff wait.
            return OFF_SCOPE if self._stuck_once else HERE

    adapter = _StuckThenOffScopeAdapter()

    # No real thread, and no real wall-clock waiting: replay() also calls
    # get_state() once per step BEFORE this run ever gets stuck (the
    # broker re-check the other test covers), so the simulated
    # claim/release below is gated on the row actually being PAUSED --
    # not on a fixed call count, which that earlier check would throw off.
    monkeypatch.setattr(
        engine_module,
        "KeepAliveThread",
        type(
            "FakeKeepAlive",
            (),
            {"__init__": lambda self, **kw: None, "start": lambda self: None, "stop": lambda self: None},
        ),
    )
    original_get_state = broker.get_state
    simulated = {"done": False}

    def _get_state_then_resuming(rid):
        row = original_get_state(rid)
        if not simulated["done"] and row is not None and row.state == "PAUSED":
            broker.claim(rid, holder="operator")
            broker.release(rid, holder="operator")
            simulated["done"] = True
            row = original_get_state(rid)
        return row

    monkeypatch.setattr(broker, "get_state", _get_state_then_resuming)

    result = replay(
        _two_step_capability(),
        {},
        adapter,
        POLICY,
        run_id=run_id,
        evidence_dir=str(tmp_path),  # raise_intervention writes here -- never the cwd
        broker=broker,
        escalation_poll_s=0.01,
        escalation_max_wait_s=2.0,
    )

    assert result.status == "failed"
    assert result.failure.kind == "session_lost"
    assert "evil.example.com" in result.failure.observed
