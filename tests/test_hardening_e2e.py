"""Watch the hardening pass work against the REAL target app -- no LLM, no
mocked surface, same replay engine `cua replay` uses. This exists purely as
a hands-on demonstration of agent/hardening.py's run_hardening_pass():

    replay() with deliberately bad input
        -> it diverges from the happy path (asserted, not assumed)
        -> classify_divergence() reads the divergence MECHANICALLY off the
           real page (dialog? error text? neither?) -- no model involved
        -> build_runtime_match() wraps that classification into the schema
           object replay's engine.py later matches against

Run with -s so the print()s aren't swallowed by pytest's capture (see
test_replay_target_app_e2e.py's docstring / the earlier conversation about
this -- pytest hides stdout on a pass unless you pass -s):

    .venv/bin/python -m pytest tests/test_hardening_e2e.py -v -s
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Error as PlaywrightError

from cua.agent import build_runtime_match, run_hardening_pass
from cua.safety import load_policy
from cua.schema import Capability, Navigate
from cua.surface import WebAdapter
from cua.target_app.app import app

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
    thread = threading.Thread(target=server.run, name="hardening-e2e-test-server", daemon=True)
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


def _approved_artifact(origin: str, *, extra_query: str = "") -> Capability:
    """The already-approved v1 artifact (`cua approve` was run on it for
    real -- see README's demo path), rebased onto this test's ephemeral
    server. `extra_query` optionally tacks a query string onto the first
    navigate, the same trick cli.py's `--fault` flag uses, to reproduce the
    ?inject=500 hard-failure fixture target_app/app.py ships for exactly
    this purpose.

    AppProfile.base_url is rebased alongside the navigate step, for the
    same reason: it's the artifact's own declared scope (intersected with
    policy.yaml by safety/allowlist.py's check_allowed), and this test
    points the artifact at a different server than the one it declares --
    leaving base_url stale would make every step refuse as policy_blocked
    before ever reaching the app.
    """
    saved = Capability.model_validate_json(ARTIFACT_PATH.read_text())
    assert saved.status == "approved", "harden always runs against an approved base"
    first_step = saved.steps[0]
    assert isinstance(first_step.action, Navigate)
    url = f"{origin}/members/search"
    if extra_query:
        url += f"?{extra_query}"
    rebased = first_step.model_copy(update={"action": first_step.action.model_copy(update={"url_template": url})})
    app_profile = saved.app_profile.model_copy(update={"base_url": origin})
    return saved.model_copy(update={"steps": [rebased, *saved.steps[1:]], "app_profile": app_profile})


@pytest.fixture
def adapter() -> Iterator[WebAdapter]:
    try:
        a = WebAdapter(headless=True)
    except PlaywrightError as exc:
        pytest.skip(f"Chromium is unavailable for browser replay: {exc}")
    try:
        yield a
    finally:
        a.close()


def test_unknown_member_id_is_classified_as_a_business_outcome(adapter: WebAdapter) -> None:
    """A member id that's well-formed (5 digits, passes ParamSpec.pattern)
    but doesn't exist in target_app/data.py::MEMBERS. The app re-renders
    the SAME search page with "No member records match" -- a stable,
    fully-rendered page, no error signal anywhere on it. That mechanical
    absence of an error signal is the entire basis for business_outcome."""
    with _target_server() as origin:
        capability = _approved_artifact(origin)
        policy = load_policy().model_copy(
            update={"allowlist": load_policy().allowlist.model_copy(update={"origins": [origin]})}
        )

        category, candidate_texts, result = run_hardening_pass(
            capability, {"member_id": "99999"}, adapter, policy, run_id="hardening-e2e-not-found"
        )

        print("\n--- replay diverged at ---")
        print(f"step_id={result.failure.step_id!r} kind={result.failure.kind!r}")
        print(f"expected={result.failure.expected!r}")
        print(f"observed={result.failure.observed!r}")
        print("\n--- mechanical classification ---")
        print(f"category = {category!r}")
        print("\n--- every text node visible on the divergent page ---")
        for t in candidate_texts:
            print(f"  {t!r}")

        assert result.status == "failed"
        assert category == "business_outcome"
        assert any("No member records match" in t for t in candidate_texts)

        match = build_runtime_match(
            after_step=result.failure.step_id,
            category=category,
            detect_text="No member records match",
            outcome_code="MEMBER_NOT_FOUND",
        )
        print("\n--- the RuntimeMatch build_runtime_match() derived from all this ---")
        print(json.dumps(match.model_dump(mode="json"), indent=2))

        assert match.terminal is True  # business_outcome always terminal
        assert match.category == "business_outcome"
        assert match.result_code == "MEMBER_NOT_FOUND"


def test_injected_server_error_is_classified_as_a_hard_failure(adapter: WebAdapter) -> None:
    """target_app/app.py's ?inject=500 fixture: a genuine HTTP 500 whose
    body literally says "System Error 500". No dialog involved -- this is
    the plain error-signal path, and (after the ordering fix) it's checked
    BEFORE any dialog-shape check, so an error page wrapped in a dialog
    would still land here instead of being misread as "recoverable"."""
    with _target_server() as origin:
        capability = _approved_artifact(origin, extra_query="inject=500")
        policy = load_policy().model_copy(
            update={"allowlist": load_policy().allowlist.model_copy(update={"origins": [origin]})}
        )

        category, candidate_texts, result = run_hardening_pass(
            capability, {"member_id": "12345"}, adapter, policy, run_id="hardening-e2e-500"
        )

        print("\n--- replay diverged at ---")
        print(f"step_id={result.failure.step_id!r} kind={result.failure.kind!r}")
        print(f"observed={result.failure.observed!r}")
        print("\n--- mechanical classification ---")
        print(f"category = {category!r}")
        print("\n--- every text node visible on the divergent page ---")
        for t in candidate_texts:
            print(f"  {t!r}")

        assert result.status == "failed"
        assert category == "hard_failure"
        assert any("System Error" in t for t in candidate_texts)


def test_build_runtime_match_disambiguates_two_outcomes_at_the_same_step() -> None:
    """Regression for a real collision: id used to be a bare
    f"{after_step}_{category}", so two different business outcomes
    diverging at the SAME step (e.g. a frozen account and a
    permission-restricted one both fail step s4's target resolution the
    same way -- member 99001 vs 99002 against this project's own artifact)
    got the identical id and Capability's duplicate-id guard refused to
    save the second one. No browser needed: this is pure id generation."""
    frozen = build_runtime_match(
        after_step="s4", category="business_outcome", detect_text="Account Frozen", outcome_code="ACCOUNT_FROZEN"
    )
    restricted = build_runtime_match(
        after_step="s4",
        category="business_outcome",
        detect_text="Access Restricted",
        outcome_code="PERMISSION_DENIED",
    )
    assert frozen.id != restricted.id
