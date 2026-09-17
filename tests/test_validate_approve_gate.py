"""`cua validate` / `cua approve --validation-run`, end to end against the
REAL target app and a REAL browser -- not just the pure schema-level checks
already covered elsewhere. Same reasoning as test_hardening_e2e.py and
test_recoverable_dialog_e2e.py: the mechanism this locks down (an artifact
whose discovery run needed a human handoff cannot be approved without first
proving, via a clean unattended replay, that it holds up on its own) is
exactly the kind of thing that looks right on paper and silently doesn't
fire for real.

Run with -s to see the narration:
    .venv/bin/python -m pytest tests/test_validate_approve_gate.py -v -s
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
import yaml
from playwright.sync_api import Error as PlaywrightError
from typer.testing import CliRunner

from cua.cli import app
from cua.safety import load_policy
from cua.schema import Capability, Navigate
from cua.target_app.app import app as target_app

ROOT = Path(__file__).parents[1]
ARTIFACT_PATH = ROOT / "artifacts" / "acme_core.lookup_savings_balance" / "1.json"

runner = CliRunner()


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _target_server() -> Iterator[str]:
    port = _unused_local_port()
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(target_app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, name="validate-approve-gate-server", daemon=True)
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


def _write_rebased_policy(origin: str) -> None:
    """cli.py's commands load policy.yaml from the CWD by default; this
    test chdirs into tmp_path (CliRunner convention in this repo -- see
    test_set_scope_preflight.py), so a policy.yaml matching THIS run's
    ephemeral origin has to exist there too, not just the artifact's own
    rebased app_profile. Loaded from the real project's config, by
    absolute path -- the CWD is already tmp_path by the time this runs."""
    base = load_policy(ROOT / "config" / "policy.yaml")
    policy = base.model_copy(update={"allowlist": base.allowlist.model_copy(update={"origins": [origin]})})
    Path("config").mkdir(parents=True, exist_ok=True)
    Path("config/policy.yaml").write_text(yaml.safe_dump(policy.model_dump(mode="json")))


def _draft_artifact_with_a_handoff(origin: str) -> Capability:
    """The real, approved v1 artifact, rebased onto this test's ephemeral
    server (same pattern as test_hardening_e2e.py), reset to draft with a
    discovery_handoffs count -- standing in for what agent/loop.py's real
    handoff path would have produced, without needing a full scripted
    discovery run just to get an artifact shaped this way."""
    saved = Capability.model_validate_json(ARTIFACT_PATH.read_text())
    first_step = saved.steps[0]
    assert isinstance(first_step.action, Navigate)
    rebased = first_step.model_copy(
        update={"action": first_step.action.model_copy(update={"url_template": f"{origin}/members/search"})}
    )
    app_profile = saved.app_profile.model_copy(update={"base_url": origin})
    provenance = saved.provenance.model_copy(update={"discovery_handoffs": 1})
    return saved.model_copy(
        update={
            "steps": [rebased, *saved.steps[1:]],
            "app_profile": app_profile,
            "provenance": provenance,
            "status": "draft",
        }
    )


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _skip_if_no_chromium():
    from cua.surface import WebAdapter

    try:
        WebAdapter(headless=True).close()
    except PlaywrightError as exc:
        pytest.skip(f"Chromium is unavailable for browser replay: {exc}")


def test_approve_refuses_a_handoff_artifact_without_validation_run(isolated_cwd) -> None:
    with _target_server() as origin:
        artifact_path = Path("input.json")
        artifact_path.write_text(_draft_artifact_with_a_handoff(origin).model_dump_json())

        result = runner.invoke(app, ["approve", "--artifact", str(artifact_path)])

        assert result.exit_code == 1
        assert "discovery handoff" in result.output
        assert "cua validate" in result.output
        assert json.loads(artifact_path.read_text())["status"] == "draft"  # untouched


def test_validate_then_approve_succeeds_on_a_clean_handoff_artifact(isolated_cwd) -> None:
    _skip_if_no_chromium()
    with _target_server() as origin:
        _write_rebased_policy(origin)
        artifact_path = Path("input.json")
        artifact_path.write_text(_draft_artifact_with_a_handoff(origin).model_dump_json())

        validate_result = runner.invoke(
            app, ["validate", "--artifact", str(artifact_path), "-p", "member_id=12345"]
        )
        print("\n--- cua validate output ---")
        print(validate_result.output)
        assert validate_result.exit_code == 0
        assert "validation PASSED" in validate_result.output

        # The command's own suggested next step, parsed back out -- proves
        # the printed run_id is the real one, not just a plausible string.
        run_id = validate_result.output.strip().splitlines()[-1].split()[-1]
        assert (Path("evidence") / run_id / "validation.json").exists()

        approve_result = runner.invoke(
            app, ["approve", "--artifact", str(artifact_path), "--validation-run", run_id]
        )
        print("\n--- cua approve output ---")
        print(approve_result.output)
        assert approve_result.exit_code == 0
        # cua approve saves to ArtifactStore's canonical path
        # (artifacts/<capability_id>/<version>.json), not back over the
        # input file it was pointed at -- same as every other approve.
        saved_path = Path("artifacts/acme_core.lookup_savings_balance/1.json")
        assert json.loads(saved_path.read_text())["status"] == "approved"


def test_approve_refuses_when_the_artifact_changed_since_validation(isolated_cwd) -> None:
    _skip_if_no_chromium()
    with _target_server() as origin:
        _write_rebased_policy(origin)
        artifact_path = Path("input.json")
        artifact_path.write_text(_draft_artifact_with_a_handoff(origin).model_dump_json())

        validate_result = runner.invoke(
            app, ["validate", "--artifact", str(artifact_path), "-p", "member_id=12345"]
        )
        assert validate_result.exit_code == 0
        run_id = validate_result.output.strip().splitlines()[-1].split()[-1]

        # Tamper: an edit after validation, before approval -- title only,
        # nothing that would change replay behaviour, which is exactly the
        # point of hashing the WHOLE artifact rather than a curated field list.
        tampered = json.loads(artifact_path.read_text())
        tampered["title"] = "Renamed after validation"
        artifact_path.write_text(json.dumps(tampered))

        approve_result = runner.invoke(
            app, ["approve", "--artifact", str(artifact_path), "--validation-run", run_id]
        )

        assert approve_result.exit_code == 1
        assert "has changed since" in approve_result.output
        assert json.loads(artifact_path.read_text())["status"] == "draft"
