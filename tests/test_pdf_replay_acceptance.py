"""Black-box acceptance tests derived from Assignment A, sections 3.3--3.5.

These tests deliberately use a tiny in-memory SurfaceAdapter rather than the
project's discovery code or a browser.  They answer the assignment-level
question: given an approved artifact and a live surface, does replay produce
the right, safe result *without* a model making decisions?
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path

from cua.replay import replay
from cua.safety import load_policy
from cua.schema import (
    AppProfile,
    Capability,
    Checkpoint,
    Money,
    Navigate,
    OutputSpec,
    Provenance,
    Read,
    RecoveryAction,
    RoleName,
    RoleNameStrategy,
    RuntimeMatch,
    Sensitivity,
    Step,
    Target,
    TextContains,
    TextMatcher,
    UrlMatches,
)
from cua.surface import BBox, InteractiveNode, Location, ResolutionResult

POLICY = load_policy(Path(__file__).parents[1] / "config" / "policy.yaml")
HERE = Location(origin="http://127.0.0.1:8800", path="/members/12345/detail")


def _node(
    node_id: str,
    *,
    role: str | None = None,
    name: str | None = None,
    text: str = "",
    interactive: bool = False,
) -> InteractiveNode:
    return InteractiveNode(
        node_id=node_id,
        role=role,
        name=name,
        text=text,
        bbox=BBox(x=0, y=0, w=20, h=10),
        interactive=interactive,
    )


class ScriptedSurface:
    """A stateful, deterministic stand-in for the live target application.

    The adapter has no model or browser behind it.  Its state transitions let
    each test describe a user-visible situation such as a dialog, an expected
    business result, or a target-app failure.
    """

    def __init__(self, state: str = "ready", *, read_value: str = "$8,160.00 USD") -> None:
        self.state = state
        self.read_value = read_value
        self.actions: list[str] = []
        self.screenshots: list[Path] = []

    def observe(self) -> list[InteractiveNode]:
        balance = _node(
            "balance", role="textbox", name="Savings Balance", interactive=True
        )
        heading = _node("heading", role="heading", name="Member Detail")

        if self.state == "dialog":
            return [
                balance,
                _node("warning", role="dialog", name="Session Warning", interactive=True),
            ]
        if self.state == "outcome":
            return [balance, _node("message", text="No member records match")]
        if self.state == "outcome_on_read":
            return [balance]
        return [balance, heading]

    def resolve(self, target: Target) -> ResolutionResult:  # pragma: no cover - replay resolves snapshots itself
        return ResolutionResult(resolved=False, attempts=[])

    def act(self, action, resolution=None, value=None) -> str | None:
        self.actions.append(action.type)
        if action.type == "read":
            if self.state == "app_error":
                raise RuntimeError("target application crashed")
            if self.state == "outcome_on_read":
                self.state = "outcome"
            return self.read_value
        if action.type == "click" and resolution and resolution.node and resolution.node.name == "Session Warning":
            self.state = "ready"
        return None

    def wait_for(self, condition, timeout_ms: int) -> bool:  # pragma: no cover - no acceptance scenario waits
        return True

    def location(self) -> Location:
        return HERE

    def screenshot(self, path: str) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"failure evidence")
        self.screenshots.append(output)


def _provenance() -> Provenance:
    return Provenance(
        discovered_at=datetime.now(UTC),
        model="discovery-model",  # provenance only; replay must never invoke it.
        goal="Read the savings balance for {{input.member_id}}.",
        discovery_run_id="acceptance-discovery",
        transcript_sha256="0" * 64,
    )


def _read_capability(
    *,
    runtime_matches: list[RuntimeMatch] | None = None,
    possible_outcomes: list[str] | None = None,
    output_sensitivity: Sensitivity = "none",
) -> Capability:
    return Capability(
        capability_id="acceptance.read_savings_balance",
        version=7,
        status="approved",
        title="Read a member savings balance",
        summary="Returns the current savings balance for a supplied member reference.",
        app_profile=AppProfile(product="acceptance-bank", version="2026.09"),
        outputs=[
            OutputSpec(
                name="savings_balance",
                type="money",
                description="Current savings balance.",
                source_step_id="read_balance",
                sensitivity=output_sensitivity,
            )
        ],
        steps=[
            Step(
                id="read_balance",
                intent="Read the savings balance displayed in member detail.",
                action=Read(into="savings_balance"),
                risk_level="SAFE_READ",
                target=Target(
                    strategies=[
                        RoleNameStrategy(
                            role="textbox",
                            name="Savings Balance",
                            rationale="Accessible name is stable in the target UI.",
                        )
                    ]
                ),
                checkpoint=Checkpoint(
                    description="The member detail screen is visible.",
                    all_of=[RoleName(role="heading", name="Member Detail")],
                ),
            )
        ],
        runtime_matches=runtime_matches or [],
        possible_outcomes=possible_outcomes or [],
        success_condition=Checkpoint(
            description="The member detail screen is still visible.",
            all_of=[RoleName(role="heading", name="Member Detail")],
        ),
        provenance=_provenance(),
    )


def test_replay_is_deterministic_and_never_enters_discovery_or_network(monkeypatch) -> None:
    """PDF 3.3: replay is fully deterministic and makes no LLM decisions.

    In addition to running the same artifact twice, the test turns the
    discovery entry point and all outbound TCP connections into immediate
    failures.  A passing replay therefore neither asks an LLM to decide a
    step nor sends a model/network request.
    """

    from cua.agent import loop as agent_loop

    def forbidden(*args, **kwargs):
        raise AssertionError("replay attempted a prohibited discovery or network interaction")

    monkeypatch.setattr(agent_loop, "run_discovery", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    capability = _read_capability()
    first_surface = ScriptedSurface()
    second_surface = ScriptedSurface()

    first = replay(capability, {}, first_surface, POLICY, run_id="deterministic-1")
    second = replay(capability, {}, second_surface, POLICY, run_id="deterministic-2")

    assert first.status == second.status == "success"
    assert first_surface.actions == second_surface.actions == ["read"]
    assert first.outputs == second.outputs == {
        "savings_balance": Money(amount_minor=816000, currency="USD")
    }


def test_replay_returns_a_declared_business_outcome_not_a_checkpoint_error() -> None:
    """PDF 3.3: a legitimate 'not found' state is a result, not a crash."""

    capability = _read_capability(
        runtime_matches=[
            RuntimeMatch(
                id="member_not_found_page",
                category="business_outcome",
                terminal=True,
                detect=TextContains(text=TextMatcher(value="No member records match")),
                after_step="read_balance",
                result_code="MEMBER_NOT_FOUND",
            )
        ],
        possible_outcomes=["MEMBER_NOT_FOUND"],
    )

    result = replay(capability, {}, ScriptedSurface("outcome_on_read"), POLICY, run_id="not-found")

    assert result.status == "business_outcome"
    assert result.outcome is not None
    assert result.outcome.code == "MEMBER_NOT_FOUND"
    assert result.failure is None
    assert result.steps[-1].status == "outcome"


def test_replay_applies_a_declared_recovery_then_retries_the_same_step() -> None:
    """PDF 3.3: known recoverable UI conditions are bounded and explicit."""

    capability = _read_capability(
        runtime_matches=[
            RuntimeMatch(
                id="session_warning",
                category="recoverable",
                terminal=False,
                detect=RoleName(role="dialog", name="Session Warning"),
                after_step="read_balance",
                recovery=RecoveryAction(
                    do="dismiss_dialog", target_role="dialog", target_name="Session Warning"
                ),
                max_retries=1,
            )
        ]
    )
    surface = ScriptedSurface("dialog")

    result = replay(capability, {}, surface, POLICY, run_id="recover-dialog")

    assert result.status == "success"
    assert surface.actions == ["read", "click", "read"]
    assert [step.status for step in result.steps] == ["recovered", "ok"]
    assert result.steps[0].recoveries_applied == ["session_warning"]


def test_hard_failure_carries_structured_context_and_screenshot_evidence(tmp_path: Path) -> None:
    """PDF 3.5: failures preserve expected/observed context plus rich evidence."""

    evidence_dir = tmp_path / "evidence"
    result = replay(
        _read_capability(),
        {},
        ScriptedSurface("app_error"),
        POLICY,
        run_id="app-error",
        evidence_dir=str(evidence_dir),
    )

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.kind == "app_error"
    assert result.failure.step_id == "read_balance"
    assert result.failure.expected == "action to succeed"
    assert result.failure.observed == "target application crashed"
    assert result.failure.screenshot_ref == str(evidence_dir / "read_balance-failure.png")
    assert (evidence_dir / "read_balance-failure.png").read_bytes() == b"failure evidence"


def test_replay_blocks_an_off_allowlist_navigation_before_it_touches_the_surface() -> None:
    """PDF 3.4: policy is enforced by replay, not trusted from artifact data."""

    capability = Capability(
        capability_id="acceptance.blocked_navigation",
        status="approved",  # this test exercises the allowlist gate, not the approval gate
        title="Blocked navigation test",
        summary="Attempts an out-of-scope navigation only to validate the executor policy gate.",
        app_profile=AppProfile(product="acceptance-bank", version="2026.09"),
        steps=[
            Step(
                id="leave_scope",
                intent="Attempt to leave the configured application scope.",
                action=Navigate(url_template="https://evil.example/collect"),
                risk_level="SAFE_READ",  # deliberately misleading; executor must ignore this hint.
            )
        ],
        success_condition=Checkpoint(
            description="This condition is never reached because policy blocks the action.",
            all_of=[UrlMatches(pattern="*/never")],
        ),
        provenance=_provenance(),
    )
    surface = ScriptedSurface()

    result = replay(capability, {}, surface, POLICY, run_id="blocked-navigation")

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.kind == "policy_blocked"
    assert surface.actions == []


def test_replay_masks_a_declared_sensitive_output_at_the_result_boundary() -> None:
    """PDF 3.4: raw sensitive values must not escape through replay results."""

    result = replay(
        _read_capability(output_sensitivity="pii"),
        {},
        ScriptedSurface(read_value="$8,160.00 USD"),
        POLICY,
        run_id="redacted-output",
    )

    assert result.status == "success"
    assert result.outputs == {"savings_balance": "<pii:13 chars>"}
    assert result.redactions == ["savings_balance"]
