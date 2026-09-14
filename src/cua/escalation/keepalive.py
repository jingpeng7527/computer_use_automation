"""Keep-alive: while a run is PAUSED and unclaimed, ping a declared
SAFE_READ route so the target app's idle-session timer doesn't expire
during the handoff (系统设计 sec 5.6) -- routing an intervention, waiting
for an operator to pick it up, and letting them read the screen can
together take longer than a typical back-office idle timeout.

This is an out-of-band HTTP read (httpx), deliberately NOT done through
the paused browser page: touching the live page to "keep it alive" would
disturb the exact stuck state the operator needs to see when they take
over, which defeats the point of pausing in the first place.

Note: the mock target app in this project does not implement session
expiry, so this has no live scenario to demonstrate against here -- it is
built correctly and designed for the real requirement, not exercised end
to end. Documented as a cut, not silently skipped.
"""

from __future__ import annotations

import threading

import httpx

from cua.safety import Policy

from .broker import ControlBroker


class KeepAliveThread(threading.Thread):
    def __init__(self, *, base_url: str, policy: Policy, broker: ControlBroker, run_id: str) -> None:
        super().__init__(daemon=True)
        self._url = base_url.rstrip("/") + policy.keep_alive.route
        self._interval_s = policy.keep_alive.interval_s
        self._broker = broker
        self._run_id = run_id
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._interval_s):
            row = self._broker.get_state(self._run_id)
            if row is None or row.state != "PAUSED":
                continue  # only ping while paused and unclaimed
            try:
                httpx.get(self._url, timeout=5)
            except httpx.HTTPError:
                pass  # best-effort; a failed ping isn't itself a hard failure
