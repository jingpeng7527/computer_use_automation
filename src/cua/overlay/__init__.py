"""Multi-tenant overlay resolution (assignment 3.7 / REPORT.md sec 4): turns
a base Capability plus a tenant Overlay into a concrete, replayable
Capability. The overlay's own data shape lives in schema/overlay.py; this
package is the logic that actually applies one.
"""

from __future__ import annotations

from .resolver import apply_overlay

__all__ = ["apply_overlay"]
