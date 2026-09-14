"""Locator resolution against hand-built snapshots -- no browser, matching
系统设计.md sec 1.7's test scope. Fixtures mirror the real markup in
src/cua/target_app/templates/: an unlabeled input anchored only by an
adjacent table cell's text, a real button, a <div onclick> control with a
stable class and no role, and a label-anchored read of a value cell.
"""

from __future__ import annotations

from cua.schema import (
    CssStrategy,
    LabelAnchorStrategy,
    RoleName,
    RoleNameStrategy,
    Target,
    TextContains,
    TextMatcher,
    UrlMatches,
)
from cua.surface import (
    BBox,
    InteractiveNode,
    Location,
    evaluate_condition,
    resolve_against_snapshot,
)


def _node(node_id: str, x: float, y: float, w: float = 100, h: float = 20, **kw) -> InteractiveNode:
    return InteractiveNode(node_id=node_id, bbox=BBox(x=x, y=y, w=w, h=h), **kw)


# One snapshot standing in for the whole search -> results -> detail flow,
# built the way WebAdapter.observe() would actually emit it.
SEARCH_PAGE = [
    _node("n0", x=0, y=100, text="Member Number:"),
    _node("n1", x=120, y=100, role="textbox", interactive=True),  # no accessible name -- legacy input
    _node("n2", x=0, y=160, role="button", name="Search", interactive=True),
]

RESULTS_PAGE = [
    _node(
        "n0",
        x=0,
        y=100,
        tag="div",
        text="View",
        attrs={"class": "action-view-detail"},
        interactive=True,
    ),
]

DETAIL_PAGE = [
    _node("n0", x=0, y=0, role="heading", name="Member Detail"),
    _node("n1", x=0, y=200, text="Savings Balance"),
    _node("n2", x=120, y=200, text="$8150.00 USD"),
]


def test_layer1_role_name_resolves_the_search_button() -> None:
    target = Target(strategies=[RoleNameStrategy(role="button", name="Search")])
    result = resolve_against_snapshot(target, SEARCH_PAGE)
    assert result.resolved
    assert result.layer == 1
    assert result.node.node_id == "n2"


def test_layer2_label_anchor_resolves_the_unlabeled_input() -> None:
    target = Target(
        strategies=[
            LabelAnchorStrategy(label="Member Number:", relation="same_row_input"),
        ]
    )
    result = resolve_against_snapshot(target, SEARCH_PAGE)
    assert result.resolved
    assert result.layer == 2
    assert result.node.node_id == "n1"


def test_layer3_css_resolves_the_div_onclick_control() -> None:
    target = Target(strategies=[CssStrategy(selector=".action-view-detail")])
    result = resolve_against_snapshot(target, RESULTS_PAGE)
    assert result.resolved
    assert result.layer == 3
    assert result.node.node_id == "n0"


def test_label_anchor_next_cell_reads_the_balance() -> None:
    target = Target(
        strategies=[LabelAnchorStrategy(label="Savings Balance", relation="next_cell")]
    )
    result = resolve_against_snapshot(target, DETAIL_PAGE)
    assert result.resolved
    assert result.node.text == "$8150.00 USD"


def test_ambiguous_layer_falls_through_to_the_next_one() -> None:
    # Two buttons named "Search" -- layer 1 is ambiguous (2 matches), so it
    # must be skipped, not resolved by picking either one.
    snapshot = [
        _node("n0", x=0, y=0, role="button", name="Search", interactive=True),
        _node("n1", x=200, y=0, role="button", name="Search", interactive=True),
        _node("n2", x=0, y=100, text="Member Number:"),
        _node("n3", x=120, y=100, role="textbox", interactive=True),
    ]
    target = Target(
        strategies=[
            RoleNameStrategy(role="button", name="Search"),
            LabelAnchorStrategy(label="Member Number:", relation="same_row_input"),
        ]
    )
    result = resolve_against_snapshot(target, snapshot)
    assert result.resolved
    assert result.layer == 2  # fell through past the ambiguous layer 1
    assert result.attempts[0].matched is False
    assert result.attempts[0].match_count == 2


def test_no_strategy_resolves_when_nothing_matches() -> None:
    # Stand-in for the img-only nav tab with no alt text (系统设计 sec 4.5):
    # no accessible name, no stable class, no nearby label to anchor off.
    snapshot = [_node("n0", x=0, y=0, tag="img", attrs={"class": "gen-8f2a1"})]
    target = Target(
        strategies=[
            RoleNameStrategy(role="button", name="Savings"),
            CssStrategy(selector=".tab-savings"),
        ]
    )
    result = resolve_against_snapshot(target, snapshot)
    assert not result.resolved
    assert result.node is None
    assert all(not a.matched for a in result.attempts)


def test_evaluate_condition_text_contains() -> None:
    snapshot = [_node("n0", x=0, y=0, text="No member records match")]
    condition = TextContains(text=TextMatcher(mode="contains", value="No member records match"))
    assert evaluate_condition(condition, snapshot) is True
    assert evaluate_condition(condition, DETAIL_PAGE) is False


def test_evaluate_condition_role_name_checkpoint() -> None:
    condition = RoleName(role="heading", name="Member Detail")
    assert evaluate_condition(condition, DETAIL_PAGE) is True
    assert evaluate_condition(condition, SEARCH_PAGE) is False


def test_evaluate_condition_url_matches() -> None:
    condition = UrlMatches(pattern="*/members/*/detail")
    here = Location(origin="http://127.0.0.1:8800", path="/members/12345/detail")
    elsewhere = Location(origin="http://127.0.0.1:8800", path="/members/search")
    assert evaluate_condition(condition, DETAIL_PAGE, here) is True
    assert evaluate_condition(condition, DETAIL_PAGE, elsewhere) is False
