"""aggregate_layer_drift(): turns the locator_layer_hit every replay already
records per step into a cross-run trend. No browser, no LLM -- just
synthetic result.json files under tmp_path, matching the real shape
ReplayResult.model_dump(mode="json") produces.

The one thing worth getting right here is the distinction the docstring in
observability/drift.py insists on: a single deeper hit is noise
(`occasional`), not the same thing as several IN A ROW (`drifting`) -- a
naive "did it ever change" check would fire on both identically and teach
whoever reads the report to ignore it.
"""

from __future__ import annotations

import json
from pathlib import Path

from cua.observability import aggregate_layer_drift, render_drift_report


def _write_result(
    root: Path,
    run_id: str,
    started_at: str,
    capability_id: str,
    step_layers: dict[str, int | None],
) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    steps = [
        {
            "step_id": step_id,
            "intent": "test",
            "status": "ok",
            "locator_layer_hit": layer,
            "recoveries_applied": [],
            "duration_ms": 1,
            "extracted": {},
        }
        for step_id, layer in step_layers.items()
    ]
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "capability_id": capability_id,
                "capability_version": 1,
                "run_id": run_id,
                "status": "success",
                "outputs": {},
                "outcome": None,
                "failure": None,
                "escalation": None,
                "steps": steps,
                "started_at": started_at,
                "ended_at": started_at,
                "evidence_dir": str(run_dir),
                "redactions": [],
            }
        )
    )


def test_a_single_run_is_stable_regardless_of_which_layer_it_hit(tmp_path: Path) -> None:
    _write_result(tmp_path, "r1", "2026-01-01T00:00:00Z", "cap.a", {"s0": 3})
    reports = aggregate_layer_drift(tmp_path)
    assert len(reports) == 1
    assert reports[0].status == "stable"
    assert reports[0].baseline_layer == 3
    assert reports[0].latest_layer == 3


def test_a_lone_deeper_hit_surrounded_by_baseline_hits_is_occasional_not_drifting(tmp_path: Path) -> None:
    _write_result(tmp_path, "r1", "2026-01-01T00:00:00Z", "cap.a", {"s0": 1})
    _write_result(tmp_path, "r2", "2026-01-02T00:00:00Z", "cap.a", {"s0": 2})  # one-off blip
    _write_result(tmp_path, "r3", "2026-01-03T00:00:00Z", "cap.a", {"s0": 1})  # back to baseline
    reports = aggregate_layer_drift(tmp_path)
    assert reports[0].status == "stable"  # latest == baseline, the blip already recovered
    assert reports[0].history == [("r1", 1), ("r2", 2), ("r3", 1)]


def test_the_latest_run_alone_being_deeper_is_occasional(tmp_path: Path) -> None:
    _write_result(tmp_path, "r1", "2026-01-01T00:00:00Z", "cap.a", {"s0": 1})
    _write_result(tmp_path, "r2", "2026-01-02T00:00:00Z", "cap.a", {"s0": 1})
    _write_result(tmp_path, "r3", "2026-01-03T00:00:00Z", "cap.a", {"s0": 2})  # just happened
    reports = aggregate_layer_drift(tmp_path, persistence_window=3)
    assert reports[0].status == "occasional"


def test_several_deeper_hits_in_a_row_is_drifting(tmp_path: Path) -> None:
    _write_result(tmp_path, "r1", "2026-01-01T00:00:00Z", "cap.a", {"s0": 1})
    _write_result(tmp_path, "r2", "2026-01-02T00:00:00Z", "cap.a", {"s0": 1})
    _write_result(tmp_path, "r3", "2026-01-03T00:00:00Z", "cap.a", {"s0": 2})
    _write_result(tmp_path, "r4", "2026-01-04T00:00:00Z", "cap.a", {"s0": 3})
    _write_result(tmp_path, "r5", "2026-01-05T00:00:00Z", "cap.a", {"s0": 3})
    reports = aggregate_layer_drift(tmp_path, persistence_window=3)
    assert reports[0].status == "drifting"
    assert reports[0].baseline_layer == 1
    assert reports[0].latest_layer == 3
    assert reports[0].history == [("r1", 1), ("r2", 1), ("r3", 2), ("r4", 3), ("r5", 3)]


def test_history_is_ordered_by_started_at_not_by_directory_or_run_id_string_order(tmp_path: Path) -> None:
    # "r10" would sort before "r2" as a bare string -- started_at must win.
    _write_result(tmp_path, "r10", "2026-01-02T00:00:00Z", "cap.a", {"s0": 2})
    _write_result(tmp_path, "r2", "2026-01-01T00:00:00Z", "cap.a", {"s0": 1})
    reports = aggregate_layer_drift(tmp_path)
    assert reports[0].history == [("r2", 1), ("r10", 2)]


def test_steps_with_no_locator_are_excluded_not_treated_as_layer_zero(tmp_path: Path) -> None:
    _write_result(tmp_path, "r1", "2026-01-01T00:00:00Z", "cap.a", {"s0": None, "s1": 1})
    reports = aggregate_layer_drift(tmp_path)
    assert [r.step_id for r in reports] == ["s1"]


def test_multiple_capabilities_and_steps_are_tracked_independently(tmp_path: Path) -> None:
    _write_result(tmp_path, "r1", "2026-01-01T00:00:00Z", "cap.a", {"s0": 1, "s1": 2})
    _write_result(tmp_path, "r2", "2026-01-02T00:00:00Z", "cap.b", {"s0": 1})
    reports = aggregate_layer_drift(tmp_path)
    keys = {(r.capability_id, r.step_id) for r in reports}
    assert keys == {("cap.a", "s0"), ("cap.a", "s1"), ("cap.b", "s0")}


def test_render_drift_report_flags_drifting_rows_and_handles_the_empty_case() -> None:
    assert "no locator_layer_hit data" in render_drift_report([])

    from cua.observability.drift import LayerDriftReport

    report = LayerDriftReport(
        capability_id="cap.a", step_id="s0", history=[("r1", 1), ("r2", 3)], baseline_layer=1,
        latest_layer=3, status="drifting",
    )
    rendered = render_drift_report([report])
    assert "cap.a / s0" in rendered
    assert "drifting" in rendered
    assert "!!" in rendered
