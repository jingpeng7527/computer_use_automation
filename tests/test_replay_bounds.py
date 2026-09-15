"""Execution bounds enforced by replay itself -- no browser, no LLM.

Regression coverage for three real gaps: replay never ran an ExecutionGuard
at all (so an artifact's own step list was the only ceiling); a recoverable
runtime_match's own `max_retries` was never checked -- only the
capability's aggregate `recovery_budget.per_run`, which meant one flaky
matcher could retry all the way up to that shared budget by itself; and
`ExecutionGuard.use_recovery()` was never called at all, so
`policy.execution_bounds.recovery_budget_per_run` -- the actual hard
ceiling policy.yaml documents -- did nothing. An artifact could declare
`recovery_budget.per_run` far above the policy value and replay would
honour it, caught only by max_steps instead of the recovery-specific bound
meant to catch it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cua.replay import replay
from cua.safety import load_policy
from cua.schema import (
    AppProfile,
    Capability,
    Checkpoint,
    OutputSpec,
    Provenance,
    Read,
    RecoveryAction,
    RecoveryBudget,
    RoleName,
    RoleNameStrategy,
    RuntimeMatch,
    Step,
    Target,
)
from cua.surface import BBox, InteractiveNode, Location, ResolutionResult

POLICY = load_policy()
HERE = Location(origin="http://127.0.0.1:8800", path="/members/search")


def _node(text: str = "", role: str | None = None, name: str | None = None, node_id: str = "n0"):
    return InteractiveNode(node_id=node_id, role=role, name=name, text=text, bbox=BBox(x=0, y=0, w=1, h=1))


class _FakeAdapter:
    """Always observes the SAME snapshot -- a recoverable condition that
    never actually clears, so the engine's own bounds are what has to stop
    the run, not the target app happening to change."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def observe(self):
        return self._snapshot

    def resolve(self, target):  # pragma: no cover -- not exercised here
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):
        return None

    def wait_for(self, condition, timeout_ms):
        self.last_wait_timeout_ms = timeout_ms
        return True

    def location(self):
        return HERE


def _capability(
    runtime_matches: list[RuntimeMatch], recovery_budget: RecoveryBudget | None = None, **step_kwargs
) -> Capability:
    return Capability(
        capability_id="test.bounds",
        title="Bounds test",
        summary="A minimal capability existing only to test replay's own execution bounds.",
        app_profile=AppProfile(product="test", version="0"),
        outputs=[OutputSpec(name="x", type="string", description="test output", source_step_id="s0")],
        recovery_budget=recovery_budget or RecoveryBudget(),
        steps=[
            Step(
                id="s0",
                intent="read something",
                action=Read(into="x"),
                risk_level="SAFE_READ",
                target=Target(strategies=[RoleNameStrategy(role="textbox", name="Balance")]),
                checkpoint=Checkpoint(
                    description="reached the member detail heading",
                    all_of=[RoleName(role="heading", name="Member Detail")],
                ),
                **step_kwargs,
            )
        ],
        runtime_matches=runtime_matches,
        possible_outcomes=[],
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


def test_step_bound_is_enforced_by_the_executor_not_the_artifact() -> None:
    """A perfectly ordinary single-step capability must still be stopped by
    the executor's own ceiling once it's configured tight enough -- bounds
    are never something the artifact opts out of by being short."""
    tight = POLICY.model_copy(
        update={"execution_bounds": POLICY.execution_bounds.model_copy(update={"max_steps": 0})}
    )
    adapter = _FakeAdapter([_node(role="textbox", name="Balance", node_id="n_target")])

    result = replay(_capability([]), {}, adapter, tight, run_id="test-run")

    assert result.status == "failed"
    assert result.failure.kind == "bounds_exceeded"


def test_per_matcher_max_retries_stops_before_the_aggregate_budget() -> None:
    """recovery_budget.per_run defaults to 5. A single matcher with
    max_retries=1 must fail after its first retry, not after five --
    otherwise the per-matcher bound the schema declares is decorative."""
    dialog_snapshot = [
        _node(role="textbox", name="Balance", node_id="n_target"),
        _node(role="dialog", name="Session Warning", node_id="n_dialog"),
    ]
    match = RuntimeMatch(
        id="dialog_recover",
        category="recoverable",
        terminal=False,
        detect=RoleName(role="dialog", name="Session Warning"),
        after_step="s0",
        max_retries=1,
        recovery=RecoveryAction(do="dismiss_dialog", target_role="dialog", target_name="Session Warning"),
    )
    adapter = _FakeAdapter(dialog_snapshot)

    result = replay(_capability([match]), {}, adapter, POLICY, run_id="test-run")

    assert result.status == "failed"
    assert result.failure.kind == "recovery_exhausted"
    assert result.failure.observed == "dialog_recover"


def test_wait_timeout_is_clamped_to_the_policy_ceiling() -> None:
    """A step declaring a wait timeout far above policy's per_wait_timeout_ms
    must never be honoured outright -- the executor's ceiling wins, exactly
    like the max_steps/recovery bounds above."""
    from cua.schema import WaitSpec

    adapter = _FakeAdapter(
        [
            _node(role="textbox", name="Balance", node_id="n_target"),
            _node(role="heading", name="Member Detail", node_id="n_head"),
        ]
    )
    capability = _capability(
        [], wait=WaitSpec(until=RoleName(role="heading", name="Member Detail"), timeout_ms=10_000_000)
    )

    replay(capability, {}, adapter, POLICY, run_id="test-run")

    assert adapter.last_wait_timeout_ms == POLICY.execution_bounds.per_wait_timeout_ms


def test_policy_recovery_budget_is_a_hard_ceiling_the_artifact_cannot_raise() -> None:
    """The artifact declares recovery_budget.per_run=100 -- far above
    policy's default of 5 -- and each match's own max_retries is set high
    enough not to be the thing that stops this run. If the policy ceiling
    (ExecutionGuard.use_recovery) isn't actually enforced, this keeps
    retrying past 5 all the way to 100 (or until max_steps, a different
    bound doing a job this one should do)."""
    dialog_snapshot = [
        _node(role="textbox", name="Balance", node_id="n_target"),
        _node(role="dialog", name="Session Warning", node_id="n_dialog"),
    ]
    match = RuntimeMatch(
        id="dialog_recover",
        category="recoverable",
        terminal=False,
        detect=RoleName(role="dialog", name="Session Warning"),
        after_step="s0",
        max_retries=100,
        recovery=RecoveryAction(do="dismiss_dialog", target_role="dialog", target_name="Session Warning"),
    )
    capability = _capability([match], recovery_budget=RecoveryBudget(per_run=100))
    adapter = _FakeAdapter(dialog_snapshot)

    assert POLICY.execution_bounds.recovery_budget_per_run < 100  # the test assumption holds
    result = replay(capability, {}, adapter, POLICY, run_id="test-run")

    assert result.status == "failed"
    assert result.failure.kind == "recovery_exhausted"
    assert result.failure.expected == "within policy.execution_bounds.recovery_budget_per_run"
