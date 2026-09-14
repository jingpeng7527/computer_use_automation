"""Execution bounds -- shared by discovery and replay, enforced by the
executor, never accepted from the artifact or decided by the model
(系统设计 P5). An unbounded loop isn't only a cost problem: it's repeatedly
clicking a real back-office application. Hitting any bound is a terminal
condition -- stop, capture evidence, escalate -- never one that silently
keeps going.
"""

from __future__ import annotations

import time

from .policy import Policy


class BoundExceeded(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ExecutionGuard:
    def __init__(self, policy: Policy) -> None:
        self._bounds = policy.execution_bounds
        self._step_count = 0
        self._started_at = time.monotonic()
        self._last_observation: str | None = None
        self._stale_count = 0
        self._recovery_used = 0

    def check_step(self) -> None:
        """Call once per loop iteration / replay step. Raises if the step
        count or wall-clock ceiling is exceeded."""
        self._step_count += 1
        if self._step_count > self._bounds.max_steps:
            raise BoundExceeded(f"max_steps ({self._bounds.max_steps}) exceeded")
        elapsed = time.monotonic() - self._started_at
        if elapsed > self._bounds.wall_clock_ceiling_s:
            raise BoundExceeded(
                f"wall_clock_ceiling_s ({self._bounds.wall_clock_ceiling_s}) exceeded"
            )

    def check_progress(self, observation: str) -> None:
        """Call with a canonical string of the current observation. Raises
        if the same observation repeats too many times in a row -- the
        discovery loop is stuck, not making progress toward the goal."""
        if observation == self._last_observation:
            self._stale_count += 1
            if self._stale_count >= self._bounds.no_progress_max_consecutive_steps:
                raise BoundExceeded(f"no progress for {self._stale_count} consecutive steps")
        else:
            self._stale_count = 0
        self._last_observation = observation

    def use_recovery(self) -> bool:
        """Consumes one unit of the shared recovery budget. Returns False
        (and consumes nothing) once the budget is exhausted, so the caller
        can fail cleanly instead of retrying forever."""
        if self._recovery_used >= self._bounds.recovery_budget_per_run:
            return False
        self._recovery_used += 1
        return True
