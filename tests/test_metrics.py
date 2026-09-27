from __future__ import annotations

import numpy as np

from fraud_rules_engine.engine import Decision
from fraud_rules_engine.metrics import (
    best_threshold,
    drift_report,
    player_confusion,
    psi,
    psi_level,
    rule_performance,
    threshold_curve,
)
from fraud_rules_engine.rules import FiredRule


def test_psi_stable_vs_shifted() -> None:
    rng = np.random.default_rng(0)
    ref = rng.lognormal(3, 0.8, 5000)
    assert psi(ref, rng.lognormal(3, 0.8, 5000)) < 0.02
    shifted = psi(ref, rng.lognormal(3.5, 0.8, 5000))
    assert shifted > 0.25 and psi_level(shifted) == "significant"
    assert psi_level(0.15) == "moderate"
    rep = drift_report({"x": list(ref)}, {"x": list(ref * 2)})
    assert rep[0].alert and rep[0].cur_mean > rep[0].ref_mean
    assert psi([1, 1, 1], [1, 1, 1]) == 0.0  # degenerate reference does not crash


def test_threshold_curve_tradeoff() -> None:
    items = [(0.0, 10.0, False), (20.0, 50.0, False), (60.0, 100.0, True), (95.0, 200.0, True), (0.0, 30.0, True)]
    curve = threshold_curve(items, [10, 50, 90, 101])
    rates = [p.approval_rate for p in curve]
    losses = [p.fraud_loss for p in curve]
    assert rates == sorted(rates) and losses == sorted(losses)  # monotone trade-off
    assert curve[0].fraud_loss == 30.0 and curve[0].good_approval_rate == 0.5
    assert curve[-1].approval_rate == 1.0 and curve[-1].fraud_loss_share == 1.0
    best = best_threshold(curve, min_good_approval=1.0)
    assert best is not None and best.threshold == 50
    assert best_threshold(curve, 1.01) is None


def _d(player: str, *rules: str) -> Decision:
    fired = tuple(FiredRule(r, 1, r.upper(), "high", "review", 10) for r in rules)
    return Decision(
        "e", player, "deposit_attempt", 0.0, "review" if rules else "allow", 10.0 * len(rules), fired, "t", "1", {}
    )


def test_rule_performance_and_review_ev() -> None:
    decisions = [_d("f1", "r1"), _d("f1", "r1"), _d("f2", "r1", "r2"), _d("l1", "r2"), _d("l2")]
    fraud = {"f1", "f2", "f3"}
    allp = fraud | {"l1", "l2", "l3", "l4"}
    perf = {
        p.rule_id: p
        for p in rule_performance(decisions, fraud, allp, {"f1": 100, "f2": 50}, review_cost=10, analyst_catch_rate=1.0)
    }
    r1, r2 = perf["r1"], perf["r2"]
    assert (r1.fired_events, r1.flagged_players, r1.precision) == (3, 2, 1.0)
    assert abs(r1.recall - 2 / 3) < 1e-9 and r1.false_positive_rate == 0
    assert r1.review_ev == 150 - 20
    assert r2.precision == 0.5 and r2.false_positive_rate == 0.25 and r2.review_ev == 50 - 20
    c = player_confusion({"f1", "f2", "l1"}, fraud, allp)
    assert (c.tp, c.fp, c.fn, c.tn) == (2, 1, 1, 3)
