"""Regression coverage for two real bugs found while debugging a live crash
(`cua replay` on an artifact with no matching runtime_match, handoff left on
by default, nobody claimed it within policy.keep_alive.interval_s):

1. KeepAliveThread used to receive the CALLER's ControlBroker instance and
   call it from its own OS thread. sqlite3 connections are only usable from
   the thread that created them (check_same_thread defaults True) -- the
   first time the keepalive thread actually fired, it raised
   sqlite3.ProgrammingError from inside a background thread, mid-`cua
   replay`. Fixed by giving the thread a db_path and having it open its own
   connection inside run().

2. Separately, the class stored its stop Event as `self._stop`, which
   shadows threading.Thread's own private `_stop()` method (join()/
   is_alive() call it internally). Nothing here called .join() so it never
   surfaced -- but the fix below does call it, and would fail with a
   confusing `TypeError: 'Event' object is not callable` from deep inside
   the standard library if the shadowing regressed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from cua.escalation import ControlBroker, KeepAliveThread
from cua.safety import load_policy


def _fast_policy():
    base = load_policy()
    return base.model_copy(update={"keep_alive": base.keep_alive.model_copy(update={"interval_s": 0.1})})


def test_keepalive_thread_opens_its_own_connection_not_the_callers(tmp_path: Path, monkeypatch) -> None:
    """The exact crash: a broker created in the main thread, a
    KeepAliveThread that has to touch the SAME sqlite file from its own
    thread while the run is PAUSED. Before the fix, this raised
    sqlite3.ProgrammingError inside the background thread on its first
    tick; threading's default excepthook only prints that, it doesn't
    propagate to the main thread, which is exactly why the original crash
    was invisible until someone actually watched for it."""
    db_path = tmp_path / "control.db"
    main_thread_broker = ControlBroker(db_path)
    main_thread_broker.mark_stuck("run-under-test", "simulated stuck step")

    thread_errors: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", thread_errors.append)

    keepalive = KeepAliveThread(
        base_url="http://127.0.0.1:1",  # nothing needs to actually answer; ping failure is swallowed
        policy=_fast_policy(),
        db_path=main_thread_broker.db_path,
        run_id="run-under-test",
    )
    keepalive.start()
    time.sleep(0.45)  # several keep_alive.interval_s ticks while state stays PAUSED
    keepalive.stop()
    keepalive.join(timeout=2)  # regression check for bug #2, see module docstring

    assert not keepalive.is_alive(), "keepalive thread did not stop within the timeout"
    assert thread_errors == [], (
        f"keepalive thread raised on its own connection: {thread_errors[0].exc_value if thread_errors else None}"
    )


def test_keepalive_thread_stops_pinging_once_claimed(tmp_path: Path, monkeypatch) -> None:
    """Not just 'doesn't crash' -- still behaves correctly: once an operator
    claims the run (state moves off PAUSED), the keepalive thread must stop
    issuing pings, exactly as `if row is None or row.state != "PAUSED":
    continue` in keepalive.py says."""
    db_path = tmp_path / "control.db"
    broker = ControlBroker(db_path)
    broker.mark_stuck("run-under-test", "simulated stuck step")

    ping_count = {"n": 0}
    import httpx

    def fake_get(url, timeout=5):
        ping_count["n"] += 1
        raise httpx.ConnectError("no server listening, that's fine, we're only counting attempts")

    monkeypatch.setattr(httpx, "get", fake_get)

    keepalive = KeepAliveThread(
        base_url="http://127.0.0.1:1",
        policy=_fast_policy(),
        db_path=broker.db_path,
        run_id="run-under-test",
    )
    keepalive.start()
    time.sleep(0.25)  # let a couple of pings happen while PAUSED
    broker.claim("run-under-test", holder="operator")  # no longer PAUSED
    count_at_claim = ping_count["n"]
    time.sleep(0.35)  # give it more ticks it should now skip
    keepalive.stop()
    keepalive.join(timeout=2)

    assert count_at_claim > 0, "expected at least one ping while genuinely PAUSED"
    assert ping_count["n"] == count_at_claim, "kept pinging after the run was claimed"
