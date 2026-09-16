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

Both are keyed on the step's ACTION TYPE (Click/TypeText/Select), not on
Step.risk_level -- a second, sharper fix after the first version of this
guard shipped trusting risk_level, which is exactly the kind of
self-reported field this codebase's own design principle (系统设计 P5:
权限不由数据自报) says a safety decision must never trust. A mislabelled
or hand-edited artifact could declare risk_level="SAFE_READ" on a Click
step with nothing to catch it under the first version; action.type can't
be mislabelled the same way, since it's what actually executes.

IRREVERSIBLE gets no separate check here at all -- it needs none. Retrying
falls through to a recursive _run_step() call, which re-resolves the
target and re-runs the SAME gate() every execution goes through; an
IRREVERSIBLE step is refused or escalated there on the very first attempt,
before a checkpoint can even fail, so it never reaches a point where
retry_step could fire.

A third fix, found auditing this guard rather than by comparison: both
layers used to check `match.recovery.do == "retry_step"` specifically --
but dismiss_dialog and reload ALSO fall through to the same _run_step()
retry at the bottom of _apply_match, carrying the identical double-submit
risk, and neither layer caught them. Both now check ANY recoverable match
on a Click/TypeText/Select step, regardless of `do`.
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


def _capability_with_retry_on_a_click_step(*, after_step: str | None, do: str = "retry_step") -> dict:
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
                intent="click save -- not idempotent regardless of its risk_level label",
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
                recovery=RecoveryAction(do=do),
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


def test_schema_rejects_retry_step_on_a_click_step_statically() -> None:
    """The offending step (s0, a Click) is known at validation time -- this
    must be caught before the artifact can even be saved or approved, not
    discovered later during a live replay."""
    with pytest.raises(ValidationError, match="retry_step"):
        Capability.model_validate(_capability_with_retry_on_a_click_step(after_step="s0"))


def test_schema_allows_retry_step_on_a_read_step() -> None:
    # after_step="s1" (a Read) -- this is exactly the legitimate case
    # (retry a flaky read), and must not be rejected.
    cap = Capability.model_validate(_capability_with_retry_on_a_click_step(after_step="s1"))
    assert cap.runtime_matches[0].after_step == "s1"


def test_schema_rejects_retry_step_on_a_click_even_when_mislabelled_safe_read() -> None:
    """The exact bypass a self-reported risk_level would allow: a Click step
    hand-labelled risk_level="SAFE_READ" must still be refused, because the
    guard reads the action's own type, never the label."""
    bad = _capability_with_retry_on_a_click_step(after_step="s0")
    bad["steps"][0]["risk_level"] = "SAFE_READ"  # the mislabel
    with pytest.raises(ValidationError, match="retry_step"):
        Capability.model_validate(bad)


def test_schema_rejects_dismiss_dialog_on_a_click_step_too() -> None:
    """The gap this guard used to have: dismiss_dialog falls through to the
    SAME retry _apply_match performs for retry_step, so it must be refused
    on a Click step exactly as retry_step already is -- not just the one
    `do` value the guard's original version happened to name."""
    with pytest.raises(ValidationError, match="dismiss_dialog"):
        Capability.model_validate(_capability_with_retry_on_a_click_step(after_step="s0", do="dismiss_dialog"))


def test_schema_allows_dismiss_dialog_on_a_read_step() -> None:
    cap = Capability.model_validate(
        _capability_with_retry_on_a_click_step(after_step="s1", do="dismiss_dialog")
    )
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
    to ANY step, so the schema-level guard (which only checks a
    statically-named after_step) can't catch this one -- replay itself
    must refuse it instead of silently re-clicking it."""
    capability = Capability.model_validate(_capability_with_retry_on_a_click_step(after_step=None))
    adapter = _DialogThenGoneAdapter()

    result = replay(capability, {}, adapter, POLICY, run_id="test-run")

    assert result.status == "failed"
    assert result.failure.kind == "recovery_refused"
    assert adapter.click_count == 1  # the original click, never a re-click of Save Changes


def test_replay_refuses_dismiss_dialog_at_runtime_when_after_step_is_none() -> None:
    """Same dynamic backstop, same gap: before this fix, this specific
    scenario -- after_step=None (so the schema can't catch it statically)
    AND do="dismiss_dialog" (so the old do=="retry_step"-only runtime check
    didn't catch it either) -- would have clicked "Save Changes" a second
    time. Now refused at the same point retry_step already was."""
    capability = Capability.model_validate(
        _capability_with_retry_on_a_click_step(after_step=None, do="dismiss_dialog")
    )
    adapter = _DialogThenGoneAdapter()

    result = replay(capability, {}, adapter, POLICY, run_id="test-run")

    assert result.status == "failed"
    assert result.failure.kind == "recovery_refused"
    assert adapter.click_count == 1  # the original click, never a re-click of Save Changes


class _IrreversibleReadAdapter:
    """A Read step whose resolved node's name happens to match an
    irreversible name_contains phrase (policy.yaml: "confirm")."""

    def observe(self):
        return [_node(role="button", name="Confirm Transfer", node_id="n_confirm")]

    def resolve(self, target):
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):
        return "irrelevant"

    def wait_for(self, condition, timeout_ms):
        return True

    def location(self):
        return HERE

    def screenshot(self, path):
        pass


def test_an_irreversible_step_never_even_reaches_retry_step() -> None:
    """"IRREVERSIBLE never retries" doesn't need a special case inside the
    recovery guard: gate() runs before _act() on every attempt, including
    the retry's own recursive re-entry into _run_step, so an IRREVERSIBLE
    step is refused there and never reaches a point where retry_step could
    fire at all -- even though this step's action.type is "read" (which the
    structural guard above would otherwise allow) and it declares a
    retry_step recovery. The declared recovery is simply never consulted."""
    capability = Capability.model_validate(
        {
            "capability_id": "test.irreversible_read",
            "status": "approved",
            "title": "x",
            "summary": "x",
            "app_profile": AppProfile(product="t", version="0").model_dump(mode="json"),
            "outputs": [
                OutputSpec(name="x", type="string", description="d", source_step_id="s0").model_dump(
                    mode="json"
                )
            ],
            "steps": [
                Step(
                    id="s0",
                    intent="read the confirm-transfer control's state",
                    action=Read(into="x"),
                    risk_level="SAFE_READ",
                    target=Target(strategies=[RoleNameStrategy(role="button", name="Confirm Transfer")]),
                    checkpoint=Checkpoint(
                        description="unreachable", all_of=[RoleName(role="heading", name="Done")]
                    ),
                ).model_dump(mode="json")
            ],
            "runtime_matches": [
                RuntimeMatch(
                    id="slow_load",
                    category="recoverable",
                    terminal=False,
                    detect=RoleName(role="button", name="Confirm Transfer"),
                    after_step=None,
                    recovery=RecoveryAction(do="retry_step"),
                ).model_dump(mode="json")
            ],
            "possible_outcomes": [],
            "success_condition": Checkpoint(
                description="unreachable", all_of=[RoleName(role="heading", name="Done")]
            ).model_dump(mode="json"),
            "provenance": Provenance(
                discovered_at=datetime.now(UTC),
                model="test",
                goal="test",
                discovery_run_id="test",
                transcript_sha256="0" * 64,
            ).model_dump(mode="json"),
        }
    )

    result = replay(capability, {}, _IrreversibleReadAdapter(), POLICY, run_id="test-run")

    # policy.yaml ships irreversible_policy: refuse -- a hard failure, never
    # an escalation and never a retry_step recovery attempt.
    assert result.status == "failed"
    assert result.failure.kind == "policy_blocked"
