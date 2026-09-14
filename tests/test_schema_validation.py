"""Builds the "lookup savings balance" example (the same one walked through
in plain language: search box -> type member id -> read balance -> handle
"no such member") as a real Capability using the redesigned schema, and
checks the guardrails the design docs call for: money is never a float,
possible_outcomes matches runtime_matches exactly (both directions), recovery
can't nest, locator layers can't be out of order or duplicated, every
input/ctx/output reference actually resolves to something, the discovery
goal can't smuggle in a raw sensitive value, and the replay result contract
enforces its four-way status mechanically.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cua.schema import (
    AppProfile,
    BboxStrategy,
    Capability,
    Checkpoint,
    Click,
    LabelAnchorStrategy,
    Money,
    OutputSpec,
    ParamSpec,
    Provenance,
    Read,
    RecoveryBudget,
    ReplayResult,
    RoleName,
    RoleNameStrategy,
    RuntimeMatch,
    Step,
    Target,
    TextContains,
    TextMatcher,
    TypeText,
)


def _lookup_savings_balance() -> Capability:
    member_id_box = Target(
        strategies=[
            LabelAnchorStrategy(
                label="Member Number:",
                relation="same_row_input",
                rationale="no accessible name on this legacy input; the adjacent "
                "label text is the only stable thing about it",
            ),
        ]
    )
    balance_field = Target(
        strategies=[
            LabelAnchorStrategy(
                label="Savings Balance",
                relation="next_cell",
                rationale="detail page has no test id on this cell; label text is stable",
            ),
        ]
    )

    return Capability(
        capability_id="acme_core.lookup_savings_balance",
        version=1,
        title="Look up a member's savings balance",
        summary="Given a member ID, searches the servicing console and reads back "
        "the current savings balance, or reports that no such member exists.",
        app_profile=AppProfile(product="acme_core", version="2024.1"),
        inputs=[
            ParamSpec(
                name="member_id",
                type="string",
                pattern="^[0-9]{5}$",
                description="the member's numeric account id",
                sensitivity="pii",
            ),
        ],
        outputs=[
            OutputSpec(
                name="savings_balance",
                type="money",
                description="current savings balance",
                source_step_id="read_balance",
            ),
        ],
        possible_outcomes=["MEMBER_NOT_FOUND"],
        steps=[
            Step(
                id="open_search",
                intent="open the member search page",
                action={"type": "navigate", "url_template": "{base_url}/members/search"},
                risk_level="SAFE_READ",
            ),
            Step(
                id="enter_member_id",
                intent="enter the member id and submit",
                action=TypeText(value_from="{{input.member_id}}", submit=True),
                risk_level="REVERSIBLE_WRITE",
                target=member_id_box,
                checkpoint=Checkpoint(
                    description="member detail panel appeared",
                    all_of=[RoleName(role="heading", name="Member Detail")],
                ),
            ),
            Step(
                id="read_balance",
                intent="read the savings balance figure",
                action=Read(into="savings_balance"),
                risk_level="SAFE_READ",
                target=balance_field,
            ),
        ],
        runtime_matches=[
            RuntimeMatch(
                id="member_not_found",
                category="business_outcome",
                terminal=True,
                detect=TextContains(
                    text=TextMatcher(mode="contains", value="No member records match")
                ),
                after_step="enter_member_id",
                result_code="MEMBER_NOT_FOUND",
            ),
        ],
        success_condition=Checkpoint(
            description="savings balance was read",
            all_of=[RoleName(role="heading", name="Member Detail")],
        ),
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model="gemini-2.5-flash",
            # Templated, not the literal member id used during recording --
            # same reference-by-name discipline as the steps themselves.
            goal="look up member {{input.member_id}} and read their current savings balance",
            discovery_run_id="discovery-r001",
            transcript_sha256="0" * 64,
        ),
    )


def _as_dict(cap: Capability) -> dict:
    return cap.model_dump(mode="json")


def test_capability_builds_and_round_trips_through_json() -> None:
    cap = _lookup_savings_balance()
    restored = Capability.model_validate_json(cap.model_dump_json())
    assert restored == cap
    assert restored.steps[1].target.strategies[0].type == "label_anchor"


def test_money_output_cannot_be_a_bare_number() -> None:
    with pytest.raises(ValidationError):
        OutputSpec(
            name="savings_balance",
            type="number",  # not in ParamType -- money is the only currency type
            description="x",
            source_step_id="s1",
        )


def test_result_output_value_can_be_a_typed_money_object() -> None:
    now = datetime.now(UTC)
    result = ReplayResult(
        capability_id="acme_core.lookup_savings_balance",
        capability_version=1,
        run_id="replay_1",
        status="success",
        outputs={"savings_balance": Money(amount_minor=815000, currency="USD")},
        started_at=now,
        ended_at=now,
        evidence_dir="evidence/replay_1",
    )
    assert result.outputs["savings_balance"].amount_minor == 815000


def test_possible_outcomes_must_match_runtime_matches_exactly() -> None:
    cap = _lookup_savings_balance()
    # Superset: declares a code no runtime_match produces.
    too_many = {**_as_dict(cap), "possible_outcomes": ["MEMBER_NOT_FOUND", "ACCOUNT_FROZEN"]}
    with pytest.raises(ValidationError):
        Capability.model_validate(too_many)
    # Subset: a runtime_match can produce a code that's undeclared.
    too_few = {**_as_dict(cap), "possible_outcomes": []}
    with pytest.raises(ValidationError):
        Capability.model_validate(too_few)


def test_recovery_budget_max_depth_is_locked_to_one() -> None:
    RecoveryBudget(max_depth=1)  # fine
    with pytest.raises(ValidationError):
        RecoveryBudget(max_depth=2)


def test_business_outcome_match_requires_a_result_code() -> None:
    with pytest.raises(ValidationError):
        RuntimeMatch(
            id="x",
            category="business_outcome",
            terminal=True,
            detect=TextContains(text=TextMatcher(value="whatever")),
        )


def test_target_layers_must_be_strictly_increasing_with_no_repeats() -> None:
    Target(strategies=[RoleNameStrategy(role="button", name="Search"), BboxStrategy(x=1, y=1, w=1, h=1)])
    with pytest.raises(ValidationError):  # layer 4 before layer 1
        Target(
            strategies=[
                BboxStrategy(x=1, y=1, w=1, h=1),
                RoleNameStrategy(role="button", name="Search"),
            ]
        )
    with pytest.raises(ValidationError):  # two layer-1 strategies
        Target(
            strategies=[
                RoleNameStrategy(role="button", name="Search"),
                RoleNameStrategy(role="button", name="Go"),
            ]
        )


def test_control_actions_require_a_target() -> None:
    with pytest.raises(ValidationError):
        Step(
            id="click_search",
            intent="click the search button",
            action=Click(),
            risk_level="REVERSIBLE_WRITE",
            # no target -- Click acts on a control, this must be rejected
        )
    # navigate and a bare wait are the only actions allowed without one:
    Step(
        id="open_search",
        intent="open the search page",
        action={"type": "navigate", "url_template": "{base_url}/members/search"},
        risk_level="SAFE_READ",
    )


def test_value_from_must_be_a_reference_not_a_literal() -> None:
    with pytest.raises(ValidationError):
        TypeText(value_from="12345")  # a literal, not "{{input.x}}" / "{{ctx.x}}"


def test_type_action_cannot_reference_an_undeclared_input() -> None:
    cap = _lookup_savings_balance()
    bad = _as_dict(cap)
    bad["steps"][1]["action"]["value_from"] = "{{input.does_not_exist}}"
    with pytest.raises(ValidationError):
        Capability.model_validate(bad)


def test_ctx_reference_needs_an_earlier_producer() -> None:
    cap = _lookup_savings_balance()
    bad = _as_dict(cap)
    # Reference ctx.txn_token before anything ever produces it.
    bad["steps"][1]["action"]["value_from"] = "{{ctx.txn_token}}"
    with pytest.raises(ValidationError):
        Capability.model_validate(bad)


def test_output_source_step_id_must_be_the_actual_producer() -> None:
    cap = _lookup_savings_balance()
    bad = _as_dict(cap)
    bad["outputs"][0]["source_step_id"] = "enter_member_id"  # wrong step
    with pytest.raises(ValidationError):
        Capability.model_validate(bad)


def test_duplicate_step_ids_are_rejected() -> None:
    cap = _lookup_savings_balance()
    bad = _as_dict(cap)
    bad["steps"][2]["id"] = bad["steps"][0]["id"]
    with pytest.raises(ValidationError):
        Capability.model_validate(bad)


def test_goal_cannot_contain_a_literal_sensitive_value() -> None:
    cap = _lookup_savings_balance()
    bad = _as_dict(cap)
    bad["provenance"]["goal"] = "look up member 12345 and read their current savings balance"
    with pytest.raises(ValidationError):
        Capability.model_validate(bad)


def test_summary_cannot_contain_a_literal_sensitive_value_either() -> None:
    """Regression: a compiler that templates provenance.goal but writes the
    raw literal into `summary` has fixed nothing, since summary is the
    field a calling agent actually reads to decide whether to invoke this
    capability."""
    cap = _lookup_savings_balance()
    bad = _as_dict(cap)
    bad["summary"] = "look up member 12345 and read their current savings balance"
    with pytest.raises(ValidationError):
        Capability.model_validate(bad)


def test_replay_result_enforces_one_payload_per_status() -> None:
    now = datetime.now(UTC)
    ok = ReplayResult(
        capability_id="acme_core.lookup_savings_balance",
        capability_version=1,
        run_id="replay_1",
        status="success",
        outputs={"savings_balance": Money(amount_minor=815000, currency="USD")},
        started_at=now,
        ended_at=now,
        evidence_dir="evidence/replay_1",
    )
    assert ok.status == "success"

    with pytest.raises(ValidationError):
        ReplayResult(
            capability_id="x",
            capability_version=1,
            run_id="replay_2",
            status="success",  # says success but attaches no outputs
            started_at=now,
            ended_at=now,
            evidence_dir="evidence/replay_2",
        )
