from __future__ import annotations

import pytest

from fraud_rules_engine.cases import CaseError, CaseManager
from fraud_rules_engine.engine import DecisionEngine
from fraud_rules_engine.rules import RulesetManager, load_ruleset
from fraud_rules_engine.simulate import generate_population
from fraud_rules_engine.store import EventStore

from .conftest import CANDIDATE_RULES, PROD_RULES, T0


def engine_with(shadow: bool = False) -> tuple[DecisionEngine, EventStore, CaseManager]:
    store = EventStore()
    cases = CaseManager(store)
    mgr = RulesetManager(PROD_RULES, CANDIDATE_RULES if shadow else None)
    return DecisionEngine(store, mgr, cases=cases), store, cases


def card_test_burst(make_event, player="ct", n=5):
    evs = [
        make_event(
            "registration", player=player, ts=T0, name="Kalo Mipek", country="XA", device_id="dct", ip="10.0.0.9"
        )
    ]
    for i in range(n):
        evs.append(
            make_event(
                "deposit_attempt",
                player=player,
                ts=T0 + 60 * (i + 1),
                amount=2.0,
                instrument_id=f"card{i}",
                device_id="dct",
                success=False,
            )
        )
    return evs


def test_card_testing_block_with_reason_codes_and_versions(make_event) -> None:
    engine, store, _ = engine_with()
    decisions = [engine.process(e) for e in card_test_burst(make_event)]
    last = decisions[-1]
    assert last.action == "block"
    assert "CARD_TESTING_PATTERN" in last.reason_codes
    fired = {r.rule_id: r for r in last.reasons}
    assert fired["CARD_TESTING"].rule_version == 3
    assert last.ruleset_version == "2026.09.1"
    assert last.evidence["failed_deposits_1h"] == 5  # current attempt included, evidence is recorded
    logged = store.decisions()
    assert len(logged) == len(decisions) and logged[-1]["action"] == "block"
    assert logged[-1]["reasons"][0]["rule_id"]


def test_sanctions_hit_blocks_at_registration(make_event) -> None:
    engine, _, _ = engine_with()
    d = engine.process(
        make_event(
            "registration",
            player="s1",
            name="BRENNHOLT Oskar",
            dob="1980-01-30",
            country="XA",
            device_id="d",
            ip="10.0.0.1",
        )
    )
    assert d.action == "block" and "SANCTIONS_LIST_HIT" in d.reason_codes
    assert d.evidence["sanctions_score"] >= 90


def test_shadow_mode_logs_diffs_without_effect(make_event) -> None:
    prod_engine, _, _ = engine_with(shadow=False)
    shadow_engine, store, _ = engine_with(shadow=True)
    evs = card_test_burst(make_event, n=3)
    prod = [prod_engine.decide(e) for e in evs]
    with_shadow = [shadow_engine.process(e) for e in evs]
    # production outcome is identical whether or not a shadow rule set is attached
    assert [d.key() for d in prod] == [d.key() for d in with_shadow]
    # candidate fires card testing one attempt earlier -> logged as a diff
    diffs = store.shadow_diffs()
    assert diffs and diffs[0]["shadow_action"] == "block" and diffs[0]["production_action"] != "block"
    assert "CARD_TESTING_PATTERN" in diffs[0]["added_reasons"]
    assert with_shadow[1].shadow is not None and with_shadow[1].shadow.ruleset_version == "2026.10.0-rc1"


def test_deterministic_replay_on_synthetic_population() -> None:
    pop = generate_population(
        150,
        seed=3,
        n_rings=3,
        n_bonus_abusers=5,
        n_card_testers=3,
        n_chargeback=4,
        n_collusion_pairs=2,
        n_cash_cyclers=3,
    )
    engine, store, _ = engine_with(shadow=True)
    live = engine.process_many(pop.events)
    replay1 = DecisionEngine.replay(store, load_ruleset(PROD_RULES))
    replay2 = DecisionEngine.replay(store, load_ruleset(PROD_RULES))
    assert [d.key() for d in live] == [d.key() for d in replay1] == [d.key() for d in replay2]
    assert [d.evidence for d in live] == [d.evidence for d in replay1]
    # replay writes nothing
    assert len(store.decisions()) == len(live)
    # generator itself is deterministic for a seed
    again = generate_population(
        150,
        seed=3,
        n_rings=3,
        n_bonus_abusers=5,
        n_card_testers=3,
        n_chargeback=4,
        n_collusion_pairs=2,
        n_cash_cyclers=3,
    )
    assert again.events == pop.events


def test_rehydrate_restores_state_without_side_effects(make_event) -> None:
    engine, store, cases = engine_with()
    evs = card_test_burst(make_event, n=3)
    for e in evs:
        engine.process(e)
    n_dec, n_cases = len(store.decisions()), len(cases.list())
    fresh = DecisionEngine(store, RulesetManager(PROD_RULES), cases=cases)
    assert fresh.rehydrate() == len(evs)
    assert len(store.decisions()) == n_dec and len(cases.list()) == n_cases
    nxt = fresh.process(
        make_event(
            "deposit_attempt",
            player="ct",
            ts=T0 + 500,
            amount=2.0,
            instrument_id="cardX",
            device_id="dct",
            success=False,
        )
    )
    assert nxt.action == "block"  # velocity state survived the "restart"


def test_cases_grouping_queue_and_feedback(make_event) -> None:
    engine, _, cases = engine_with()
    for e in card_test_burst(make_event, player="a", n=5):
        engine.process(e)
    engine.process(
        make_event(
            "registration", player="b", ts=T0, name="Tarik Almansuri Wedd", country="XA", device_id="db", ip="10.0.0.2"
        )
    )
    queue = cases.queue()
    assert [c.player_id for c in queue][:1] == ["a"] or {c.player_id for c in queue} >= {"a"}
    case_a = next(c for c in queue if c.player_id == "a")
    assert len(case_a.alerts) >= 2  # several blocked attempts -> one case per player
    assert queue == sorted(queue, key=lambda c: (-c.priority, c.created_ts, c.id))

    picked = cases.next_case("alice")
    assert picked is not None and picked.status == "assigned" and picked.assignee == "alice"
    closed = cases.dispose(case_a.id, "fraud", analyst="alice", note="confirmed")
    assert closed.status == "closed" and closed.disposition == "fraud"
    fb = cases.rule_feedback()
    assert fb["CARD_TESTING"]["tp_cases"] == 1 and fb["CARD_TESTING"]["precision"] == 1.0
    with pytest.raises(CaseError):
        cases.dispose(case_a.id, "not_fraud")
    with pytest.raises(CaseError):
        cases.dispose(999, "fraud")
    with pytest.raises(CaseError):
        cases.dispose(queue[-1].id, "maybe")

    # a new alert after closure opens a new case
    engine.process(
        make_event(
            "deposit_attempt", player="a", ts=T0 + 900, amount=2.0, instrument_id="cz", device_id="dct", success=False
        )
    )
    assert len([c for c in cases.list() if c.player_id == "a"]) == 2
