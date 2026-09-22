"""ExecutionGuard, directly -- no discovery loop, no replay engine, no
browser. `reset_progress`/`use_discovery_handoff` had never been exercised
on their own before this file (ExecutionGuard itself had no direct test at
all; only exercised indirectly through replay()/run_discovery()), which is
how a reset that accidentally cleared the wrong counter, or a budget that
never actually bounded anything, could have shipped unnoticed.
"""

from __future__ import annotations

import pytest

from cua.safety import BoundExceeded, ExecutionGuard, load_policy


def _guard(**bounds_overrides) -> ExecutionGuard:
    policy = load_policy()
    bounds = policy.execution_bounds.model_copy(update=bounds_overrides)
    return ExecutionGuard(policy.model_copy(update={"execution_bounds": bounds}))


def test_reset_progress_clears_the_stale_count() -> None:
    guard = _guard(no_progress_max_consecutive_steps=3)
    guard.check_progress("same")
    guard.check_progress("same")  # stale_count now 1 -- one more identical call would raise

    guard.reset_progress("fresh")

    # Immediately after reset, the SAME "same" observation is a fresh streak
    # of one, not a continuation of the pre-reset one -- reset_progress
    # seeded _last_observation with "fresh", so "same" differs from it.
    guard.check_progress("same")
    guard.check_progress("same")  # would have raised pre-reset; doesn't now


def test_reset_progress_seeds_last_observation_not_none() -> None:
    """The point of taking an observation argument at all: seeding with the
    REAL post-handoff snapshot means the very next check_progress() call
    compares against what the human actually left on screen. A bare clear
    to None would instead treat any observation as trivially "different
    from nothing" for one free pass, silently hiding a stall that resumed
    into the exact same stuck state."""
    guard = _guard(no_progress_max_consecutive_steps=3)
    guard.reset_progress("still the same stuck page")

    guard.check_progress("still the same stuck page")  # stale_count -> 1, matches the seed
    guard.check_progress("still the same stuck page")  # stale_count -> 2
    with pytest.raises(BoundExceeded, match="no progress"):
        guard.check_progress("still the same stuck page")  # stale_count -> 3, raises


def test_use_discovery_handoff_is_bounded_and_consumes_one_unit() -> None:
    guard = _guard(max_discovery_handoffs=2)

    assert guard.use_discovery_handoff() is True
    assert guard.use_discovery_handoff() is True
    assert guard.use_discovery_handoff() is False  # budget exhausted -- consumes nothing further
    assert guard.use_discovery_handoff() is False


def test_use_discovery_handoff_default_is_exactly_one() -> None:
    guard = _guard()  # policy.yaml's own default: max_discovery_handoffs: 1
    assert guard.use_discovery_handoff() is True
    assert guard.use_discovery_handoff() is False


def test_reset_progress_never_touches_step_count_or_wall_clock() -> None:
    """Deliberately no reset path exists for _step_count/_started_at -- a
    discovery handoff clears confusion, never the run's own step or
    wall-clock budget. Nothing to call here; the assertion is that
    check_step()'s ceiling is unaffected by an intervening reset_progress()."""
    guard = _guard(max_steps=2)
    guard.check_step()  # 1
    guard.reset_progress("whatever")
    guard.check_step()  # 2
    with pytest.raises(BoundExceeded, match="max_steps"):
        guard.check_step()  # 3 -- exceeds max_steps=2 regardless of the reset in between
