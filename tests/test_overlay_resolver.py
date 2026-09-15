"""apply_overlay() -- no browser, no LLM.

Each of the four override ops gets a direct test, plus the two guarantees
REPORT.md sec 4 claims for the resolver: an overlay naming the wrong base
version is rejected, and an overlay that breaks a downstream reference
(skipping a step whose ctx output a later step consumes) is rejected too --
via the resolved capability re-running Capability's own existing
referential-integrity check, not a second copy of that logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cua.overlay import apply_overlay
from cua.schema import (
    AppProfile,
    Capability,
    Checkpoint,
    Click,
    CssStrategy,
    InsertAfter,
    Navigate,
    OutputSpec,
    Overlay,
    ParamSpec,
    Provenance,
    Read,
    ReplaceTarget,
    ReplaceValue,
    RoleName,
    RoleNameStrategy,
    Select,
    Skip,
    Step,
    Target,
    TypeText,
)


def _provenance() -> Provenance:
    return Provenance(
        discovered_at=datetime.now(UTC),
        model="test",
        goal="test",
        discovery_run_id="test",
        transcript_sha256="0" * 64,
    )


def _base_capability(*, version: int = 2) -> Capability:
    return Capability(
        capability_id="acme_core.lookup_savings_balance",
        version=version,
        status="approved",
        title="Lookup Savings Balance",
        summary="look up member {{input.member_id}} and read their current savings balance",
        app_profile=AppProfile(product="acme_core", version="2024.1"),
        inputs=[
            ParamSpec(name="member_id", type="string", description="member id", sensitivity="pii")
        ],
        outputs=[
            OutputSpec(name="balance", type="money", description="savings balance", source_step_id="s3")
        ],
        steps=[
            Step(
                id="s0",
                intent="navigate to the base tenant's search page",
                action=Navigate(url_template="http://127.0.0.1:8800/members/search"),
                risk_level="SAFE_READ",
            ),
            Step(
                id="s1",
                intent="type {{input.member_id}}",
                action=TypeText(value_from="{{input.member_id}}", submit=True),
                risk_level="REVERSIBLE_WRITE",
                target=Target(strategies=[RoleNameStrategy(role="textbox", name="Member Number")]),
            ),
            Step(
                id="s2",
                intent="click 'View'",
                action=Click(),
                risk_level="REVERSIBLE_WRITE",
                target=Target(strategies=[CssStrategy(selector=".action-view-detail")]),
                checkpoint=Checkpoint(
                    description="reached member detail", all_of=[RoleName(role="heading", name="Member Detail")]
                ),
            ),
            Step(
                id="s3",
                intent="read balance",
                action=Read(into="balance"),
                risk_level="SAFE_READ",
                target=Target(strategies=[RoleNameStrategy(role="textbox", name="Savings Balance")]),
            ),
        ],
        success_condition=Checkpoint(
            description="reached member detail", all_of=[RoleName(role="heading", name="Member Detail")]
        ),
        provenance=_provenance(),
    )


def test_replace_target_swaps_only_the_named_step() -> None:
    base = _base_capability()
    overlay = Overlay(
        extends="acme_core.lookup_savings_balance@2",
        tenant_id="cu_northgate",
        overrides=[
            ReplaceTarget(
                step_id="s1",
                target=Target(strategies=[RoleNameStrategy(role="textbox", name="Acct/Member #")]),
            )
        ],
    )
    resolved = apply_overlay(base, overlay)

    assert resolved.capability_id == "acme_core.lookup_savings_balance.cu_northgate"
    assert resolved.status == "draft"
    s1 = next(s for s in resolved.steps if s.id == "s1")
    assert s1.target.strategies[0].name == "Acct/Member #"
    # untouched steps are untouched
    s2 = next(s for s in resolved.steps if s.id == "s2")
    assert s2.target.strategies[0].selector == ".action-view-detail"


def test_replace_value_overrides_a_navigate_steps_url() -> None:
    base = _base_capability()
    overlay = Overlay(
        extends="acme_core.lookup_savings_balance@2",
        tenant_id="cu_northgate",
        overrides=[ReplaceValue(step_id="s0", value="http://127.0.0.1:8800/tenant-b/members/search")],
    )
    resolved = apply_overlay(base, overlay)
    s0 = next(s for s in resolved.steps if s.id == "s0")
    assert s0.action.url_template == "http://127.0.0.1:8800/tenant-b/members/search"


def test_replace_value_refuses_a_non_navigate_step() -> None:
    base = _base_capability()
    overlay = Overlay(
        extends="acme_core.lookup_savings_balance@2",
        tenant_id="cu_northgate",
        overrides=[ReplaceValue(step_id="s1", value="whatever")],
    )
    with pytest.raises(ValueError, match="not supported"):
        apply_overlay(base, overlay)


def test_skip_removes_the_step() -> None:
    base = _base_capability()
    # s2's checkpoint isn't load-bearing for anything downstream, so
    # skipping it is a legal (if slightly artificial) overlay for this test.
    overlay = Overlay(
        extends="acme_core.lookup_savings_balance@2", tenant_id="cu_direct", overrides=[Skip(step_id="s2")]
    )
    resolved = apply_overlay(base, overlay)
    assert [s.id for s in resolved.steps] == ["s0", "s1", "s3"]


def test_skip_that_breaks_a_downstream_ctx_reference_is_rejected() -> None:
    """This is the reference-integrity guarantee REPORT.md sec 4 promises --
    it comes from Capability's own existing validator running against the
    RESOLVED steps, not from any check apply_overlay() writes itself."""
    base = _base_capability()
    # Rebuild s1 to produce ctx.token, and s2 to consume it, so skipping s1
    # breaks a real reference.
    steps = list(base.steps)
    steps[1] = steps[1].model_copy(update={"action": Read(into="ctx.token")})
    steps[2] = steps[2].model_copy(
        update={"action": TypeText(value_from="{{ctx.token}}", submit=True)}
    )
    base_with_ctx = Capability.model_validate({**base.model_dump(mode="json"), "steps": [s.model_dump(mode="json") for s in steps]})

    overlay = Overlay(
        extends="acme_core.lookup_savings_balance@2", tenant_id="cu_broken", overrides=[Skip(step_id="s1")]
    )
    with pytest.raises(Exception, match="ctx.token"):
        apply_overlay(base_with_ctx, overlay)


def test_insert_after_adds_a_new_step_and_a_new_input() -> None:
    base = _base_capability()
    overlay = Overlay(
        extends="acme_core.lookup_savings_balance@2",
        tenant_id="cu_northgate",
        add_inputs=[ParamSpec(name="branch", type="string", description="branch to search within")],
        overrides=[
            InsertAfter(
                step_id="s0",
                step={
                    "id": "s0b",
                    "intent": "select branch {{input.branch}}",
                    "action": {"type": "select", "value_from": "{{input.branch}}", "by": "value"},
                    "risk_level": "REVERSIBLE_WRITE",
                    "target": {
                        "strategies": [
                            {"type": "label_anchor", "label": "Branch:", "relation": "same_row_input"}
                        ]
                    },
                },
            )
        ],
    )
    resolved = apply_overlay(base, overlay)

    assert [s.id for s in resolved.steps] == ["s0", "s0b", "s1", "s2", "s3"]
    assert {p.name for p in resolved.inputs} == {"member_id", "branch"}
    inserted = resolved.steps[1]
    assert isinstance(inserted.action, Select)


def test_add_inputs_colliding_with_a_base_input_is_rejected() -> None:
    base = _base_capability()
    overlay = Overlay(
        extends="acme_core.lookup_savings_balance@2",
        tenant_id="cu_dup",
        add_inputs=[ParamSpec(name="member_id", type="string", description="duplicate on purpose")],
    )
    with pytest.raises(ValueError, match="member_id"):
        apply_overlay(base, overlay)


def test_extends_must_match_the_exact_base_version() -> None:
    base = _base_capability(version=2)
    overlay = Overlay(
        extends="acme_core.lookup_savings_balance@1",  # wrong version
        tenant_id="cu_northgate",
        overrides=[Skip(step_id="s2")],
    )
    with pytest.raises(ValueError, match="extends"):
        apply_overlay(base, overlay)


def test_override_naming_an_unknown_step_id_is_rejected() -> None:
    base = _base_capability()
    overlay = Overlay(
        extends="acme_core.lookup_savings_balance@2",
        tenant_id="cu_northgate",
        overrides=[Skip(step_id="s99")],
    )
    with pytest.raises(ValueError, match="s99"):
        apply_overlay(base, overlay)
