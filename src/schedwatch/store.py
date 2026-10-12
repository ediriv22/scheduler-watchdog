"""SQLite run log. A partial unique index guarantees one success per (job, slot)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .plan import SlotState

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    slot        TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('running','ok','failed')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT,
    external_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_ok_per_slot ON runs(job, slot) WHERE status = 'ok';
CREATE UNIQUE INDEX IF NOT EXISTS one_running_per_slot ON runs(job, slot) WHERE status = 'running';
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    job TEXT NOT NULL, slot TEXT NOT NULL, kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job, slot, kind)
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)  # autocommit
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def watching_since(self, now: datetime) -> datetime:
        """First time this database was swept. History before it is not judged (no alert storm on a fresh install)."""
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('watching_since', ?)", (_iso(now),))
        return _parse(self.db.execute("SELECT value FROM meta WHERE key='watching_since'").fetchone()[0])

    def state(self, job: str, slot: str) -> SlotState:
        rows = self.db.execute("SELECT * FROM runs WHERE job=? AND slot=? ORDER BY attempt", (job, slot)).fetchall()
        st = SlotState(attempts=len(rows))
        for r in rows:
            if r["status"] == "ok":
                st.ok = True
            elif r["status"] == "running":
                st.running_since = _parse(r["started_at"])
            elif r["finished_at"]:
                st.last_finished = _parse(r["finished_at"])
        return st

    def start(self, job: str, slot: str, now: datetime) -> int | None:
        """Claim the slot. Returns None if another run already holds it or it already succeeded."""
        n = self.db.execute("SELECT COUNT(*) FROM runs WHERE job=? AND slot=?", (job, slot)).fetchone()[0]
        try:
            cur = self.db.execute(
                "INSERT INTO runs(job, slot, attempt, status, started_at) VALUES (?,?,?,'running',?)",
                (job, slot, n + 1, _iso(now)),
            )
        except sqlite3.IntegrityError:
            return None
        return cur.lastrowid

    def finish(self, run_id: int, now: datetime, error: str | None = None, external_id: str | None = None) -> None:
        status = "failed" if error else "ok"
        try:
            self.db.execute(
                "UPDATE runs SET status=?, finished_at=?, error=?, external_id=? WHERE id=?",
                (status, _iso(now), error, external_id, run_id),
            )
        except sqlite3.IntegrityError:  # a concurrent run already succeeded for this slot
            self.db.execute(
                "UPDATE runs SET status='failed', finished_at=?, error='duplicate success suppressed' WHERE id=?",
                (_iso(now), run_id),
            )

    def expire_stale(self, now: datetime, stale_minutes: int, job: str) -> int:
        """Rows stuck in 'running' (process killed, machine slept) become failed attempts."""
        cutoff = _iso(now - timedelta(minutes=stale_minutes))
        cur = self.db.execute(
            "UPDATE runs SET status='failed', finished_at=?, error='crashed: no result recorded' "
            "WHERE job=? AND status='running' AND started_at < ?",
            (_iso(now), job, cutoff),
        )
        return cur.rowcount

    def alert_once(self, job: str, slot: str, kind: str, now: datetime) -> bool:
        try:
            self.db.execute("INSERT INTO alerts VALUES (?,?,?,?)", (job, slot, kind, _iso(now)))
            return True
        except sqlite3.IntegrityError:
            return False

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
