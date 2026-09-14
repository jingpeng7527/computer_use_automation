"""Exercises the real FastAPI app in-process (no browser, no live server --
just calling it directly, like a fast fake request) and checks the
rendered page against data.py itself. This is the check that was missing:
test_locator_resolution.py's fixtures are hand-drawn and never touch this
app, so a change to data.py could drift silently until someone remembered
to run scripts/watch_web_adapter.py by hand.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cua.target_app.app import app
from cua.target_app.data import MEMBERS

client = TestClient(app)


def test_detail_page_shows_the_balance_from_data_py() -> None:
    member = MEMBERS["12345"]
    expected = f"${member['savings_balance_minor'] / 100:.2f} {member['currency']}"
    resp = client.get("/members/12345/detail")
    assert resp.status_code == 200
    assert expected in resp.text


def test_search_then_results_then_detail_happy_path() -> None:
    resp = client.post("/members/search", data={"member_id": "12345"}, follow_redirects=True)
    assert resp.status_code == 200
    assert "View" in resp.text  # landed on the results page

    resp = client.get("/members/12345/detail")
    assert "Member Detail" in resp.text
    assert MEMBERS["12345"]["name"] in resp.text


def test_unknown_member_redirects_back_to_search_without_crashing() -> None:
    resp = client.post("/members/search", data={"member_id": "99999"}, follow_redirects=True)
    assert resp.status_code == 200
    assert resp.url.path == "/members/search"
