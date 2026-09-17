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
    updated_at REAL NOT NULL,
    claimed_at REAL,
    release_note TEXT,
    phase TEXT NOT NULL DEFAULT 'replay' CHECK(phase IN ('replay','discovery')),
    resolution TEXT CHECK(resolution IS NULL OR resolution IN ('cleared_obstacle','workflow_advanced'))
)
"""

# Columns added after the table already shipped: CREATE TABLE IF NOT EXISTS
# does nothing for a db_path that already exists on disk with the OLD
# column set, so a lightweight migration is required -- this project's own
# runs/control.db (gitignored, local state) predates `phase`/`resolution`.
_MIGRATIONS: list[str] = [
    "ALTER TABLE control ADD COLUMN phase TEXT NOT NULL DEFAULT 'replay'",
    "ALTER TABLE control ADD COLUMN resolution TEXT",
]


@dataclass
class ControlRow:
    run_id: str
    state: str
    holder: str | None
    lease_expires_at: float | None
    reason: str | None
    updated_at: float
    claimed_at: float | None = None
    release_note: str | None = None
    phase: str = "replay"
    resolution: str | None = None


class ControlBroker:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit (isolation_level=None): each statement is its own
        # transaction, which is what makes the UPDATE...WHERE below atomic
        # without needing an explicit BEGIN/COMMIT around it.
        self._conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        self._conn.execute(_SCHEMA)
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(control)")}
        if "phase" not in existing_columns:
            self._conn.execute(_MIGRATIONS[0])
        if "resolution" not in existing_columns:
            self._conn.execute(_MIGRATIONS[1])

    def close(self) -> None:
        self._conn.close()

    def mark_stuck(self, run_id: str, reason: str, *, phase: str = "replay") -> None:
        """`phase` names which side of the system raised this intervention
        -- written here, not accepted later from whoever calls release(),
        because the caller of `ops release` is exactly who a phase-mismatch
        check has to distrust. `release()` reads it back off the row to
        decide whether `resolution` is required, optional or forbidden."""
        if phase not in ("replay", "discovery"):
            raise ValueError(f"phase must be 'replay' or 'discovery', got {phase!r}")
        now = time.time()
        self._conn.execute(
            "INSERT INTO control(run_id, state, holder, lease_expires_at, reason, updated_at, "
            "claimed_at, release_note, phase, resolution) "
            "VALUES (?, 'PAUSED', NULL, NULL, ?, ?, NULL, NULL, ?, NULL) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "state='PAUSED', holder=NULL, lease_expires_at=NULL, reason=excluded.reason, "
            "updated_at=excluded.updated_at, claimed_at=NULL, release_note=NULL, "
            "phase=excluded.phase, resolution=NULL",
            (run_id, reason, now, phase),
        )

    def claim(self, run_id: str, holder: str, lease_seconds: float = 300) -> bool:
        """Conditional atomic claim: succeeds only if the row is PAUSED, or
        HUMAN with an already-expired lease. Returns whether THIS call won."""
        now = time.time()
        cur = self._conn.execute(
            "UPDATE control SET state='HUMAN', holder=?, lease_expires_at=?, updated_at=?, claimed_at=? "
            "WHERE run_id=? AND (state='PAUSED' OR (state='HUMAN' AND lease_expires_at < ?))",
            (holder, now + lease_seconds, now, now, run_id, now),
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

    def release(
        self, run_id: str, holder: str, note: str | None = None, *, resolution: str | None = None
    ) -> bool:
        """Phase-aware, enforced HERE rather than trusted to whichever CLI
        command calls this: a replay-phase intervention takes no
        `resolution` at all (find_resume_point derives the resume point
        mechanically, by re-checking the actual page -- there is nothing
        for a human to declare); a discovery-phase intervention REQUIRES
        one of exactly two values, because discovery has no compiled
        checkpoint yet to derive a resume point from, so the only honest
        source of "what happened" is a structured choice by whoever holds
        the lease -- never free text, and never skippable.

            cleared_obstacle  -- a transient UI obstacle is gone; hand
                                 control back to the LLM to observe and
                                 keep deciding.
            workflow_advanced -- the operator did some or all of the
                                 actual task by hand. Whatever the LLM
                                 recorded before this point cannot become
                                 an artifact: the steps that mattered were
                                 never logged.

        Raises ValueError on a phase/resolution mismatch (distinct from
        returning False, which means "the conditional UPDATE's WHERE
        clause didn't match" -- a stale holder or wrong state) so the CLI
        can report the ACTUAL problem instead of a generic failure.
        """
        row = self.get_state(run_id)
        if row is None:
            return False
        if row.phase == "discovery" and resolution not in ("cleared_obstacle", "workflow_advanced"):
            raise ValueError(
                f"{run_id!r} is a discovery-phase intervention; --resolution must be "
                f"'cleared_obstacle' or 'workflow_advanced', got {resolution!r}"
            )
        if row.phase == "replay" and resolution is not None:
            raise ValueError(
                f"{run_id!r} is a replay-phase intervention; it does not take --resolution "
                f"(the resume point is derived by find_resume_point, not declared)"
            )
        now = time.time()
        cur = self._conn.execute(
            "UPDATE control SET state='RESUMING', updated_at=?, release_note=?, resolution=? "
            "WHERE run_id=? AND state='HUMAN' AND holder=?",
            (now, note, resolution, run_id, holder),
        )
        return cur.rowcount > 0

    def mark_resumed(self, run_id: str) -> None:
        self._conn.execute(
            "UPDATE control SET state='AUTOMATION', updated_at=? WHERE run_id=?",
            (time.time(), run_id),
        )

    def get_state(self, run_id: str) -> ControlRow | None:
        row = self._conn.execute(
            "SELECT run_id, state, holder, lease_expires_at, reason, updated_at, "
            "claimed_at, release_note, phase, resolution FROM control WHERE run_id=?",
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
            return ControlRow(
                control.run_id, "PAUSED", None, None, control.reason, control.updated_at, phase=control.phase
            )
        return control

    def is_automations_turn(self, run_id: str) -> bool:
        row = self.get_state(run_id)
        return row is None or row.state == "AUTOMATION"
