"""Allowlist enforcement -- runs between the decision and the surface, so a
refused action is never handed to adapter.act() at all (REPORT.md sec 6).
Default posture is deny: an action type or destination not explicitly
listed is refused, regardless of what the artifact says.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cua.surface import Location

from .policy import Policy

if TYPE_CHECKING:
    from cua.schema import AppProfile


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
    app_profile: AppProfile | None = None,
) -> AllowlistDecision:
    """Check one action against the allowlist.

    For a `navigate`, pass the fully-resolved destination URL (the caller
    has already interpolated `{base_url}` etc. -- this function checks a
    literal string, same as adapter.act()'s `value`). For everything else,
    pass the adapter's current `location()`; the action is understood to
    operate on the page already open.

    `app_profile`, when given, is an ADDITIONAL narrowing, never a wider
    grant: the effective scope is policy.yaml's own allowlist intersected
    with whatever the capability declares for itself
    (base_url / allowed_route_patterns). A capability with neither set is
    scoped by policy.yaml alone, exactly as before this parameter existed.
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

    if app_profile is not None:
        if app_profile.base_url and not full.startswith(app_profile.base_url):
            return AllowlistDecision(
                False, f"{full!r} is outside this capability's own declared base_url {app_profile.base_url!r}"
            )
        if app_profile.allowed_route_patterns and not any(
            fnmatch.fnmatch(path, pattern) for pattern in app_profile.allowed_route_patterns
        ):
            return AllowlistDecision(
                False,
                f"path {path!r} is outside this capability's own declared "
                f"allowed_route_patterns {app_profile.allowed_route_patterns!r}",
            )

    return AllowlistDecision(True)
