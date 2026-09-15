"""Unattended replay refuses a capability that isn't status="approved" --
no browser, no LLM.

REPORT.md sec 7 named this "the smallest change with the largest safety
return": Capability.status already distinguished draft from approved, but
nothing checked it before this. A draft artifact -- one nobody has
reviewed -- must never run unattended in production, since there's no
human watching to catch its mistakes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cua.replay import replay
from cua.safety import load_policy
from cua.schema import (
    AppProfile,
    Capability,
    Checkpoint,
    Navigate,
    Provenance,
    RoleName,
    Step,
)
from cua.surface import Location

POLICY = load_policy()
HERE = Location(origin="http://127.0.0.1:8800", path="/members/search")


class _UntouchedAdapter:
    """Every method raises -- proves the gate refuses before the adapter
    is touched at all, not merely before a step completes."""

    def observe(self):
        raise AssertionError("must not observe before the approval gate")

    def resolve(self, target):
        raise AssertionError("unused")

    def act(self, action, resolution=None, value=None):
        raise AssertionError("must not act on a draft capability")

    def wait_for(self, condition, timeout_ms):
        raise AssertionError("unused")

    def location(self):
        raise AssertionError("unused")

    def screenshot(self, path):
        raise AssertionError("unused")


def _capability(status: str) -> Capability:
    return Capability(
        capability_id="test.approval_gate",
        status=status,
        title="Approval gate test",
        summary="A minimal capability existing only to test the approval gate.",
        app_profile=AppProfile(product="test", version="0"),
        steps=[
            Step(
                id="s0",
                intent="navigate",
                action=Navigate(url_template="http://127.0.0.1:8800/members/search"),
                risk_level="SAFE_READ",
            )
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


def test_draft_capability_is_refused_before_touching_the_surface() -> None:
    result = replay(_capability("draft"), {}, _UntouchedAdapter(), POLICY, run_id="test-run")

    assert result.status == "failed"
    assert result.failure.kind == "not_approved"
    assert result.failure.observed == "draft"


def test_approved_capability_is_not_blocked_by_the_gate() -> None:
    class _OneShotAdapter(_UntouchedAdapter):
        def act(self, action, resolution=None, value=None):
            return None

        def observe(self):
            return []

        def location(self):
            return HERE

    result = replay(_capability("approved"), {}, _OneShotAdapter(), POLICY, run_id="test-run")

    # Gets past the gate and actually runs the step -- fails later on the
    # unmet success_condition, which is expected and irrelevant here; the
    # point is it wasn't refused for its approval status.
    assert result.status == "failed"
    assert result.failure.kind != "not_approved"
