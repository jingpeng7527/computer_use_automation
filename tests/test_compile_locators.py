"""_strategies_for / _nearest_label_to_the_left -- no browser, no LLM.

Regression coverage for a real bug: the compiler anchored a locator on
whatever non-interactive text happened to sit nearest to a control, with no
check that the text was a genuine static label rather than per-record data.
On the mock app's results page that meant a real member's name
("Dolores Ibarra") got written into a versioned, git-tracked capability
artifact as the "View" button's locator. Fixtures below mirror the two
real shapes from src/cua/target_app/templates/: a label/value pair
(detail.html, safe to anchor on) and a data-table row with several dynamic
cells (results.html, must NOT be anchored on).
"""

from __future__ import annotations

from cua.agent.compile import _nearest_label_to_the_left, _strategies_for
from cua.surface import BBox, InteractiveNode


def _node(node_id: str, x: float, y: float, w: float = 100, h: float = 20, **kw) -> InteractiveNode:
    return InteractiveNode(node_id=node_id, bbox=BBox(x=x, y=y, w=w, h=h), **kw)


# detail.html: <tr><td>Savings Balance</td><td>$8160.00 USD</td></tr> --
# exactly one static label to the left of the value cell being read.
LABEL_VALUE_ROW = [
    _node("label", x=0, y=200, text="Savings Balance"),
    _node("value", x=120, y=200, text="$8160.00 USD"),
]

# results.html: <tr><td>{{member_id}}</td><td>{{member.name}}</td>
#   <td><div onclick class="action-view-detail">View</div></td></tr> --
# two dynamic, per-record cells sit between nothing and the "View" control.
RESULTS_ROW = [
    _node("member_id_cell", x=0, y=100, text="12345"),
    _node("member_name_cell", x=80, y=100, text="Dolores Ibarra"),
    _node(
        "view_button",
        x=200,
        y=100,
        tag="div",
        text="View",
        attrs={"class": "action-view-detail"},
        interactive=True,
    ),
]


def test_label_value_row_anchors_on_the_one_static_label() -> None:
    value_node = LABEL_VALUE_ROW[1]
    anchor = _nearest_label_to_the_left(value_node, LABEL_VALUE_ROW)
    assert anchor is not None
    assert anchor.text == "Savings Balance"


def test_ambiguous_data_row_never_anchors_on_a_record_value() -> None:
    """The regression case: two dynamic sibling cells means there is no
    single "the label" for this row, so the compiler must not guess one --
    least of all a real member's name."""
    view_node = RESULTS_ROW[2]
    anchor = _nearest_label_to_the_left(view_node, RESULTS_ROW)
    assert anchor is None


def test_strategies_for_falls_through_to_css_not_a_name_leak() -> None:
    view_node = RESULTS_ROW[2]
    target = _strategies_for(view_node, RESULTS_ROW)
    kinds = [s.type for s in target.strategies]
    assert "label_anchor" not in kinds
    assert "css" in kinds
    # the whole point: nothing in the compiled target may contain the
    # record's own data.
    dumped = " ".join(str(s.model_dump()) for s in target.strategies)
    assert "Dolores Ibarra" not in dumped
    assert "12345" not in dumped
