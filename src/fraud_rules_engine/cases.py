"""Alerts -> cases -> analyst disposition -> per-rule feedback statistics.

* Every non-``allow`` decision raises an alert.
* Alerts for a player attach to that player's open case (one open case per player), so
  analysts review people, not individual events.
* Queue order: priority (max alert score) desc, then oldest first.
* A disposition (``fraud`` / ``not_fraud``) closes the case and credits every rule that
  contributed an alert with a true or false positive. That is the feedback loop that
  drives per-rule precision in ``GET /rules/performance``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .store import EventStore

CASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   TEXT NOT NULL,
    status      TEXT NOT NULL,            -- open | assigned | closed
    priority    REAL NOT NULL,
    created_ts  REAL NOT NULL,
    assignee    TEXT,
    disposition TEXT,                     -- fraud | not_fraud
    note        TEXT
);
CREATE INDEX IF NOT EXISTS ix_cases_player ON cases(player_id, status);
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER NOT NULL REFERENCES cases(id),
    event_id    TEXT NOT NULL,
    ts          REAL NOT NULL,
    action      TEXT NOT NULL,
    score       REAL NOT NULL,
    rule_ids    TEXT NOT NULL,
    reasons     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_feedback (
    rule_id     TEXT PRIMARY KEY,
    tp          INTEGER NOT NULL DEFAULT 0,
    fp          INTEGER NOT NULL DEFAULT 0
);
"""

DISPOSITIONS = ("fraud", "not_fraud")


class CaseError(ValueError):
    pass


@dataclass
class Case:
    id: int
    player_id: str
    status: str
    priority: float
    created_ts: float
    assignee: str | None
    disposition: str | None
    note: str | None
    alerts: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "player_id": self.player_id,
            "status": self.status,
            "priority": self.priority,
            "created_ts": self.created_ts,
            "assignee": self.assignee,
            "disposition": self.disposition,
            "note": self.note,
            "alerts": self.alerts,
        }


class CaseManager:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.conn = store.conn
        with store.lock:
            self.conn.executescript(CASE_SCHEMA)
            self.conn.commit()

    def raise_alert(
        self,
        *,
        player_id: str,
        event_id: str,
        ts: float,
        action: str,
        score: float,
        fired: list[dict[str, Any]],
        commit: bool = True,
    ) -> int:
        rule_ids = sorted({f["rule_id"] for f in fired if not f["rule_id"].startswith("__")})
        priority = max(score, 100.0 if action == "block" else 0.0)
        with self.store.lock:
            row = self.conn.execute(
                "SELECT id, priority FROM cases WHERE player_id=? AND status!='closed'", (player_id,)
            ).fetchone()
            if row is None:
                cur = self.conn.execute(
                    "INSERT INTO cases(player_id, status, priority, created_ts) VALUES (?,?,?,?)",
                    (player_id, "open", priority, ts),
                )
                assert cur.lastrowid is not None
                case_id = cur.lastrowid
            else:
                case_id = int(row["id"])
                if priority > row["priority"]:
                    self.conn.execute("UPDATE cases SET priority=? WHERE id=?", (priority, case_id))
            self.conn.execute(
                "INSERT INTO alerts(case_id, event_id, ts, action, score, rule_ids, reasons) VALUES (?,?,?,?,?,?,?)",
                (
                    case_id,
                    event_id,
                    ts,
                    action,
                    score,
                    json.dumps(rule_ids),
                    json.dumps([f["reason_code"] for f in fired]),
                ),
            )
            if commit:
                self.conn.commit()
        return case_id

    def _alerts(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM alerts WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["rule_ids"] = json.loads(d["rule_ids"])
            d["reasons"] = json.loads(d["reasons"])
            out.append(d)
        return out

    def get(self, case_id: int) -> Case:
        with self.store.lock:
            row = self.conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                raise CaseError(f"case {case_id} not found")
            return Case(**dict(row), alerts=self._alerts(case_id))

    def list(self, status: str | None = None, limit: int = 100) -> list[Case]:
        with self.store.lock:
            if status:
                rows = self.conn.execute(
                    "SELECT id FROM cases WHERE status=? ORDER BY priority DESC, created_ts, id LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id FROM cases ORDER BY priority DESC, created_ts, id LIMIT ?", (limit,)
                ).fetchall()
        return [self.get(int(r["id"])) for r in rows]

    def queue(self) -> list[Case]:
        return self.list("open", limit=10_000)

    def assign(self, case_id: int, analyst: str) -> Case:
        case = self.get(case_id)
        if case.status == "closed":
            raise CaseError(f"case {case_id} is closed")
        with self.store.lock:
            self.conn.execute("UPDATE cases SET status='assigned', assignee=? WHERE id=?", (analyst, case_id))
            self.conn.commit()
        return self.get(case_id)

    def next_case(self, analyst: str) -> Case | None:
        """Pull the highest-priority unassigned case and assign it."""
        q = self.queue()
        return self.assign(q[0].id, analyst) if q else None

    def dispose(self, case_id: int, label: str, analyst: str | None = None, note: str = "") -> Case:
        if label not in DISPOSITIONS:
            raise CaseError(f"label must be one of {DISPOSITIONS}")
        case = self.get(case_id)
        if case.status == "closed":
            raise CaseError(f"case {case_id} already closed")
        rule_ids = sorted({rid for a in case.alerts for rid in a["rule_ids"]})
        col = "tp" if label == "fraud" else "fp"
        with self.store.lock:
            self.conn.execute(
                "UPDATE cases SET status='closed', disposition=?, note=?, assignee=COALESCE(?, assignee) WHERE id=?",
                (label, note, analyst, case_id),
            )
            for rid in rule_ids:
                self.conn.execute(
                    f"INSERT INTO rule_feedback(rule_id, {col}) VALUES (?, 1)"
                    f" ON CONFLICT(rule_id) DO UPDATE SET {col} = {col} + 1",
                    (rid,),
                )
            self.conn.commit()
        return self.get(case_id)

    def rule_feedback(self) -> dict[str, dict[str, float | None]]:
        """Per-rule stats from analyst dispositions (the online feedback loop)."""
        with self.store.lock:
            fb = {r["rule_id"]: (r["tp"], r["fp"]) for r in self.conn.execute("SELECT * FROM rule_feedback")}
            alert_counts: dict[str, int] = {}
            for r in self.conn.execute("SELECT rule_ids FROM alerts"):
                for rid in json.loads(r["rule_ids"]):
                    alert_counts[rid] = alert_counts.get(rid, 0) + 1
        out: dict[str, dict[str, float | None]] = {}
        for rid in sorted(set(fb) | set(alert_counts)):
            tp, fp = fb.get(rid, (0, 0))
            reviewed = tp + fp
            out[rid] = {
                "alerts": alert_counts.get(rid, 0),
                "reviewed_cases": reviewed,
                "tp_cases": tp,
                "fp_cases": fp,
                "precision": round(tp / reviewed, 4) if reviewed else None,
            }
        return out
