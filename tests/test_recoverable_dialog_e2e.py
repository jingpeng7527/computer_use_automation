"""The third and last divergence category, proved live: business_outcome
and hard_failure both already have real browser-driven e2e coverage
(test_hardening_e2e.py); recoverable/dismiss_dialog previously only had a
synthetic-snapshot unit test (test_error_classification.py's
`_node(role="dialog", ...)`), never a real page a real browser actually had
to dismiss something on.

target_app/app.py's `?interstitial=1` fixture renders a genuine "Session
Warning" dialog on the FIRST hit only (then never again this process) --
this test attaches a recoverable RuntimeMatch to the entry navigate step,
purely in-memory (the real, approved artifact on disk is never touched),
and watches replay() actually click through it: dismiss_dialog resolves and
clicks "Continue" on the real DOM, the step retries, and this time the
server has moved on, so the checkpoint holds and the rest of the flow
completes normally.

Run with -s to see the narration:
    .venv/bin/python -m pytest tests/test_recoverable_dialog_e2e.py -v -s
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
from cua.schema import (
    Capability,
    Navigate,
    RecoveryAction,
    RoleName,
    RuntimeMatch,
    TextContains,
    TextMatcher,
)
from cua.surface import WebAdapter
from cua.target_app.app import app
from cua.target_app.data import MEMBERS

ROOT = Path(__file__).parents[1]
ARTIFACT_PATH = ROOT / "artifacts" / "acme_core.lookup_savings_balance" / "1.json"


def _unused_local_port() -> int:
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
    thread = threading.Thread(target=server.run, name="recoverable-dialog-e2e-server", daemon=True)
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


def _capability_with_dialog_recovery(origin: str) -> Capability:
    """The real, approved v1 artifact, rebased onto this test's ephemeral
    server -- SAME pattern test_hardening_e2e.py uses (origin rebase +
    extra_query on the first navigate). The recoverable RuntimeMatch is
    added purely for this in-memory copy; the file on disk is never
    written to, so this can never regress the actual approved artifact."""
    saved = Capability.model_validate_json(ARTIFACT_PATH.read_text())
    assert saved.status == "approved"
    first_step = saved.steps[0]
    assert isinstance(first_step.action, Navigate)
    url = f"{origin}/members/search?interstitial=1"
    rebased = first_step.model_copy(update={"action": first_step.action.model_copy(update={"url_template": url})})
    # The artifact's own declared scope narrows the allowlist too (see
    # safety.check_app_profile_scope) -- leaving base_url pointed at the
    # real demo server would make every step refuse as policy_blocked
    # before ever reaching this test's ephemeral one.
    app_profile = saved.app_profile.model_copy(update={"base_url": origin})

    dialog_match = RuntimeMatch(
        id="s0_session_warning",
        category="recoverable",
        terminal=False,
        detect=RoleName(role="dialog", name="Session Warning"),
        after_step="s0",
        recovery=RecoveryAction(do="dismiss_dialog", target_role="link", target_name="Continue"),
        max_retries=2,
    )
    return saved.model_copy(
        update={
            "steps": [rebased, *saved.steps[1:]],
            "runtime_matches": [dialog_match],
            "app_profile": app_profile,
        }
    )


def _capability_with_slow_load_recovery(origin: str) -> Capability:
    """Same rebasing pattern as _capability_with_dialog_recovery, but for
    target_app's `?slow=1` fixture: a "Loading, please wait" page with
    nothing to click on it. `recovery.do="wait"` does no extra action --
    _apply_match falls straight through to retrying the step -- which is
    the right shape here, since the divergence clears on its own (the
    server has moved on by the second hit), not because anything was
    dismissed."""
    saved = Capability.model_validate_json(ARTIFACT_PATH.read_text())
    assert saved.status == "approved"
    first_step = saved.steps[0]
    assert isinstance(first_step.action, Navigate)
    url = f"{origin}/members/search?slow=1"
    rebased = first_step.model_copy(update={"action": first_step.action.model_copy(update={"url_template": url})})
    app_profile = saved.app_profile.model_copy(update={"base_url": origin})

    slow_load_match = RuntimeMatch(
        id="s0_slow_load",
        category="recoverable",
        terminal=False,
        detect=TextContains(text=TextMatcher(mode="contains", value="Loading, please wait")),
        after_step="s0",
        recovery=RecoveryAction(do="wait"),
        max_retries=2,
    )
    return saved.model_copy(
        update={
            "steps": [rebased, *saved.steps[1:]],
            "runtime_matches": [slow_load_match],
            "app_profile": app_profile,
        }
    )


def _policy_for(origin: str):
    policy = load_policy()
    allowlist = policy.allowlist.model_copy(update={"origins": [origin]})
    return policy.model_copy(update={"allowlist": allowlist})


def test_dismiss_dialog_recovers_from_a_real_session_warning_interstitial() -> None:
    with _target_server() as origin:
        try:
            adapter = WebAdapter(headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium is unavailable for browser replay: {exc}")
        try:
            capability = _capability_with_dialog_recovery(origin)
            result = replay(
                capability,
                {"member_id": "12345"},
                adapter,
                _policy_for(origin),
                run_id="recoverable-dialog-e2e",
            )

            print("\n--- per-step trace ---")
            for s in result.steps:
                print(f"  {s.step_id}: status={s.status!r} recoveries_applied={s.recoveries_applied}")
            print(f"\n--- final status: {result.status} ---")
            if result.status == "success":
                print(f"outputs: {result.outputs}")

            s0 = result.steps[0]
            assert s0.step_id == "s0"
            assert s0.status == "recovered", (
                "expected the entry step to show a real recovery in its trace, "
                f"got status={s0.status!r}"
            )
            assert s0.recoveries_applied == ["s0_session_warning"]

            assert result.status == "success", f"expected the flow to complete after recovery, got {result.status}"
            assert result.outputs is not None
            expected = MEMBERS["12345"]["savings_balance_minor"]
            assert result.outputs["savings_balance"].amount_minor == expected  # type: ignore[union-attr]
        finally:
            adapter.close()


def test_wait_recovers_from_a_real_slow_load() -> None:
    with _target_server() as origin:
        try:
            adapter = WebAdapter(headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium is unavailable for browser replay: {exc}")
        try:
            capability = _capability_with_slow_load_recovery(origin)
            result = replay(
                capability,
                {"member_id": "12345"},
                adapter,
                _policy_for(origin),
                run_id="recoverable-slow-load-e2e",
            )

            print("\n--- per-step trace ---")
            for s in result.steps:
                print(f"  {s.step_id}: status={s.status!r} recoveries_applied={s.recoveries_applied}")
            print(f"\n--- final status: {result.status} ---")
            if result.status == "success":
                print(f"outputs: {result.outputs}")

            s0 = result.steps[0]
            assert s0.step_id == "s0"
            assert s0.status == "recovered", (
                "expected the entry step to show a real recovery in its trace, "
                f"got status={s0.status!r}"
            )
            assert s0.recoveries_applied == ["s0_slow_load"]

            assert result.status == "success", f"expected the flow to complete after recovery, got {result.status}"
            assert result.outputs is not None
            expected = MEMBERS["12345"]["savings_balance_minor"]
            assert result.outputs["savings_balance"].amount_minor == expected  # type: ignore[union-attr]
        finally:
            adapter.close()
