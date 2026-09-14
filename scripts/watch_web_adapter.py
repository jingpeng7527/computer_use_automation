"""Manual dev tool, not part of the pytest suite: drives the REAL mock app
with the REAL WebAdapter, headed, so you can watch the browser click
through search -> results -> detail and see which locator layer resolved
each control. This is what pytest can't cover -- the pure-function tests
prove the matching logic in isolation; this proves it against an actual
browser and an actual running server.

Requires `cua serve-target` running in another terminal first:

    # terminal 1
    cua serve-target

    # terminal 2
    python scripts/watch_web_adapter.py
"""

from __future__ import annotations

from cua.schema import (
    Click,
    CssStrategy,
    LabelAnchorStrategy,
    Navigate,
    Read,
    RoleName,
    RoleNameStrategy,
    Target,
    TypeText,
)
from cua.surface import WebAdapter

BASE_URL = "http://127.0.0.1:8800"


def main() -> None:
    adapter = WebAdapter(headless=False)
    try:
        adapter.act(Navigate(url_template=""), value=f"{BASE_URL}/members/search")
        print("1. opened search page ->", adapter.location())

        member_box = Target(
            strategies=[LabelAnchorStrategy(label="Member Number:", relation="same_row_input")]
        )
        r = adapter.resolve(member_box)
        print(f"2. member id box resolved at layer {r.layer} (attempts: {[a.kind for a in r.attempts]})")
        adapter.act(TypeText(value_from="{{input.member_id}}"), resolution=r, value="12345")

        search_btn = Target(strategies=[RoleNameStrategy(role="button", name="Search")])
        r = adapter.resolve(search_btn)
        print(f"3. Search button resolved at layer {r.layer}")
        adapter.act(Click(), resolution=r)
        print("   ->", adapter.location())

        view_link = Target(strategies=[CssStrategy(selector=".action-view-detail")])
        r = adapter.resolve(view_link)
        print(f"4. View control resolved at layer {r.layer} (a <div onclick>, no role/name)")
        adapter.act(Click(), resolution=r)
        print("   ->", adapter.location())

        ok = adapter.wait_for(RoleName(role="heading", name="Member Detail"), timeout_ms=5000)
        print(f"5. waited for the detail heading: {'appeared' if ok else 'TIMED OUT'}")

        balance_field = Target(
            strategies=[LabelAnchorStrategy(label="Savings Balance", relation="next_cell")]
        )
        r = adapter.resolve(balance_field)
        value = adapter.act(Read(into="savings_balance"), resolution=r)
        print(f"6. read balance (layer {r.layer}): {value!r}")

        print("\nEND TO END OK -- browser stays open 5s so you can look")
        import time

        time.sleep(5)
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
