from __future__ import annotations

import random

from fraud_rules_engine.routing import (
    PSP,
    BreakerState,
    CircuitBreaker,
    InternalTxn,
    Outcome,
    PaymentRouter,
    ProviderProfile,
    SettlementRow,
    read_settlement_csv,
    reconcile,
    simulate_routing,
    write_settlement_csv,
)


def test_circuit_breaker_lifecycle() -> None:
    cb = CircuitBreaker(window=10, min_calls=4, error_threshold=0.5, cooldown_s=60)
    for i in range(3):
        cb.record(i, error=True)
    assert cb.state == BreakerState.CLOSED  # below min_calls
    cb.record(3, error=True)
    assert cb.state == BreakerState.OPEN and not cb.allow(10)
    assert cb.allow(70) and cb.state == BreakerState.HALF_OPEN
    cb.record(70, error=True)  # failed probe
    assert cb.state == BreakerState.OPEN and not cb.allow(100)
    assert cb.allow(131)
    cb.record(131, error=False)  # successful probe
    assert cb.state == BreakerState.CLOSED
    assert [s for _, s in cb.transitions] == ["open", "half_open", "open", "half_open", "closed"]


def test_failover_on_error_but_not_on_decline() -> None:
    psps = [PSP("a", 0.01, 0), PSP("b", 0.02, 0)]
    router = PaymentRouter(psps)
    calls: list[str] = []

    def gw_error_then_ok(name: str, amount: float, ts: float) -> Outcome:
        calls.append(name)
        return Outcome.ERROR if name == "a" else Outcome.APPROVED

    res = router.route("t1", 100, 0, gw_error_then_ok)
    assert res.outcome == Outcome.APPROVED and res.psp == "b" and calls == ["a", "b"]
    assert res.cost == 2.0

    calls.clear()
    router2 = PaymentRouter(psps)

    def gw_decline(name: str, amount: float, ts: float) -> Outcome:
        calls.append(name)
        return Outcome.DECLINED

    res = router2.route("t2", 100, 0, gw_decline)
    assert res.outcome == Outcome.DECLINED and calls == ["a"]  # decline is final


def test_router_prefers_higher_expected_value() -> None:
    router = PaymentRouter([PSP("cheap", 0.01, 0), PSP("reliable", 0.02, 0)])
    for _ in range(100):
        router.approval["cheap"].record(False)
        router.approval["reliable"].record(True)
    assert router.rank(100, 0)[0].name == "reliable"


def test_outage_simulation_smart_beats_naive() -> None:
    rng = random.Random(1)
    txs = [(rng.uniform(10, 200), float(i * 60)) for i in range(3000)]
    profiles = {
        "psp_alpha": ProviderProfile(0.9, outage=(60_000.0, 120_000.0)),
        "psp_beta": ProviderProfile(0.88),
        "psp_gamma": ProviderProfile(0.92),
    }
    naive, _ = simulate_routing(txs, profiles, strategy="naive", seed=5)
    smart, router = simulate_routing(txs, profiles, strategy="smart", seed=5)
    assert smart.approval_rate > naive.approval_rate + 0.05
    assert smart.customer_errors < naive.customer_errors
    assert any(s == BreakerState.OPEN for _, s in router.breakers["psp_alpha"].transitions)


def test_reconciliation_finds_every_break_type(tmp_path) -> None:
    internal = [
        InternalTxn("t1", "a", 10.0, "approved"),
        InternalTxn("t2", "a", 20.0, "approved"),
        InternalTxn("t3", "b", 30.0, "approved"),
        InternalTxn("t4", "b", 40.0, "approved"),
        InternalTxn("t5", "b", 50.0, "declined"),
        InternalTxn("t6", "a", 60.0, "approved"),
    ]
    rows = [
        SettlementRow("t1", "a", 10.0, 0.2, "settled"),
        SettlementRow("t2", "a", 20.5, 0.4, "settled"),  # amount mismatch
        SettlementRow("t3", "b", 30.0, 0.6, "reversed"),  # status mismatch
        SettlementRow("t6", "a", 60.0, 1.0, "settled"),
        SettlementRow("t6", "a", 60.0, 1.0, "settled"),  # duplicate
        SettlementRow("zz", "b", 5.0, 0.1, "settled"),  # unknown
    ]  # t4 missing
    path = tmp_path / "settlement.csv"
    write_settlement_csv(path, rows)
    rep = reconcile(internal, read_settlement_csv(path))
    assert rep.matched == 2
    assert rep.amount_mismatch == [("t2", 20.0, 20.5)]
    assert rep.status_mismatch == [("t3", "approved", "reversed")]
    assert rep.missing_in_settlement == ["t4"]
    assert rep.unknown_in_settlement == ["zz"]
    assert rep.duplicates_in_settlement == ["t6"]
    assert rep.breaks == 5
    assert rep.internal_total == 160.0 and rep.settled_total == 95.5
