from __future__ import annotations

import sqlite3

import pytest

from fraud_rules_engine.events import Event, EventValidationError
from fraud_rules_engine.projections import build_accounts
from fraud_rules_engine.store import DuplicateEventError, EventStore

from .conftest import T0


def test_event_validation() -> None:
    with pytest.raises(EventValidationError, match="unknown event type"):
        Event.create("e1", "teleport", "p1", T0, {})
    with pytest.raises(EventValidationError, match="missing payload"):
        Event.create("e1", "deposit_attempt", "p1", T0, {"amount": 10})
    with pytest.raises(EventValidationError, match="non-negative"):
        Event.create("e1", "bonus_claim", "p1", T0, {"amount": -5})
    e = Event.create("e1", "bonus_claim", "p1", T0, {"amount": "12.5"})
    assert e.amount == 12.5


def test_store_is_append_only(make_event) -> None:
    store = EventStore()
    store.append(make_event("login", device_id="d1", ip="10.0.0.1", success=True))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE events SET player_id='x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM events")
    assert store.count() == 1


def test_duplicate_event_rejected(make_event) -> None:
    store = EventStore()
    e = make_event("login", device_id="d1", ip="10.0.0.1", success=True)
    store.append(e)
    with pytest.raises(DuplicateEventError):
        store.append(e)


def test_iteration_order_and_roundtrip(make_event, tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    store = EventStore(path)
    events = [make_event("bet", ts=T0 + i, amount=i, market_id="m1", selection="home") for i in range(5)]
    store.append_many(events)
    store.close()
    reopened = EventStore(path)
    assert list(reopened.iter_events()) == events
    assert list(reopened.iter_events(after_seq=3)) == events[3:]
    reopened.close()


def test_projection_rebuild(make_event) -> None:
    store = EventStore()
    evs = [
        make_event("registration", name="A B", country="XA", device_id="d1", ip="10.0.0.1"),
        make_event("deposit_attempt", amount=100, instrument_id="c1", device_id="d1", success=True),
        make_event("deposit_attempt", amount=50, instrument_id="c2", device_id="d1", success=False),
        make_event("bet", amount=30, market_id="m", selection="home"),
        make_event("withdrawal_request", amount=40, instrument_id="c1"),
        make_event("chargeback", amount=100),
    ]
    store.append_many(evs)
    acc = build_accounts(store.iter_events())["p1"]
    assert acc.deposited == 100 and acc.failed_deposits == 1
    assert acc.wagered == 30 and acc.chargebacks == 1
    assert acc.instruments == {"c1", "c2"}
    assert acc.net_cash == 100 - 40 - 100
    # rebuilding twice yields the same projection
    assert build_accounts(store.iter_events())["p1"] == acc
