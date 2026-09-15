"""`cua set-scope` used to be fail-safe but not fail-loud: it would happily
save a scope that already excludes one of the capability's own literal
Navigate targets, and the mistake would only surface later, mid-replay, as
a `policy_blocked` failure on whichever step hit it first. This is the
regression test for the fix: the command now checks the capability's own
steps against the scope it's about to save, and refuses (unless --force)
if any literal Navigate target would already be excluded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from cua.cli import app
from cua.schema import (
    AppProfile,
    Capability,
    Checkpoint,
    Navigate,
    Provenance,
    RoleName,
    Step,
)

runner = CliRunner()


def _capability_navigating_to_members_search() -> Capability:
    return Capability(
        capability_id="test.set_scope_preflight",
        status="approved",
        title="Set-scope preflight test",
        summary="A minimal one-step capability whose only step navigates to /members/search.",
        app_profile=AppProfile(product="test", version="0"),
        steps=[
            Step(
                id="s0",
                intent="navigate to the search page",
                action=Navigate(url_template="http://127.0.0.1:8800/members/search"),
                risk_level="SAFE_READ",
            )
        ],
        success_condition=Checkpoint(
            description="unreachable in this test", all_of=[RoleName(role="heading", name="Member Detail")]
        ),
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model="test",
            goal="test",
            discovery_run_id="test",
            transcript_sha256="0" * 64,
        ),
    )


def test_set_scope_refuses_a_pattern_that_would_already_exclude_the_entry_step(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact_path = Path("input.json")
    artifact_path.write_text(_capability_navigating_to_members_search().model_dump_json())

    result = runner.invoke(
        app,
        ["set-scope", "--artifact", str(artifact_path), "--allowed-route-pattern", "/tenant-b/*"],
    )

    assert result.exit_code == 1
    assert "already excludes" in result.output
    assert "s0" in result.output
    assert "refusing to save" in result.output
    # Nothing should have been written -- the mistake is caught before
    # the save, not discovered later mid-replay.
    assert not Path("artifacts").exists()


def test_set_scope_with_force_saves_anyway_but_still_warns(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    artifact_path = Path("input.json")
    artifact_path.write_text(_capability_navigating_to_members_search().model_dump_json())

    result = runner.invoke(
        app,
        [
            "set-scope",
            "--artifact",
            str(artifact_path),
            "--allowed-route-pattern",
            "/tenant-b/*",
            "--force",
        ],
    )

    assert result.exit_code == 0
    assert "already excludes" in result.output
    assert "--force given: saving anyway" in result.output
    assert "scope set:" in result.output

    saved = json.loads(Path("artifacts/test.set_scope_preflight/1.json").read_text())
    assert saved["app_profile"]["allowed_route_patterns"] == ["/tenant-b/*"]
    assert saved["status"] == "draft"


def test_set_scope_with_a_consistent_pattern_saves_without_any_warning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    artifact_path = Path("input.json")
    artifact_path.write_text(_capability_navigating_to_members_search().model_dump_json())

    result = runner.invoke(
        app,
        ["set-scope", "--artifact", str(artifact_path), "--allowed-route-pattern", "/members/*"],
    )

    assert result.exit_code == 0
    assert "already excludes" not in result.output
    assert "scope set:" in result.output


def test_set_scope_cannot_statically_check_a_templated_navigate_url(tmp_path: Path, monkeypatch) -> None:
    """A url_template containing {{...}} isn't a literal this command can
    resolve -- it must be skipped, not falsely flagged (or falsely
    cleared)."""
    monkeypatch.chdir(tmp_path)
    capability = _capability_navigating_to_members_search()
    templated = capability.model_copy(
        update={
            "steps": [
                capability.steps[0].model_copy(
                    update={"action": Navigate(url_template="http://127.0.0.1:8800/{{input.branch}}/search")}
                )
            ]
        }
    )
    artifact_path = Path("input.json")
    artifact_path.write_text(templated.model_dump_json())

    result = runner.invoke(
        app,
        ["set-scope", "--artifact", str(artifact_path), "--allowed-route-pattern", "/tenant-b/*"],
    )

    assert result.exit_code == 0
    assert "already excludes" not in result.output
