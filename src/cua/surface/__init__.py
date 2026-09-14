"""The surface abstraction (REPORT.md sec 4): SurfaceAdapter is the seam,
resolve.py is the pure logic above the browser, web.py is the one place
Playwright is imported.
"""

from __future__ import annotations

from .protocol import (
    BBox,
    InteractiveNode,
    Location,
    LocatorAttempt,
    ResolutionResult,
    SurfaceAdapter,
    SurfaceSnapshot,
)
from .resolve import evaluate_condition, resolve_against_snapshot
from .web import WebAdapter

__all__ = [
    "BBox",
    "InteractiveNode",
    "Location",
    "LocatorAttempt",
    "ResolutionResult",
    "SurfaceAdapter",
    "SurfaceSnapshot",
    "WebAdapter",
    "evaluate_condition",
    "resolve_against_snapshot",
]
