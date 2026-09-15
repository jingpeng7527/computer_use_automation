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
from pathlib import Path

import httpx

from cua.safety import Policy

from .broker import ControlBroker


class KeepAliveThread(threading.Thread):
    def __init__(self, *, base_url: str, policy: Policy, db_path: Path | str, run_id: str) -> None:
        super().__init__(daemon=True)
        self._url = base_url.rstrip("/") + policy.keep_alive.route
        self._interval_s = policy.keep_alive.interval_s
        # A db_path, not a ControlBroker instance: sqlite3 connections are
        # only usable from the thread that created them (check_same_thread
        # defaults True), so this thread opens its OWN connection to the
        # same file in run() below, rather than reaching across into the
        # caller's connection -- sharing that one would raise
        # sqlite3.ProgrammingError the first time this thread actually
        # calls it, which is exactly what happened before this fix.
        self._db_path = db_path
        self._run_id = run_id
        # Named _stop_event, not _stop: threading.Thread already has a
        # private ._stop() method of its own (join()/is_alive() call it
        # internally via _wait_for_tstate_lock()) -- an attribute literally
        # named `self._stop` silently shadows it. Nothing here called
        # .join() so it never surfaced, but the first caller that did would
        # get "TypeError: 'Event' object is not callable" out of the
        # standard library's own internals, nowhere near this file.
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        broker = ControlBroker(self._db_path)
        try:
            while not self._stop_event.wait(self._interval_s):
                row = broker.get_state(self._run_id)
                if row is None or row.state != "PAUSED":
                    continue  # only ping while paused and unclaimed
                try:
                    httpx.get(self._url, timeout=5)
                except httpx.HTTPError:
                    pass  # best-effort; a failed ping isn't itself a hard failure
        finally:
            broker.close()
