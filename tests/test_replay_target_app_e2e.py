"""The real replay path: data.py -> FastAPI -> browser -> artifact -> result.

This is intentionally separate from the fake-surface acceptance tests.  It
proves that the approved artifact's locators still drive the actual target
application and that its typed output reflects the application's current
fixture data.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Error as PlaywrightError

from cua.replay import replay
from cua.safety import load_policy
from cua.schema import Capability, Money, Navigate
from cua.surface import WebAdapter
from cua.target_app.app import app
from cua.target_app.data import MEMBERS

ROOT = Path(__file__).parents[1]
ARTIFACT_PATH = ROOT / "artifacts" / "acme_core.lookup_savings_balance" / "2.json"


def _unused_local_port() -> int:
    """Reserve an ephemeral port long enough to learn its number.

    The test server is started immediately afterwards.  Using a temporary
    port prevents this test from silently exercising a developer's manually
    running target app on port 8800.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _target_server() -> Iterator[str]:
    port = _unused_local_port()
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, name="target-app-test-server", daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("the test target app did not start")

    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("the test target app did not stop")


def _artifact_for(origin: str) -> Capability:
    """Load the reviewed artifact, rebasing only its environment-specific URL.

    The saved artifact targets the demo server at :8800.  The test owns an
    ephemeral local server instead, so only the first navigation's origin is
    replaced; all targets, actions, checkpoints, outputs, and runtime rules
    remain those of the reviewed artifact.

    AppProfile.base_url is rebased too, for the same reason: the artifact's
    own declared scope (safety/allowlist.py's check_allowed intersects it
    with policy.yaml) is meant to track wherever this run is actually
    pointed, same as the navigate step above -- rebasing one without the
    other would make a real, ephemeral-port test server look like it's
    outside the capability's own declared scope, which is a correct refusal
    for a genuinely stale scope, not a bug to route around.
    """

    saved = Capability.model_validate_json(ARTIFACT_PATH.read_text())
    first_step = saved.steps[0]
    assert isinstance(first_step.action, Navigate)
    assert first_step.action.url_template == "http://127.0.0.1:8800/members/search"

    rebased_action = first_step.action.model_copy(update={"url_template": f"{origin}/members/search"})
    steps = [first_step.model_copy(update={"action": rebased_action}), *saved.steps[1:]]
    app_profile = saved.app_profile.model_copy(update={"base_url": origin})
    return saved.model_copy(update={"steps": steps, "app_profile": app_profile})


def _policy_for(origin: str):
    policy = load_policy(ROOT / "config" / "policy.yaml")
    allowlist = policy.allowlist.model_copy(update={"origins": [origin]})
    return policy.model_copy(update={"allowlist": allowlist})


def test_approved_artifact_replays_against_the_real_target_app_and_returns_current_data() -> None:
    """Changing ``MEMBERS`` changes the expected typed replay output.

    This differs from a fixed-value golden test: the fixture data is allowed
    to change, but the browser-rendered value and replay result must change
    with it.  A stale page template, broken locator, or disconnected replay
    output fails this assertion.
    """

    member = MEMBERS["12345"]
    expected_raw = f"${member['savings_balance_minor'] / 100:.2f} {member['currency']}"
    expected_output = Money(
        amount_minor=member["savings_balance_minor"], currency=member["currency"]
    )

    with _target_server() as origin:
        try:
            adapter = WebAdapter(headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium is unavailable for browser replay: {exc}")
        try:
            result = replay(
                _artifact_for(origin),
                {"member_id": "12345"},
                adapter,
                _policy_for(origin),
                run_id="target-app-e2e",
            )

            assert result.status == "success"
            assert result.outputs == {"savings_balance": expected_output}
            print(expected_output, result.outputs)
            assert expected_raw in adapter.page.locator("body").inner_text()
            assert [step.status for step in result.steps] == ["ok", "ok", "ok", "ok", "ok"]
            assert [step.locator_layer_hit for step in result.steps] == [None, 2, 1, 3, 2]
        finally:
            adapter.close()
