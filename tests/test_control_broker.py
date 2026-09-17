"""ControlBroker -- SQLite only, no browser. Not one of 系统设计.md sec
1.7's four scoped categories, but cheap and it's the part of the
escalation mechanism a race condition would silently break: a claim race
with no atomicity, or a lease that only expires if something remembers to
check, would both be invisible until two operators collided for real.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from cua.escalation import ControlBroker, find_resume_point
from cua.schema import (
    AppProfile,
    Capability,
    Checkpoint,
    OutputSpec,
    Provenance,
    Read,
    RoleName,
    RoleNameStrategy,
    Step,
    Target,
)
from cua.surface import BBox, InteractiveNode, Location, ResolutionResult


def _broker(tmp_path) -> ControlBroker:
    return ControlBroker(tmp_path / "control.db")


def test_claim_is_conditional_and_only_one_operator_wins(tmp_path) -> None:
    broker = _broker(tmp_path)
    broker.mark_stuck("run-1", "checkpoint_failed")

    first = broker.claim("run-1", holder="alice", lease_seconds=60)
    second = broker.claim("run-1", holder="bob", lease_seconds=60)

    assert first is True
    assert second is False  # already HUMAN with a live lease -- bob's claim matches zero rows
    row = broker.get_state("run-1")
    assert row.holder == "alice"


def test_expired_lease_is_reclaimable_without_a_background_job(tmp_path) -> None:
    broker = _broker(tmp_path)
    broker.mark_stuck("run-2", "checkpoint_failed")
    assert broker.claim("run-2", holder="alice", lease_seconds=0.01)

    time.sleep(0.05)  # lease has now expired; nothing is "running" to notice

    # get_state() itself lazily reports the expiry...
    assert broker.get_state("run-2").state == "PAUSED"
    # ...and a second claim succeeds off the exact same lazy check.
    assert broker.claim("run-2", holder="bob", lease_seconds=60) is True
    assert broker.get_state("run-2").holder == "bob"


def test_release_then_claim_again_after_resume(tmp_path) -> None:
    broker = _broker(tmp_path)
    broker.mark_stuck("run-3", "checkpoint_failed")
    broker.claim("run-3", holder="alice", lease_seconds=60)

    assert broker.release("run-3", holder="alice") is True
    assert broker.get_state("run-3").state == "RESUMING"

    broker.mark_resumed("run-3")
    assert broker.get_state("run-3").state == "AUTOMATION"
    assert broker.is_automations_turn("run-3") is True


# ---- phase/resolution: enforced by the broker itself, not by whichever CLI
# command happens to call release() ----


def test_replay_phase_defaults_and_forbids_resolution(tmp_path) -> None:
    broker = _broker(tmp_path)
    broker.mark_stuck("run-replay", "checkpoint_failed")  # phase defaults to "replay"
    broker.claim("run-replay", holder="alice", lease_seconds=60)

    assert broker.get_state("run-replay").phase == "replay"
    with pytest.raises(ValueError, match="does not take --resolution"):
        broker.release("run-replay", holder="alice", resolution="cleared_obstacle")
    # no resolution at all is the correct, unmarked call -- still works
    assert broker.release("run-replay", holder="alice") is True


def test_discovery_phase_requires_a_valid_resolution(tmp_path) -> None:
    broker = _broker(tmp_path)
    broker.mark_stuck("run-disco", "no progress for 3 consecutive steps", phase="discovery")
    broker.claim("run-disco", holder="alice", lease_seconds=60)

    assert broker.get_state("run-disco").phase == "discovery"
    with pytest.raises(ValueError, match="cleared_obstacle.*workflow_advanced|resolution"):
        broker.release("run-disco", holder="alice")  # missing entirely
    with pytest.raises(ValueError, match="resolution"):
        broker.release("run-disco", holder="alice", resolution="looks fine to me")  # free text, not the enum

    assert broker.release("run-disco", holder="alice", resolution="cleared_obstacle") is True
    row = broker.get_state("run-disco")
    assert row.state == "RESUMING"
    assert row.resolution == "cleared_obstacle"


def test_mark_stuck_rejects_an_unknown_phase(tmp_path) -> None:
    broker = _broker(tmp_path)
    with pytest.raises(ValueError, match="phase"):
        broker.mark_stuck("run-bad", "whatever", phase="not_a_real_phase")


# ---- resume candidate precedence: success_condition before the step's own checkpoint ----


class _FakeAdapter:
    def __init__(self, snapshot: list[InteractiveNode], location: Location) -> None:
        self._snapshot = snapshot
        self._location = location

    def observe(self):
        return self._snapshot

    def resolve(self, target):  # pragma: no cover -- unused here
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):  # pragma: no cover -- unused here
        return None

    def wait_for(self, condition, timeout_ms):  # pragma: no cover -- unused here
        return True

    def location(self):
        return self._location


def _node(role=None, name=None) -> InteractiveNode:
    return InteractiveNode(node_id="n0", role=role, name=name, bbox=BBox(x=0, y=0, w=1, h=1))


def test_resume_prefers_success_condition_over_the_stuck_steps_checkpoint() -> None:
    # A page that satisfies BOTH the capability's overall success_condition
    # AND the stuck step's own checkpoint -- resume must pick "success" and
    # not merely "step", per 系统设计 sec 5.5's stated precedence.
    snapshot = [_node(role="heading", name="Member Detail")]
    here = Location(origin="http://x", path="/done")

    stuck_step = Step(
        id="s0",
        intent="click something",
        action=Read(into="x"),
        risk_level="SAFE_READ",
        target=Target(strategies=[RoleNameStrategy(role="button", name="whatever")]),
        checkpoint=Checkpoint(
            description="also satisfied by this same page",
            all_of=[RoleName(role="heading", name="Member Detail")],
        ),
    )
    capability = Capability(
        capability_id="test.resume",
        title="Resume precedence test",
        summary="test",
        app_profile=AppProfile(product="test", version="0"),
        outputs=[OutputSpec(name="x", type="string", description="test", source_step_id="s0")],
        steps=[stuck_step],
        success_condition=Checkpoint(
            description="reached Member Detail", all_of=[RoleName(role="heading", name="Member Detail")]
        ),
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model="test",
            goal="test",
            discovery_run_id="test",
            transcript_sha256="0" * 64,
        ),
    )

    assert find_resume_point(capability, stuck_step, _FakeAdapter(snapshot, here)) == "success"


def test_resume_falls_back_to_the_stuck_steps_checkpoint() -> None:
    snapshot = [_node(role="heading", name="Search Results")]  # not the final page
    here = Location(origin="http://x", path="/results")

    stuck_step = Step(
        id="s0",
        intent="click something",
        action=Read(into="x"),
        risk_level="SAFE_READ",
        target=Target(strategies=[RoleNameStrategy(role="button", name="whatever")]),
        checkpoint=Checkpoint(
            description="reached search results",
            all_of=[RoleName(role="heading", name="Search Results")],
        ),
    )
    capability = Capability(
        capability_id="test.resume2",
        title="Resume precedence test 2",
        summary="test",
        app_profile=AppProfile(product="test", version="0"),
        outputs=[OutputSpec(name="x", type="string", description="test", source_step_id="s0")],
        steps=[stuck_step],
        success_condition=Checkpoint(
            description="reached Member Detail", all_of=[RoleName(role="heading", name="Member Detail")]
        ),
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model="test",
            goal="test",
            discovery_run_id="test",
            transcript_sha256="0" * 64,
        ),
    )

    assert find_resume_point(capability, stuck_step, _FakeAdapter(snapshot, here)) == "step"


def test_resume_gives_up_when_neither_candidate_holds() -> None:
    snapshot = [_node(role="heading", name="Something Else Entirely")]
    here = Location(origin="http://x", path="/somewhere")

    stuck_step = Step(
        id="s0",
        intent="click something",
        action=Read(into="x"),
        risk_level="SAFE_READ",
        target=Target(strategies=[RoleNameStrategy(role="button", name="whatever")]),
        checkpoint=Checkpoint(
            description="reached search results",
            all_of=[RoleName(role="heading", name="Search Results")],
        ),
    )
    capability = Capability(
        capability_id="test.resume3",
        title="Resume precedence test 3",
        summary="test",
        app_profile=AppProfile(product="test", version="0"),
        outputs=[OutputSpec(name="x", type="string", description="test", source_step_id="s0")],
        steps=[stuck_step],
        success_condition=Checkpoint(
            description="reached Member Detail", all_of=[RoleName(role="heading", name="Member Detail")]
        ),
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model="test",
            goal="test",
            discovery_run_id="test",
            transcript_sha256="0" * 64,
        ),
    )

    assert find_resume_point(capability, stuck_step, _FakeAdapter(snapshot, here)) == "none"
