"""Turns `StepResult.locator_layer_hit` -- already recorded on every replay,
per step, since Phase F -- into a cross-run drift signal, per its own
docstring's promise (schema/result.py): "steady demotion from layer 1 to a
deeper layer means the app is changing underneath the artifact." Nothing
upstream changes; this only reads the `result.json` files replay already
writes to `evidence/`.

A single deeper hit on one run is not drift -- a locator can miss a layer
transiently (a slow paint, a load-order race) and still resolve fine next
time. What actually means "the artifact needs a new version or a tenant
override" is a PERSISTENT demotion: the last several runs of the same step,
in a row, all landing deeper than where that step used to resolve. That
distinction (`occasional` vs `drifting`) is the entire point of this
module -- a naive "did the layer ever change" check would fire on normal
noise and train whoever reads it to ignore the signal.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DriftStatus = Literal["stable", "occasional", "drifting"]


@dataclass
class LayerDriftReport:
    capability_id: str
    step_id: str
    # Chronological (by the run's own started_at), oldest first: (run_id, layer).
    history: list[tuple[str, int]]
    baseline_layer: int
    latest_layer: int
    status: DriftStatus


def aggregate_layer_drift(
    evidence_root: Path | str,
    *,
    persistence_window: int = 3,
) -> list[LayerDriftReport]:
    """Scans every `<evidence_root>/*/result.json`, groups
    `locator_layer_hit` by (capability_id, step_id) in the order those runs
    actually happened (`ReplayResult.started_at`), and classifies each
    group's trend.

    `persistence_window`: how many of the most recent runs must ALL be
    deeper than the baseline for this to count as `drifting` rather than
    `occasional`. Requires at least 2 runs to say anything but `stable`
    (nothing to compare a single run against)."""
    root = Path(evidence_root)
    # (capability_id, step_id) -> list of (started_at, run_id, layer)
    raw: dict[tuple[str, str], list[tuple[str, str, int]]] = defaultdict(list)

    for result_path in sorted(root.glob("*/result.json")):
        try:
            data = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        capability_id = data.get("capability_id")
        started_at = data.get("started_at")
        run_id = data.get("run_id")
        if not capability_id or not started_at or not run_id:
            continue
        for step in data.get("steps", []):
            layer = step.get("locator_layer_hit")
            if layer is None:
                continue
            raw[(capability_id, step["step_id"])].append((started_at, run_id, layer))

    reports: list[LayerDriftReport] = []
    for (capability_id, step_id), entries in raw.items():
        entries.sort(key=lambda e: e[0])  # chronological by started_at
        history = [(run_id, layer) for _started_at, run_id, layer in entries]
        baseline_layer = history[0][1]
        latest_layer = history[-1][1]

        if len(history) < 2:
            status: DriftStatus = "stable"
        else:
            window = history[-min(persistence_window, len(history)) :]
            if len(window) >= 2 and all(layer > baseline_layer for _run_id, layer in window):
                status = "drifting"
            elif latest_layer > baseline_layer:
                status = "occasional"
            else:
                status = "stable"

        reports.append(
            LayerDriftReport(
                capability_id=capability_id,
                step_id=step_id,
                history=history,
                baseline_layer=baseline_layer,
                latest_layer=latest_layer,
                status=status,
            )
        )

    reports.sort(key=lambda r: (r.capability_id, r.step_id))
    return reports


def render_drift_report(reports: list[LayerDriftReport]) -> str:
    if not reports:
        return "no locator_layer_hit data found across the scanned evidence."
    lines = []
    for r in reports:
        history_str = " -> ".join(f"{run_id}:L{layer}" for run_id, layer in r.history)
        marker = {"stable": "  ", "occasional": "? ", "drifting": "!!"}[r.status]
        lines.append(
            f"{marker} {r.capability_id} / {r.step_id}: baseline=L{r.baseline_layer} "
            f"latest=L{r.latest_layer} [{r.status}]  {history_str}"
        )
    return "\n".join(lines)
