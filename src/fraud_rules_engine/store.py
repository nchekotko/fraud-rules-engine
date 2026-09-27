"""Append-only SQLite event store plus an audit log of decisions.

Events are the source of truth. UPDATE and DELETE on the ``events`` table are rejected
by triggers, so the log cannot be rewritten through SQL either. Decisions are derived
data, but they are persisted as an audit trail (who/what/which ruleset version).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .events import Event, EventType

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL UNIQUE,
    type       TEXT NOT NULL,
    player_id  TEXT NOT NULL,
    ts         REAL NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_player ON events(player_id, seq);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events table is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events table is append-only'); END;

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    player_id       TEXT NOT NULL,
    ruleset         TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    action          TEXT NOT NULL,
    score           REAL NOT NULL,
    reasons         TEXT NOT NULL,
    shadow          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_decisions_event ON decisions(event_id);

CREATE TABLE IF NOT EXISTS shadow_diffs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id           TEXT NOT NULL,
    production_version TEXT NOT NULL,
    shadow_version     TEXT NOT NULL,
    production_action  TEXT NOT NULL,
    shadow_action      TEXT NOT NULL,
    added_reasons      TEXT NOT NULL,
    removed_reasons    TEXT NOT NULL
);
"""


class DuplicateEventError(ValueError):
    pass


class EventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # -- events -----------------------------------------------------------------
    def append(self, event: Event, commit: bool = True) -> int:
        with self.lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO events(event_id, type, player_id, ts, payload) VALUES (?,?,?,?,?)",
                    (event.event_id, event.type.value, event.player_id, event.ts, event.payload_json()),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateEventError(event.event_id) from exc
            if commit:
                self.conn.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid

    def append_many(self, events: list[Event]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT INTO events(event_id, type, player_id, ts, payload) VALUES (?,?,?,?,?)",
                [(e.event_id, e.type.value, e.player_id, e.ts, e.payload_json()) for e in events],
            )
            self.conn.commit()

    def has_event(self, event_id: str) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone()
        return row is not None

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            type=EventType(row["type"]),
            player_id=row["player_id"],
            ts=row["ts"],
            payload=json.loads(row["payload"]),
        )

    def iter_events(self, after_seq: int = 0) -> Iterator[Event]:
        """Events in append order (the replay order)."""
        with self.lock:
            rows = self.conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (after_seq,)).fetchall()
        for row in rows:
            yield self._row_to_event(row)

    def events_for_player(self, player_id: str) -> list[Event]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM events WHERE player_id=? ORDER BY seq", (player_id,)).fetchall()
        return [self._row_to_event(r) for r in rows]

    def count(self) -> int:
        with self.lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    # -- decisions (audit) --------------------------------------------------------
    def log_decision(
        self,
        *,
        event_id: str,
        player_id: str,
        ruleset: str,
        ruleset_version: str,
        action: str,
        score: float,
        reasons: list[dict[str, Any]],
        shadow: bool = False,
        commit: bool = True,
    ) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO decisions(event_id, player_id, ruleset, ruleset_version, action, score,"
                " reasons, shadow) VALUES (?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    player_id,
                    ruleset,
                    ruleset_version,
                    action,
                    score,
                    json.dumps(reasons, sort_keys=True),
                    int(shadow),
                ),
            )
            if commit:
                self.conn.commit()

    def log_shadow_diff(
        self,
        *,
        event_id: str,
        production_version: str,
        shadow_version: str,
        production_action: str,
        shadow_action: str,
        added: list[str],
        removed: list[str],
        commit: bool = True,
    ) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO shadow_diffs(event_id, production_version, shadow_version,"
                " production_action, shadow_action, added_reasons, removed_reasons)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    event_id,
                    production_version,
                    shadow_version,
                    production_action,
                    shadow_action,
                    json.dumps(added),
                    json.dumps(removed),
                ),
            )
            if commit:
                self.conn.commit()

    def decisions(self, shadow: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM decisions WHERE shadow=? ORDER BY id", (int(shadow),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d["reasons"])
            out.append(d)
        return out

    def shadow_diffs(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM shadow_diffs ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["added_reasons"] = json.loads(d["added_reasons"])
            d["removed_reasons"] = json.loads(d["removed_reasons"])
            out.append(d)
        return out

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()
