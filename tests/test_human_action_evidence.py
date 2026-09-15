"""human_action.json: what the operator actually did during a handoff,
recorded as structured evidence alongside intervention.json --
`human_performed_pending_action` derived mechanically (via
find_resume_point re-checking the stuck step's own checkpoint), not from
the operator's self-report; `operator_note` kept alongside it, clearly
labeled as the separate, unverified account. No browser -- a real
ControlBroker (SQLite, tmp_path) plus a scripted adapter, same style as
test_escalation_wiring.py.
"""

from __future__ import annotations

import json
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
FIXED = Location(origin="http://127.0.0.1:8800", path="/members/results")


def _node(text: str = "", role: str | None = None, name: str | None = None, node_id: str = "n0"):
    return InteractiveNode(node_id=node_id, role=role, name=name, text=text, bbox=BBox(x=0, y=0, w=1, h=1))


def _capability_with_a_checkpointed_stuck_step() -> Capability:
    return Capability(
        capability_id="test.human_action_evidence",
        status="approved",
        title="Human action evidence test",
        summary="A minimal two-step capability whose first step has a checkpoint an operator's fix can satisfy.",
        app_profile=AppProfile(product="test", version="0"),
        outputs=[OutputSpec(name="x", type="string", description="test output", source_step_id="s1")],
        steps=[
            Step(
                id="s0",
                intent="click something that breaks the first time",
                action=Click(),
                risk_level="SAFE_READ",
                target=Target(strategies=[RoleNameStrategy(role="button", name="Search")]),
                checkpoint=Checkpoint(
                    description="reached the results page",
                    all_of=[RoleName(role="heading", name="Search Results")],
                ),
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
            description="unreachable in this test -- this fixture only cares about s0's own checkpoint",
            all_of=[RoleName(role="heading", name="Member Detail")],
        ),
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model="test",
            goal="test",
            discovery_run_id="test",
            transcript_sha256="0" * 64,
        ),
    )


class _BreaksOnceThenFixedAdapter:
    """s0's action raises once (simulating a real crash); after the
    operator's fix, the page it observes matches s0's own checkpoint --
    the "resume from the next step" candidate, not "the whole thing is
    done"."""

    def __init__(self) -> None:
        self._broken = True
        self.actions: list[str] = []

    def observe(self):
        if self._broken:
            return [_node(role="button", name="Search", node_id="n_btn")]
        return [
            _node(role="heading", name="Search Results", node_id="n_head"),
            _node(role="textbox", name="Balance", node_id="n_bal"),
        ]

    def resolve(self, target):
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):
        self.actions.append(action.type)
        if action.type == "click" and self._broken:
            raise RuntimeError("simulated app crash")
        return "$100.00 USD" if action.type == "read" else None

    def wait_for(self, condition, timeout_ms):
        return True

    def location(self):
        return FIXED if not self._broken else HERE

    def screenshot(self, path):
        pass


def test_human_action_json_records_the_fix_and_the_operators_note(tmp_path, monkeypatch) -> None:
    broker = ControlBroker(tmp_path / "control.db")
    run_id = "human-action-test"
    adapter = _BreaksOnceThenFixedAdapter()

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
            adapter._broken = False  # the operator's fix, applied directly to the live session
            broker.claim(rid, holder="operator")
            broker.release(rid, holder="operator", note="reloaded without the bad query param")
            simulated["done"] = True
            row = original_get_state(rid)
        return row

    monkeypatch.setattr(broker, "get_state", _get_state_then_resuming)

    result = replay(
        _capability_with_a_checkpointed_stuck_step(),
        {},
        adapter,
        POLICY,
        run_id=run_id,
        evidence_dir=str(tmp_path),
        broker=broker,
        escalation_poll_s=0.01,
        escalation_max_wait_s=2.0,
    )

    # "step" resume means: the operator already completed s0 by hand, so
    # replay resumes at s1 -- it does NOT re-run s0's click. (The overall
    # run still ends "failed" on the fixture's own deliberately-unreachable
    # success_condition, same as test_escalation_wiring.py's fixtures --
    # not the point of this test, which is the human_action.json record.)
    assert result.status == "failed"
    assert adapter.actions == ["click", "read"]

    record = json.loads((tmp_path / "human_action.json").read_text())
    assert record["claimed_by"] == "operator"
    assert record["operator_note"] == "reloaded without the bad query param"
    assert record["resume_decision"] == "step"
    assert record["human_performed_pending_action"] is True
    assert record["before"]["url"] == "http://127.0.0.1:8800/members/search"
    assert record["after"]["url"] == "http://127.0.0.1:8800/members/results"
    assert record["before"]["screenshot_ref"] == f"{tmp_path}/s0-failure.png"
    assert record["after"]["screenshot_ref"] == f"{tmp_path}/s0-after-handoff.png"


def test_human_performed_pending_action_is_false_when_the_fix_did_not_hold(tmp_path, monkeypatch) -> None:
    """An operator releases control, but nothing on the page actually
    changed -- neither resume candidate holds. The derived fact must say
    False even though a release (with or without a note) happened."""
    broker = ControlBroker(tmp_path / "control.db")
    run_id = "human-action-no-fix-test"
    adapter = _BreaksOnceThenFixedAdapter()
    adapter._broken = True

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
            # Operator claims and releases WITHOUT actually fixing anything.
            broker.claim(rid, holder="operator")
            broker.release(rid, holder="operator")
            simulated["done"] = True
            row = original_get_state(rid)
        return row

    monkeypatch.setattr(broker, "get_state", _get_state_then_resuming)

    result = replay(
        _capability_with_a_checkpointed_stuck_step(),
        {},
        adapter,
        POLICY,
        run_id=run_id,
        evidence_dir=str(tmp_path),
        broker=broker,
        escalation_poll_s=0.01,
        escalation_max_wait_s=2.0,
    )

    # give_up returns the ORIGINAL unresolved failure (the crash on s0's
    # click), not a fresh "escalated" status -- see engine.py's `replay()`
    # loop, `return stop.result` on give_up.
    assert result.status == "failed"
    assert adapter.actions == ["click"]

    record = json.loads((tmp_path / "human_action.json").read_text())
    assert record["operator_note"] is None
    assert record["resume_decision"] == "none"
    assert record["human_performed_pending_action"] is False
