"""Allowlist enforcement -- runs between the decision and the surface, so a
refused action is never handed to adapter.act() at all (REPORT.md sec 6).
Default posture is deny: an action type or destination not explicitly
listed is refused, regardless of what the artifact says.
"""

from __future__ import annotations

from dataclasses import dataclass

from cua.surface import Location

from .policy import Policy


@dataclass
class AllowlistDecision:
    allowed: bool
    reason: str = ""


def check_allowed(
    action_type: str,
    policy: Policy,
    *,
    current_location: Location | None = None,
    destination: str | None = None,
) -> AllowlistDecision:
    """Check one action against the allowlist.

    For a `navigate`, pass the fully-resolved destination URL (the caller
    has already interpolated `{base_url}` etc. -- this function checks a
    literal string, same as adapter.act()'s `value`). For everything else,
    pass the adapter's current `location()`; the action is understood to
    operate on the page already open.
    """
    if action_type not in policy.allowlist.action_types:
        return AllowlistDecision(False, f"action type {action_type!r} is not in the allowlist")

    full = destination
    if full is None and current_location is not None:
        full = f"{current_location.origin}{current_location.path}"
    if full is None:
        return AllowlistDecision(False, "no location to check the allowlist against")

    origin = next((o for o in policy.allowlist.origins if full.startswith(o)), None)
    if origin is None:
        return AllowlistDecision(False, f"{full!r} is outside the allowed origins")

    path = full[len(origin) :] or "/"
    if not any(path.startswith(prefix) for prefix in policy.allowlist.path_prefixes):
        return AllowlistDecision(False, f"path {path!r} is outside the allowed prefixes")

    return AllowlistDecision(True)
