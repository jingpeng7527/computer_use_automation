"""ControlBroker: the SQLite-backed control-transfer state machine
(REPORT.md sec 5, 系统设计 sec 5.4).

    AUTOMATION --stuck--> PAUSED --claim--> HUMAN --release--> RESUMING --> AUTOMATION
                            ^                  |
                            +--- lease expiry -+

A SQLite file, not a service: the operator reaches the live session by
clicking the browser window already in front of them, not by proxying
through this broker, so the broker's entire job is holding one row saying
who is in control. Claims are conditional atomic UPDATEs, not
read-modify-write -- two operators claiming at once, only one wins (the
UPDATE's rowcount says which). Lease expiry is evaluated lazily, at
read/claim time, not by a background job: nothing has to be running for a
stale HUMAN lease to free up, which matters because everything else here
is single-process and synchronous.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path("runs/control.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS control (
    run_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('AUTOMATION','PAUSED','HUMAN','RESUMING')),
    holder TEXT,
    lease_expires_at REAL,
    reason TEXT,
    updated_at REAL NOT NULL
)
"""


@dataclass
class ControlRow:
    run_id: str
    state: str
    holder: str | None
    lease_expires_at: float | None
    reason: str | None
    updated_at: float


class ControlBroker:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit (isolation_level=None): each statement is its own
        # transaction, which is what makes the UPDATE...WHERE below atomic
        # without needing an explicit BEGIN/COMMIT around it.
        self._conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        self._conn.execute(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def mark_stuck(self, run_id: str, reason: str) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT INTO control(run_id, state, holder, lease_expires_at, reason, updated_at) "
            "VALUES (?, 'PAUSED', NULL, NULL, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "state='PAUSED', holder=NULL, lease_expires_at=NULL, reason=excluded.reason, "
            "updated_at=excluded.updated_at",
            (run_id, reason, now),
        )

    def claim(self, run_id: str, holder: str, lease_seconds: float = 300) -> bool:
        """Conditional atomic claim: succeeds only if the row is PAUSED, or
        HUMAN with an already-expired lease. Returns whether THIS call won."""
        now = time.time()
        cur = self._conn.execute(
            "UPDATE control SET state='HUMAN', holder=?, lease_expires_at=?, updated_at=? "
            "WHERE run_id=? AND (state='PAUSED' OR (state='HUMAN' AND lease_expires_at < ?))",
            (holder, now + lease_seconds, now, run_id, now),
        )
        return cur.rowcount > 0

    def renew(self, run_id: str, holder: str, lease_seconds: float = 300) -> bool:
        now = time.time()
        cur = self._conn.execute(
            "UPDATE control SET lease_expires_at=?, updated_at=? "
            "WHERE run_id=? AND state='HUMAN' AND holder=?",
            (now + lease_seconds, now, run_id, holder),
        )
        return cur.rowcount > 0

    def release(self, run_id: str, holder: str) -> bool:
        now = time.time()
        cur = self._conn.execute(
            "UPDATE control SET state='RESUMING', updated_at=? "
            "WHERE run_id=? AND state='HUMAN' AND holder=?",
            (now, run_id, holder),
        )
        return cur.rowcount > 0

    def mark_resumed(self, run_id: str) -> None:
        self._conn.execute(
            "UPDATE control SET state='AUTOMATION', updated_at=? WHERE run_id=?",
            (time.time(), run_id),
        )

    def get_state(self, run_id: str) -> ControlRow | None:
        row = self._conn.execute(
            "SELECT run_id, state, holder, lease_expires_at, reason, updated_at "
            "FROM control WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        control = ControlRow(*row)
        if (
            control.state == "HUMAN"
            and control.lease_expires_at is not None
            and control.lease_expires_at < time.time()
        ):
            # Lazily-observed expiry: report PAUSED without writing anything.
            # The next claim() call performs the actual state transition, in
            # the same conditional UPDATE that grants the new claim.
            return ControlRow(control.run_id, "PAUSED", None, None, control.reason, control.updated_at)
        return control

    def is_automations_turn(self, run_id: str) -> bool:
        row = self.get_state(run_id)
        return row is None or row.state == "AUTOMATION"
