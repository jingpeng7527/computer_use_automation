"""Allowlist + risk-tiering, against the real config/policy.yaml -- no
browser, no LLM. Not one of the four categories 系统设计.md sec 1.7 names,
but it's the same kind of cheap pure-logic check and it directly covers an
evaluation-criteria item (safety), so it earns its place in the suite.
"""

from __future__ import annotations

from pathlib import Path

from cua.safety import check_allowed, gate, load_policy
from cua.surface import BBox, InteractiveNode, Location, ResolutionResult

POLICY_PATH = Path(__file__).parent.parent / "config" / "policy.yaml"


def _policy():
    return load_policy(POLICY_PATH)


def _resolution_for(name: str) -> ResolutionResult:
    node = InteractiveNode(
        node_id="n9", role="button", name=name, bbox=BBox(x=0, y=0, w=10, h=10), interactive=True
    )
    return ResolutionResult(resolved=True, layer=1, node=node, attempts=[])


def test_read_on_allowed_origin_is_allowed() -> None:
    here = Location(origin="http://127.0.0.1:8800", path="/members/search")
    decision = check_allowed("read", _policy(), current_location=here)
    assert decision.allowed


def test_navigate_off_allowlist_is_refused() -> None:
    decision = check_allowed("navigate", _policy(), destination="http://evil.example.com/steal")
    assert not decision.allowed


def test_unlisted_action_type_is_refused() -> None:
    here = Location(origin="http://127.0.0.1:8800", path="/members/search")
    decision = check_allowed("drag", _policy(), current_location=here)
    assert not decision.allowed


def test_read_is_safe_read_tier() -> None:
    decision = gate("read", _policy())
    assert decision.tier == "SAFE_READ"
    assert decision.allowed_unattended


def test_plain_click_is_reversible_write_tier() -> None:
    decision = gate("click", _policy())
    assert decision.tier == "REVERSIBLE_WRITE"
    assert decision.allowed_unattended


def test_change_credit_limit_is_irreversible_and_refused() -> None:
    decision = gate("click", _policy(), resolution=_resolution_for("Change Credit Limit"))
    assert decision.tier == "IRREVERSIBLE"
    assert not decision.allowed_unattended


def test_irreversible_refuse_does_not_escalate() -> None:
    """policy.yaml ships `irreversible_policy: refuse` -- the executor must
    turn this into a hard, non-escalating failure. Escalating anyway would
    mean "refuse" quietly behaves like "require_confirm", asking a human to
    authorise the exact action the policy says must never run at all."""
    policy = _policy()
    assert policy.irreversible_policy == "refuse"
    decision = gate("click", policy, resolution=_resolution_for("Change Credit Limit"))
    assert not decision.allowed_unattended
    assert not decision.escalate


def test_irreversible_require_confirm_does_escalate() -> None:
    policy = _policy().model_copy(update={"irreversible_policy": "require_confirm"})
    decision = gate("click", policy, resolution=_resolution_for("Change Credit Limit"))
    assert not decision.allowed_unattended
    assert decision.escalate
