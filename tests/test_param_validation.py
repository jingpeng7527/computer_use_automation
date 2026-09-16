"""_validate_params, exercised directly -- previously untested at all (no
test file called _validate_params or asserted on FailureKind.param_invalid
before this one), which is how an enum-typed ParamSpec.enum_values went
undetected as declared-but-never-enforced: `required` and `pattern` were
(accidentally) exercised end-to-end by other tests using real artifacts,
but nothing ever passed an out-of-range enum value.

A `_NeverTouchedAdapter` proves param validation runs BEFORE replay ever
touches the surface, same guarantee `required`/`pattern` already had:
any adapter call raises, so a passing test here means validation short-
circuited before the loop started.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cua.replay import replay
from cua.safety import load_policy
from cua.schema import (
    AppProfile,
    Capability,
    Checkpoint,
    OutputSpec,
    ParamSpec,
    Provenance,
    Read,
    RoleName,
    RoleNameStrategy,
    Step,
    Target,
)

POLICY = load_policy()


class _NeverTouchedAdapter:
    def observe(self):
        raise AssertionError("param validation should refuse before the surface is ever touched")

    def resolve(self, target):
        raise AssertionError("param validation should refuse before the surface is ever touched")

    def act(self, action, resolution=None, value=None):
        raise AssertionError("param validation should refuse before the surface is ever touched")

    def wait_for(self, condition, timeout_ms):
        raise AssertionError("param validation should refuse before the surface is ever touched")

    def location(self):
        raise AssertionError("param validation should refuse before the surface is ever touched")

    def screenshot(self, path):
        raise AssertionError("param validation should refuse before the surface is ever touched")


def _capability_with_enum_input() -> Capability:
    return Capability(
        capability_id="test.enum_param",
        status="approved",
        title="x",
        summary="x",
        app_profile=AppProfile(product="test", version="0"),
        inputs=[
            ParamSpec(
                name="account_type",
                type="enum",
                description="checking or savings",
                enum_values=["checking", "savings"],
            )
        ],
        outputs=[OutputSpec(name="x", type="string", description="d", source_step_id="s0")],
        steps=[
            Step(
                id="s0",
                intent="read something",
                action=Read(into="x"),
                risk_level="SAFE_READ",
                target=Target(strategies=[RoleNameStrategy(role="heading", name="whatever")]),
            )
        ],
        possible_outcomes=[],
        success_condition=Checkpoint(
            description="unreachable", all_of=[RoleName(role="heading", name="Done")]
        ),
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model="test",
            goal="test",
            discovery_run_id="test",
            transcript_sha256="0" * 64,
        ),
    )


def test_an_out_of_range_enum_value_is_refused_before_touching_the_surface() -> None:
    capability = _capability_with_enum_input()

    result = replay(
        capability, {"account_type": "frobnicate"}, _NeverTouchedAdapter(), POLICY, run_id="test-run"
    )

    assert result.status == "failed"
    assert result.failure.kind == "param_invalid"
    assert "checking" in result.failure.expected
    assert "savings" in result.failure.expected


def test_a_value_within_the_declared_enum_is_accepted() -> None:
    capability = _capability_with_enum_input()

    # Passes param validation and only THEN reaches the surface, where the
    # fake adapter's own AssertionError propagates uncaught (target
    # resolution isn't wrapped in a try/except the way _act() is) --
    # proving this value cleared param_invalid rather than being silently
    # skipped some other way.
    with pytest.raises(AssertionError, match="should refuse before the surface"):
        replay(capability, {"account_type": "savings"}, _NeverTouchedAdapter(), POLICY, run_id="test-run")


def test_no_enum_values_declared_means_no_enum_check_at_all() -> None:
    """A type="enum" ParamSpec with enum_values left unset (None) is not a
    contradiction to catch here -- the pattern regex, if any, is still the
    only check, exactly as before this fix. This is what keeps the fix
    additive: nothing that validated before now starts failing."""
    base = _capability_with_enum_input()
    loose = base.model_copy(
        update={"inputs": [p.model_copy(update={"enum_values": None}) for p in base.inputs]}
    )

    with pytest.raises(AssertionError, match="should refuse before the surface"):
        replay(loose, {"account_type": "anything at all"}, _NeverTouchedAdapter(), POLICY, run_id="test-run")
