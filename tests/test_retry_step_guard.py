"""retry_step recovery must never re-perform a non-idempotent action -- no
browser, no LLM.

Found by comparing this project against another team's submission for the
same assignment: their replay engine explicitly refuses RETRY on a
non-SAFE action ("a checkpoint failing does not prove a click didn't
land"), and this codebase had no equivalent check. Verified real before
fixing: nothing previously stopped a declared retry_step recovery from
re-clicking a REVERSIBLE_WRITE or IRREVERSIBLE step, which risks
performing it twice (e.g. a double-submitted payment).

Two layers, matching how the rest of this codebase does defense in depth:
a schema-level guard when the offending step is known statically
(Capability._referential_integrity), and a replay-time backstop for an
after_step=None matcher, which the schema can't check statically since it
could land on any step.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

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
    RecoveryAction,
    RoleName,
    RoleNameStrategy,
    RuntimeMatch,
    Step,
    Target,
)
from cua.surface import BBox, InteractiveNode, Location, ResolutionResult

POLICY = load_policy()
HERE = Location(origin="http://127.0.0.1:8800", path="/members/search")


def _node(role: str | None = None, name: str | None = None, node_id: str = "n0"):
    return InteractiveNode(node_id=node_id, role=role, name=name, bbox=BBox(x=0, y=0, w=1, h=1))


def _capability_with_retry_on_a_click_step(*, after_step: str | None) -> dict:
    """A raw dict, not a validated Capability -- the caller decides whether
    to expect model_validate to raise (static case) or to bypass validation
    another way (dynamic-only case)."""
    return {
        "capability_id": "test.retry_guard",
        "status": "approved",
        "title": "Retry guard test",
        "summary": "A minimal capability existing only to test the retry_step guard.",
        "app_profile": AppProfile(product="test", version="0").model_dump(mode="json"),
        "outputs": [
            OutputSpec(name="x", type="string", description="test output", source_step_id="s1").model_dump(
                mode="json"
            )
        ],
        "steps": [
            Step(
                id="s0",
                intent="click save -- REVERSIBLE_WRITE, not idempotent",
                action=Click(),
                risk_level="REVERSIBLE_WRITE",
                target=Target(strategies=[RoleNameStrategy(role="button", name="Save Changes")]),
                # Never holds in the runtime test's adapter -- forces the
                # engine past the terminal-match check into the same
                # remaining-match fallback a real stuck checkpoint would,
                # which is the only path that ever consults a non-terminal
                # (recoverable) match at all.
                checkpoint=Checkpoint(
                    description="a 'Saved' heading appears", all_of=[RoleName(role="heading", name="Saved")]
                ),
            ).model_dump(mode="json"),
            Step(
                id="s1",
                intent="read something",
                action=Read(into="x"),
                risk_level="SAFE_READ",
                target=Target(strategies=[RoleNameStrategy(role="textbox", name="Balance")]),
            ).model_dump(mode="json"),
        ],
        "runtime_matches": [
            RuntimeMatch(
                id="slow_load",
                category="recoverable",
                terminal=False,
                detect=RoleName(role="dialog", name="Loading"),
                after_step=after_step,
                recovery=RecoveryAction(do="retry_step"),
            ).model_dump(mode="json")
        ],
        "possible_outcomes": [],
        "success_condition": Checkpoint(
            description="unreachable in this test", all_of=[RoleName(role="heading", name="Done")]
        ).model_dump(mode="json"),
        "provenance": Provenance(
            discovered_at=datetime.now(UTC),
            model="test",
            goal="test",
            discovery_run_id="test",
            transcript_sha256="0" * 64,
        ).model_dump(mode="json"),
    }


def test_schema_rejects_retry_step_on_a_non_safe_step_statically() -> None:
    """The offending step (s0, REVERSIBLE_WRITE) is known at validation time
    -- this must be caught before the artifact can even be saved or
    approved, not discovered later during a live replay."""
    with pytest.raises(ValidationError, match="retry_step"):
        Capability.model_validate(_capability_with_retry_on_a_click_step(after_step="s0"))


def test_schema_allows_retry_step_on_a_safe_read_step() -> None:
    # after_step="s1" (SAFE_READ) -- this is exactly the legitimate case
    # (retry a flaky read), and must not be rejected.
    cap = Capability.model_validate(_capability_with_retry_on_a_click_step(after_step="s1"))
    assert cap.runtime_matches[0].after_step == "s1"


class _DialogThenGoneAdapter:
    """A dialog blocks step s0's checkpoint permanently in this test --
    nothing here ever dismisses it, since the point is that retry_step
    must be refused before ever attempting a second click, not that
    retrying would have helped."""

    def __init__(self):
        self.click_count = 0

    def observe(self):
        return [
            _node(role="button", name="Save Changes", node_id="n_submit"),
            _node(role="dialog", name="Loading", node_id="n_dialog"),
        ]

    def resolve(self, target):
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):
        if action.type == "click":
            self.click_count += 1

    def wait_for(self, condition, timeout_ms):
        return True

    def location(self):
        return HERE

    def screenshot(self, path):
        pass


def test_replay_refuses_retry_step_at_runtime_when_after_step_is_none() -> None:
    """The dynamic backstop: after_step=None means this matcher could apply
    to ANY step, including the non-SAFE_READ s0, so the schema-level guard
    (which only checks a statically-named after_step) can't catch this one
    -- replay itself must refuse it instead of silently re-clicking it."""
    capability = Capability.model_validate(_capability_with_retry_on_a_click_step(after_step=None))
    adapter = _DialogThenGoneAdapter()

    result = replay(capability, {}, adapter, POLICY, run_id="test-run")

    assert result.status == "failed"
    assert result.failure.kind == "recovery_refused"
    assert adapter.click_count == 1  # the original click, never a re-click of Save Changes
