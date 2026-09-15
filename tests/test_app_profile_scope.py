"""AppProfile-declared scope narrows policy.yaml's allowlist, never widens
it -- no browser, no LLM.

effective scope = policy.yaml's global allowlist ∩ AppProfile's own
declared base_url/allowed_route_patterns. A capability with neither set
behaves exactly as before this existed (policy.yaml alone governs); one
that declares a narrower scope than policy.yaml is additionally
restricted to it, so a reviewer can see from the artifact alone where it
is meant to go, without cross-referencing policy.yaml.
"""

from __future__ import annotations

from pathlib import Path

from cua.safety import check_allowed, load_policy
from cua.schema import AppProfile
from cua.surface import Location

POLICY = load_policy(Path(__file__).parent.parent / "config" / "policy.yaml")
HERE_MEMBERS = Location(origin="http://127.0.0.1:8800", path="/members/search")
HERE_TENANT_B = Location(origin="http://127.0.0.1:8800", path="/tenant-b/members/search")


def test_no_app_profile_behaves_exactly_as_before() -> None:
    assert check_allowed("navigate", POLICY, current_location=HERE_MEMBERS).allowed


def test_app_profile_with_no_scope_declared_is_a_no_op() -> None:
    profile = AppProfile(product="acme_core", version="1.0")
    decision = check_allowed(
        "navigate", POLICY, current_location=HERE_MEMBERS, app_profile=profile
    )
    assert decision.allowed


def test_app_profile_route_patterns_narrow_within_the_same_origin() -> None:
    """Both locations are within policy.yaml's global allowlist (origin
    127.0.0.1:8800, path_prefixes=["/"]); the artifact's own declared
    scope is what tells them apart."""
    profile = AppProfile(
        product="acme_core", version="1.0", allowed_route_patterns=["/members/*"]
    )
    assert check_allowed("navigate", POLICY, current_location=HERE_MEMBERS, app_profile=profile).allowed
    denied = check_allowed("navigate", POLICY, current_location=HERE_TENANT_B, app_profile=profile)
    assert not denied.allowed
    assert "allowed_route_patterns" in denied.reason


def test_app_profile_cannot_widen_past_the_global_allowlist() -> None:
    """The artifact declares a base_url the GLOBAL policy doesn't allow at
    all -- the intersection is empty, so this stays refused. An artifact's
    own scope is a ceiling on itself, never a grant policy.yaml didn't
    already make."""
    profile = AppProfile(product="acme_core", version="1.0", base_url="http://evil.example.com")
    off_policy_scope_location = Location(origin="http://evil.example.com", path="/")
    decision = check_allowed(
        "navigate", POLICY, current_location=off_policy_scope_location, app_profile=profile
    )
    assert not decision.allowed
    assert "allowed origins" in decision.reason  # refused by the GLOBAL check, before app_profile is even consulted


def test_tenant_b_scope_correctly_excludes_the_base_tenants_routes() -> None:
    """The concrete case this exists for: two tenants share one origin and
    policy.yaml's allowlist, but each capability should stay inside its own
    tenant's routes even though the other tenant's routes are globally
    permitted too."""
    tenant_b_profile = AppProfile(
        product="acme_core", version="1.0", allowed_route_patterns=["/tenant-b/*"]
    )
    assert check_allowed(
        "navigate", POLICY, current_location=HERE_TENANT_B, app_profile=tenant_b_profile
    ).allowed
    assert not check_allowed(
        "navigate", POLICY, current_location=HERE_MEMBERS, app_profile=tenant_b_profile
    ).allowed
