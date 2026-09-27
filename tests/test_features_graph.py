from __future__ import annotations

from fraud_rules_engine.features import FEATURE_REGISTRY, HOUR, FeatureStore
from fraud_rules_engine.graph import PlayerGraph, subnet_of

from .conftest import T0


def dep(make_event, ts, amount=20.0, card="c1", device="d1", ok=True, player="p1"):
    return make_event(
        "deposit_attempt", player=player, ts=ts, amount=amount, instrument_id=card, device_id=device, success=ok
    )


def test_velocity_windows_use_event_time(make_event) -> None:
    fs = FeatureStore()
    fs.process(make_event("registration", ts=T0, name="A", country="XA", device_id="d1", ip="10.0.0.1"))
    for i in range(3):
        f = fs.process(dep(make_event, T0 + 60 * i, ok=i != 1))
    assert f["deposits_1h"] == 3 and f["failed_deposits_1h"] == 1
    f = fs.process(dep(make_event, T0 + 2 * HOUR))
    assert f["deposits_1h"] == 1 and f["deposits_24h"] == 4
    f = fs.process(dep(make_event, T0 + 25 * HOUR))
    assert f["deposits_24h"] == 2  # the T0+2h deposit is still inside 24h, the first three are not
    assert f["account_age_hours"] == 25


def test_zscore_uses_history_before_current(make_event) -> None:
    fs = FeatureStore()
    for i, a in enumerate([20, 22, 18, 20]):
        f = fs.process(dep(make_event, T0 + i * HOUR, amount=a))
    assert abs(float(f["amount_zscore"])) < 1
    f = fs.process(dep(make_event, T0 + 10 * HOUR, amount=500))
    assert float(f["amount_zscore"]) > 100  # std floored at 1.0 for near-constant history


def test_cash_cycling_and_bonus_ratios(make_event) -> None:
    fs = FeatureStore()
    fs.process(dep(make_event, T0, amount=100))
    fs.process(make_event("bonus_claim", ts=T0 + 60, amount=100))
    fs.process(make_event("bet", ts=T0 + 120, amount=10, market_id="m", selection="home"))
    f = fs.process(make_event("withdrawal_request", ts=T0 + 3600, amount=150, instrument_id="c1"))
    assert f["wagering_ratio"] == 0.1
    assert f["bonus_to_deposit_ratio"] == 1.0
    assert f["withdrawal_to_deposit_ratio"] == 1.5
    assert f["minutes_since_last_deposit"] == 60


def test_cross_player_device_and_instrument_features(make_event) -> None:
    fs = FeatureStore()
    for i in range(4):
        f = fs.process(dep(make_event, T0 + i, card=f"c{i}", player=f"p{i}", device="shared"))
    assert f["distinct_instruments_device"] == 4 and f["distinct_players_device"] == 4
    f = fs.process(dep(make_event, T0 + 10, card="c0", player="p9", device="other"))
    assert f["distinct_players_instrument"] == 2


def test_every_produced_feature_is_registered(make_event) -> None:
    fs, g = FeatureStore(), PlayerGraph()
    e = dep(make_event, T0)
    names = set(fs.process(e)) | set(g.process(e)) | {"sanctions_score", "pep_score"}
    assert names == set(FEATURE_REGISTRY)


def test_ring_size_via_device_and_card_but_not_subnet(make_event) -> None:
    g = PlayerGraph()
    g.process(dep(make_event, T0, player="a", device="d1", card="c1"))
    g.process(dep(make_event, T0, player="b", device="d1", card="c2"))
    g.process(dep(make_event, T0, player="c", device="d9", card="c2"))
    g.process(make_event("login", player="z", device_id="dz", ip="10.1.1.5", success=True))
    g.process(make_event("login", player="a", device_id="d1", ip="10.1.1.7", success=True))
    assert g.ring_size("a") == 3 and g.ring_size("c") == 3
    assert g.ring_size("z") == 1  # same /24 subnet only -> weak link, not merged
    f = g.process(make_event("login", player="z", device_id="dz", ip="10.1.1.5", success=True))
    assert f["shared_subnet_players"] == 1
    assert subnet_of("10.1.1.5") == "10.1.1"
    assert g.rings() == [{"a", "b", "c"}]


def test_collusion_mirrored_opposite_bets(make_event) -> None:
    g = PlayerGraph()
    g.process(make_event("p2p_transfer", player="a", ts=T0, amount=50, counterparty_id="b"))
    g.process(make_event("bet", player="a", ts=T0 + 10, amount=100, selection="home", market_id="m1"))
    f = g.process(make_event("bet", player="b", ts=T0 + 50, amount=95, selection="away", market_id="m1"))
    assert f["opposite_bets_linked"] == 1 and f["p2p_counterparties"] == 1
    # far apart in time, or very different stakes -> not counted (household following the same match)
    g.process(make_event("bet", player="a", ts=T0, amount=100, selection="home", market_id="m2"))
    g.process(make_event("bet", player="b", ts=T0 + 5 * 3600, amount=100, selection="away", market_id="m2"))
    g.process(make_event("bet", player="a", ts=T0, amount=100, selection="home", market_id="m3"))
    f = g.process(make_event("bet", player="b", ts=T0 + 10, amount=10, selection="away", market_id="m3"))
    assert f["opposite_bets_linked"] == 1
    # unlinked players betting opposite sides never count
    g.process(make_event("bet", player="x", ts=T0, amount=100, selection="home", market_id="m4"))
    f = g.process(make_event("bet", player="y", ts=T0 + 5, amount=100, selection="away", market_id="m4"))
    assert f["opposite_bets_linked"] == 0
