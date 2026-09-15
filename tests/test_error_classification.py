"""Error classification -- no browser, no LLM. Two things live here:

1. classify_divergence(): pure and mechanical, exactly as designed --
   given what's actually on screen (plus the FailureKind the engine
   already assigned), read off business_outcome / recoverable /
   hard_failure without a model in the loop.
2. The replay engine's load-bearing ordering: a TERMINAL runtime_match is
   checked BEFORE the step's own checkpoint, on the same snapshot. Get
   this backwards and "no such member" becomes a reported crash instead
   of the correct answer -- the exact mistake the brief calls out.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cua.agent import classify_divergence
from cua.replay import replay
from cua.safety import load_policy
from cua.schema import (
    AppProfile,
    Capability,
    Checkpoint,
    OutputSpec,
    Provenance,
    Read,
    RoleName,
    RoleNameStrategy,
    RuntimeMatch,
    Step,
    Target,
    TextContains,
    TextMatcher,
)
from cua.surface import BBox, InteractiveNode, Location, ResolutionResult

POLICY = load_policy()
HERE = Location(origin="http://127.0.0.1:8800", path="/members/search")


def _node(
    text: str = "",
    role: str | None = None,
    name: str | None = None,
    interactive: bool = False,
    node_id: str = "n0",
) -> InteractiveNode:
    return InteractiveNode(
        node_id=node_id, role=role, name=name, text=text, bbox=BBox(x=0, y=0, w=1, h=1), interactive=interactive
    )


# ---- classify_divergence: the three categories, read mechanically -------


def test_stable_page_with_no_error_signal_is_a_business_outcome() -> None:
    snapshot = [_node(text="No member records match")]
    assert classify_divergence(snapshot, failure_kind="target_not_found", policy=POLICY) == "business_outcome"


def test_a_dialog_shaped_element_is_recoverable() -> None:
    snapshot = [_node(role="dialog", name="Session Warning")]
    assert classify_divergence(snapshot, failure_kind="checkpoint_failed", policy=POLICY) == "recoverable"


def test_error_page_text_is_a_hard_failure() -> None:
    snapshot = [_node(text="System Error 500: an internal failure occurred")]
    assert classify_divergence(snapshot, failure_kind="checkpoint_failed", policy=POLICY) == "hard_failure"


def test_an_exception_during_act_is_always_a_hard_failure() -> None:
    # even a page with no visible error text -- the engine already knows
    # something threw, which is decisive on its own.
    snapshot = [_node(text="whatever happens to be on screen")]
    assert classify_divergence(snapshot, failure_kind="app_error", policy=POLICY) == "hard_failure"


def test_a_dialog_containing_error_text_is_a_hard_failure_not_recoverable() -> None:
    # The dangerous ordering bug this guards against: a dialog-shaped
    # element is not automatically "recoverable" (dismiss and retry) if
    # what it actually says is a real fault -- retrying a genuine server
    # error is the wrong response, and a wrong "recoverable" here would
    # have driven the replay engine's retry budget straight at it.
    snapshot = [_node(role="dialog", name="Error", text="System Error 500: an internal failure occurred")]
    assert classify_divergence(snapshot, failure_kind="checkpoint_failed", policy=POLICY) == "hard_failure"


def test_hard_failure_signals_come_from_policy_not_a_hardcoded_list() -> None:
    # A phrase this target app never uses, so the demo policy's fixed
    # signal list won't catch it on its own -- proves the check actually
    # reads policy.error_classification rather than a module constant.
    custom_policy = POLICY.model_copy(
        update={
            "error_classification": POLICY.error_classification.model_copy(
                update={"hard_failure_signals": ["service unavailable"]}
            )
        }
    )
    snapshot = [_node(text="503 Service Unavailable -- please try again later")]
    assert classify_divergence(snapshot, failure_kind="checkpoint_failed", policy=custom_policy) == "hard_failure"
    assert classify_divergence(snapshot, failure_kind="checkpoint_failed", policy=POLICY) == "business_outcome"


# ---- replay engine ordering: terminal match wins over the checkpoint ----


class _FakeAdapter:
    """A scripted SurfaceAdapter: one canned snapshot, no real browser.
    Exists purely to prove the engine's *ordering* logic -- resolve() is
    never exercised because the step below has no target."""

    def __init__(self, snapshot: list[InteractiveNode], location: Location) -> None:
        self._snapshot = snapshot
        self._location = location

    def observe(self):
        return self._snapshot

    def resolve(self, target):  # pragma: no cover -- unused, no step.target here
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None):
        return None

    def wait_for(self, condition, timeout_ms):  # pragma: no cover -- no step.wait here
        return True

    def location(self):
        return self._location


def _one_step_capability(runtime_matches: list[RuntimeMatch]) -> Capability:
    return Capability(
        capability_id="test.terminal_ordering",
        status="approved",  # these tests exercise ordering/classification, not the approval gate
        title="Terminal ordering test",
        summary="A minimal capability existing only to test replay's step ordering.",
        app_profile=AppProfile(product="test", version="0"),
        outputs=[OutputSpec(name="x", type="string", description="test output", source_step_id="s0")],
        steps=[
            Step(
                id="s0",
                intent="read something",
                action=Read(into="x"),
                risk_level="SAFE_READ",
                target=Target(strategies=[RoleNameStrategy(role="textbox", name="Balance")]),
                # A checkpoint the canned snapshot will NOT satisfy -- if the
                # engine checked this before the terminal match, it would
                # fall through to "no remaining match" and report FAILED.
                checkpoint=Checkpoint(
                    description="reached the member detail heading",
                    all_of=[RoleName(role="heading", name="Member Detail")],
                ),
            )
        ],
        runtime_matches=runtime_matches,
        possible_outcomes=["MEMBER_NOT_FOUND"] if runtime_matches else [],
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


def test_terminal_match_is_checked_before_the_checkpoint() -> None:
    snapshot = [
        _node(role="textbox", name="Balance", interactive=True, node_id="n_target"),
        _node(text="No member records match", node_id="n_text"),
    ]
    capability = _one_step_capability(
        [
            RuntimeMatch(
                id="not_found",
                category="business_outcome",
                terminal=True,
                detect=TextContains(text=TextMatcher(mode="contains", value="No member records match")),
                after_step="s0",
                result_code="MEMBER_NOT_FOUND",
            )
        ]
    )
    adapter = _FakeAdapter(snapshot, HERE)

    result = replay(capability, {}, adapter, POLICY, run_id="test-run")

    assert result.status == "business_outcome"
    assert result.outcome.code == "MEMBER_NOT_FOUND"


def test_no_matching_runtime_match_falls_back_to_checkpoint_failed() -> None:
    snapshot = [
        _node(role="textbox", name="Balance", interactive=True, node_id="n_target"),
        _node(text="something unrelated", node_id="n_text"),
    ]
    capability = _one_step_capability([])  # nothing declared to explain a divergence
    adapter = _FakeAdapter(snapshot, HERE)

    result = replay(capability, {}, adapter, POLICY, run_id="test-run")

    assert result.status == "failed"
    assert result.failure.kind == "checkpoint_failed"
