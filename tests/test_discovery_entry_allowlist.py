"""Discovery's entry navigation must go through the same allowlist gate
every model-chosen action does -- regression test for a real gap where the
CLI's own --target argument bypassed check_allowed() entirely, on the
reasoning that "it's not a model decision so it doesn't need gating." A
system-boundary input (a typo or a malicious --target) is exactly what the
allowlist exists to catch, regardless of who supplied it.
"""

from __future__ import annotations

import pytest

from cua.agent import run_discovery
from cua.safety import load_policy

POLICY = load_policy()


class _NeverCalledProvider:
    def decide(self, messages, tools):
        raise AssertionError("the model must never be consulted for a target refused up front")


class _NeverCalledAdapter:
    """Every method raises -- proves the refusal happens before the
    adapter is touched at all, not merely before a successful navigation."""

    def observe(self):
        raise AssertionError("must not touch the adapter before the allowlist check")

    def resolve(self, target):
        raise AssertionError("unused")

    def act(self, action, resolution=None, value=None):
        raise AssertionError("must not navigate to a refused target")

    def wait_for(self, condition, timeout_ms):
        raise AssertionError("unused")

    def location(self):
        raise AssertionError("unused")

    def screenshot(self, path):
        raise AssertionError("unused")


def test_entry_navigation_off_the_allowlist_is_refused_before_any_action() -> None:
    with pytest.raises(ValueError, match="refused"):
        run_discovery(
            "steal something",
            "http://evil.example.com/steal",
            _NeverCalledAdapter(),
            _NeverCalledProvider(),
            POLICY,
        )
