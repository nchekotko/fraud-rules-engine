"""Streaming, windowed feature store.

State is updated incrementally from events; windows are measured in *event time*, so
replaying the same log always yields the same features (and therefore the same
decisions). Features that describe "history" (e.g. amount z-score) are computed from
state *before* the current event is applied; counters include the current event.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .events import Event, EventType

HOUR = 3600.0
DAY = 24 * HOUR

FeatureValue = float | int | str | bool
Features = dict[str, FeatureValue]

# Registry of every feature the rule DSL may reference: name -> (kind, description).
# Graph and screening features are produced by their own modules but live in the same
# namespace so rules can combine them.
FEATURE_REGISTRY: dict[str, tuple[str, str]] = {
    "event_type": ("str", "event type"),
    "amount": ("num", "amount of the current event (0 if none)"),
    "country": ("str", "registration country (synthetic ISO code)"),
    "account_age_hours": ("num", "hours since registration"),
    "kyc_status": ("str", "none | pending | passed | failed"),
    "deposits_1h": ("num", "deposit attempts in last 1h, incl. current"),
    "deposits_24h": ("num", "deposit attempts in last 24h, incl. current"),
    "failed_deposits_1h": ("num", "failed deposit attempts in last 1h"),
    "failed_deposits_24h": ("num", "failed deposit attempts in last 24h"),
    "distinct_instruments_24h": ("num", "distinct payment instruments used by player in 24h"),
    "distinct_instruments_device": ("num", "distinct instruments ever seen on this device (all players)"),
    "distinct_players_device": ("num", "distinct players ever seen on this device"),
    "distinct_players_instrument": ("num", "distinct players ever using this instrument"),
    "amount_zscore": ("num", "z-score of deposit amount vs player's successful deposit history"),
    "total_deposited": ("num", "lifetime successful deposits"),
    "wagering_ratio": ("num", "lifetime wagered / lifetime deposited"),
    "withdrawal_to_deposit_ratio": ("num", "lifetime withdrawal requests (incl. current) / deposited"),
    "bonus_to_deposit_ratio": ("num", "lifetime bonus claimed / deposited"),
    "bonus_claims": ("num", "lifetime bonus claims"),
    "chargebacks": ("num", "lifetime chargebacks, incl. current"),
    "minutes_since_last_deposit": ("num", "minutes since last successful deposit (1e6 if none)"),
    "failed_logins_1h": ("num", "failed logins in last 1h"),
    # graph features (graph.py)
    "ring_size": ("num", "players in the connected component via shared device/instrument"),
    "shared_subnet_players": ("num", "other players seen on the same /24 subnet"),
    "p2p_counterparties": ("num", "distinct peer-to-peer counterparties"),
    "opposite_bets_linked": ("num", "linked players that bet the opposite selection on same market"),
    # screening features (screening.py)
    "sanctions_score": ("num", "best fuzzy match score vs synthetic sanctions list (0-100)"),
    "pep_score": ("num", "best fuzzy match score vs synthetic PEP list (0-100)"),
}

NO_DEPOSIT_MINUTES = 1e6


class SlidingWindow:
    """Timestamps (with optional flag) retained for ``horizon`` seconds of event time."""

    def __init__(self, horizon: float) -> None:
        self.horizon = horizon
        self.items: deque[tuple[float, Any]] = deque()

    def add(self, ts: float, value: Any = None) -> None:
        self.items.append((ts, value))

    def evict(self, now: float) -> None:
        while self.items and self.items[0][0] <= now - self.horizon:
            self.items.popleft()

    def count(self, now: float, window: float) -> int:
        n = 0
        for ts, _ in reversed(self.items):
            if ts <= now - window:
                break
            n += 1
        return n

    def values(self, now: float, window: float) -> list[Any]:
        out = []
        for ts, v in reversed(self.items):
            if ts <= now - window:
                break
            out.append(v)
        return out


@dataclass
class RunningStats:
    """Welford online mean/variance."""

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)

    @property
    def std(self) -> float:
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0


@dataclass
class PlayerState:
    registered_ts: float | None = None
    country: str = "NA"
    kyc_status: str = "none"
    deposit_attempts: SlidingWindow = field(default_factory=lambda: SlidingWindow(DAY))
    failed_deposits: SlidingWindow = field(default_factory=lambda: SlidingWindow(DAY))
    instruments_24h: SlidingWindow = field(default_factory=lambda: SlidingWindow(DAY))
    failed_logins: SlidingWindow = field(default_factory=lambda: SlidingWindow(HOUR))
    deposit_stats: RunningStats = field(default_factory=RunningStats)
    deposited: float = 0.0
    wagered: float = 0.0
    withdrawn: float = 0.0
    bonus: float = 0.0
    bonus_claims: int = 0
    chargebacks: int = 0
    last_deposit_ts: float | None = None


MIN_HISTORY_FOR_ZSCORE = 3


class FeatureStore:
    def __init__(self) -> None:
        self.players: dict[str, PlayerState] = {}
        self.device_instruments: dict[str, set[str]] = {}
        self.device_players: dict[str, set[str]] = {}
        self.instrument_players: dict[str, set[str]] = {}

    def _state(self, player_id: str) -> PlayerState:
        st = self.players.get(player_id)
        if st is None:
            st = self.players[player_id] = PlayerState()
        return st

    def process(self, event: Event) -> Features:
        """Apply ``event`` to state and return the features describing it."""
        st = self._state(event.player_id)
        now = event.ts
        p = event.payload
        amount = event.amount

        # --- history-based features (pre-update) ---
        zscore = 0.0
        if event.type == EventType.DEPOSIT_ATTEMPT and st.deposit_stats.n >= MIN_HISTORY_FOR_ZSCORE:
            std = max(st.deposit_stats.std, 1.0)  # floor avoids blow-ups on constant history
            zscore = (amount - st.deposit_stats.mean) / std
        minutes_since_dep = (now - st.last_deposit_ts) / 60.0 if st.last_deposit_ts is not None else NO_DEPOSIT_MINUTES

        # --- apply event ---
        for w in (st.deposit_attempts, st.failed_deposits, st.instruments_24h, st.failed_logins):
            w.evict(now)
        device = p.get("device_id")
        if device is not None:
            self.device_players.setdefault(device, set()).add(event.player_id)

        match event.type:
            case EventType.REGISTRATION:
                st.registered_ts = now
                st.country = str(p.get("country", "NA"))
            case EventType.KYC:
                st.kyc_status = str(p["status"])
            case EventType.DEPOSIT_ATTEMPT:
                inst = str(p["instrument_id"])
                st.deposit_attempts.add(now)
                st.instruments_24h.add(now, inst)
                self.device_instruments.setdefault(str(device), set()).add(inst)
                self.instrument_players.setdefault(inst, set()).add(event.player_id)
                if p["success"]:
                    st.deposit_stats.add(amount)
                    st.deposited += amount
                    st.last_deposit_ts = now
                else:
                    st.failed_deposits.add(now)
            case EventType.WITHDRAWAL_REQUEST:
                st.withdrawn += amount
                inst = str(p["instrument_id"])
                self.instrument_players.setdefault(inst, set()).add(event.player_id)
            case EventType.CHARGEBACK:
                st.chargebacks += 1
            case EventType.LOGIN:
                if not p["success"]:
                    st.failed_logins.add(now)
            case EventType.BONUS_CLAIM:
                st.bonus += amount
                st.bonus_claims += 1
            case EventType.BET:
                st.wagered += amount
            case _:
                pass

        denom = max(st.deposited, 1.0)
        inst_id = p.get("instrument_id")
        features: Features = {
            "event_type": event.type.value,
            "amount": amount,
            "country": st.country,
            "account_age_hours": (now - st.registered_ts) / HOUR if st.registered_ts is not None else 0.0,
            "kyc_status": st.kyc_status,
            "deposits_1h": st.deposit_attempts.count(now, HOUR),
            "deposits_24h": st.deposit_attempts.count(now, DAY),
            "failed_deposits_1h": st.failed_deposits.count(now, HOUR),
            "failed_deposits_24h": st.failed_deposits.count(now, DAY),
            "distinct_instruments_24h": len(set(st.instruments_24h.values(now, DAY))),
            "distinct_instruments_device": len(self.device_instruments.get(str(device), ()))
            if device is not None
            else 0,
            "distinct_players_device": len(self.device_players.get(device, ())) if device else 0,
            "distinct_players_instrument": len(self.instrument_players.get(str(inst_id), ()))
            if inst_id is not None
            else 0,
            "amount_zscore": round(zscore, 6),
            "total_deposited": st.deposited,
            "wagering_ratio": st.wagered / denom,
            "withdrawal_to_deposit_ratio": st.withdrawn / denom,
            "bonus_to_deposit_ratio": st.bonus / denom,
            "bonus_claims": st.bonus_claims,
            "chargebacks": st.chargebacks,
            "minutes_since_last_deposit": minutes_since_dep,
            "failed_logins_1h": st.failed_logins.count(now, HOUR),
        }
        return features
