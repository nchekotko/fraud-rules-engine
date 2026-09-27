"""Payment routing across PSPs, circuit-breaker failover and settlement reconciliation.

Routing score per provider: ``p_approve * amount - fee(amount)``, i.e. expected processed
volume minus expected cost, where ``p_approve`` is a Beta-smoothed approval rate over a
sliding window of recent outcomes. Technical errors (timeouts, 5xx) feed a per-provider
circuit breaker; hard declines do not (a decline is the issuer's answer, not an outage)
and are never retried on another PSP, which would look like card testing to issuers.

All providers, fees and rates are synthetic.
"""

from __future__ import annotations

import csv
import random
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Outcome(StrEnum):
    APPROVED = "approved"
    DECLINED = "declined"
    ERROR = "error"


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class PSP:
    name: str
    fee_pct: float
    fee_fixed: float

    def cost(self, amount: float) -> float:
        return amount * self.fee_pct + self.fee_fixed


class CircuitBreaker:
    """Error-rate breaker on a sliding window of calls, driven by event time."""

    def __init__(
        self, window: int = 20, min_calls: int = 10, error_threshold: float = 0.5, cooldown_s: float = 300.0
    ) -> None:
        self.window = window
        self.min_calls = min_calls
        self.error_threshold = error_threshold
        self.cooldown_s = cooldown_s
        self.calls: deque[bool] = deque(maxlen=window)  # True = error
        self.state = BreakerState.CLOSED
        self.opened_at = 0.0
        self.transitions: list[tuple[float, BreakerState]] = []

    def _set(self, ts: float, state: BreakerState) -> None:
        if state != self.state:
            self.state = state
            self.transitions.append((ts, state))

    def allow(self, ts: float) -> bool:
        if self.state == BreakerState.OPEN and ts - self.opened_at >= self.cooldown_s:
            self._set(ts, BreakerState.HALF_OPEN)
        return self.state != BreakerState.OPEN

    def record(self, ts: float, error: bool) -> None:
        if self.state == BreakerState.HALF_OPEN:
            if error:
                self.opened_at = ts
                self._set(ts, BreakerState.OPEN)
            else:
                self.calls.clear()
                self._set(ts, BreakerState.CLOSED)
            return
        self.calls.append(error)
        if len(self.calls) >= self.min_calls and sum(self.calls) / len(self.calls) >= self.error_threshold:
            self.opened_at = ts
            self.calls.clear()
            self._set(ts, BreakerState.OPEN)


class ApprovalEstimator:
    """Beta(prior) smoothed approval rate over the last ``window`` non-error outcomes."""

    def __init__(self, window: int = 200, prior_approved: float = 8.0, prior_declined: float = 2.0) -> None:
        self.outcomes: deque[bool] = deque(maxlen=window)
        self.a = prior_approved
        self.b = prior_declined

    def record(self, approved: bool) -> None:
        self.outcomes.append(approved)

    @property
    def estimate(self) -> float:
        k = sum(self.outcomes)
        return (k + self.a) / (len(self.outcomes) + self.a + self.b)


@dataclass
class Attempt:
    psp: str
    outcome: Outcome


@dataclass
class RouteResult:
    txn_id: str
    outcome: Outcome
    psp: str | None
    attempts: list[Attempt] = field(default_factory=list)
    cost: float = 0.0


Gateway = Callable[[str, float, float], Outcome]  # (psp_name, amount, ts) -> outcome


class PaymentRouter:
    def __init__(self, psps: Iterable[PSP], *, failover: bool = True, smart: bool = True) -> None:
        self.psps = {p.name: p for p in psps}
        self.order = list(self.psps)
        self.failover = failover
        self.smart = smart
        self.breakers = {name: CircuitBreaker() for name in self.psps}
        self.approval = {name: ApprovalEstimator() for name in self.psps}

    def rank(self, amount: float, ts: float) -> list[PSP]:
        available = [p for p in self.psps.values() if not self.smart or self.breakers[p.name].allow(ts)]
        if not self.smart:
            return sorted(available, key=lambda p: p.cost(amount))  # naive: cheapest first
        return sorted(
            available,
            key=lambda p: (-(self.approval[p.name].estimate * amount - p.cost(amount)), p.name),
        )

    def route(self, txn_id: str, amount: float, ts: float, gateway: Gateway) -> RouteResult:
        ranked = self.rank(amount, ts)
        if not self.failover:
            ranked = ranked[:1]
        result = RouteResult(txn_id, Outcome.ERROR, None)
        for psp in ranked:
            outcome = gateway(psp.name, amount, ts)
            result.attempts.append(Attempt(psp.name, outcome))
            self.breakers[psp.name].record(ts, outcome == Outcome.ERROR)
            if outcome != Outcome.ERROR:
                self.approval[psp.name].record(outcome == Outcome.APPROVED)
                result.outcome = outcome
                result.psp = psp.name
                if outcome == Outcome.APPROVED:
                    result.cost = psp.cost(amount)
                return result  # a decline is final: no cascading retries
        return result


# ------------------------------------------------------------------ outage simulation
@dataclass(frozen=True)
class ProviderProfile:
    approval_rate: float
    error_rate: float = 0.01
    outage: tuple[float, float] | None = None  # event-time window with elevated errors
    outage_error_rate: float = 0.9


DEFAULT_PSPS = (
    PSP("psp_alpha", fee_pct=0.018, fee_fixed=0.10),
    PSP("psp_beta", fee_pct=0.022, fee_fixed=0.05),
    PSP("psp_gamma", fee_pct=0.030, fee_fixed=0.00),
)


def simulated_gateway(profiles: dict[str, ProviderProfile], rng: random.Random) -> Gateway:
    def call(psp: str, amount: float, ts: float) -> Outcome:
        prof = profiles[psp]
        err = prof.error_rate
        if prof.outage and prof.outage[0] <= ts < prof.outage[1]:
            err = prof.outage_error_rate
        if rng.random() < err:
            return Outcome.ERROR
        return Outcome.APPROVED if rng.random() < prof.approval_rate else Outcome.DECLINED

    return call


@dataclass
class RoutingStats:
    strategy: str
    transactions: int
    approved: int
    customer_errors: int
    volume_approved: float
    fees: float
    attempts: int

    @property
    def approval_rate(self) -> float:
        return self.approved / self.transactions if self.transactions else 0.0

    @property
    def fee_bps(self) -> float:
        return 1e4 * self.fees / self.volume_approved if self.volume_approved else 0.0


def simulate_routing(
    amounts_ts: list[tuple[float, float]],
    profiles: dict[str, ProviderProfile],
    *,
    strategy: str,
    seed: int = 0,
    psps: Iterable[PSP] = DEFAULT_PSPS,
) -> tuple[RoutingStats, PaymentRouter]:
    """strategy: ``naive`` (cheapest PSP, no failover) or ``smart`` (score + breaker + failover)."""
    rng = random.Random(seed)
    smart = strategy == "smart"
    router = PaymentRouter(psps, failover=smart, smart=smart)
    gw = simulated_gateway(profiles, rng)
    stats = RoutingStats(strategy, 0, 0, 0, 0.0, 0.0, 0)
    for i, (amount, ts) in enumerate(amounts_ts):
        res = router.route(f"t{i}", amount, ts, gw)
        stats.transactions += 1
        stats.attempts += len(res.attempts)
        if res.outcome == Outcome.APPROVED:
            stats.approved += 1
            stats.volume_approved += amount
            stats.fees += res.cost
        elif res.outcome == Outcome.ERROR:
            stats.customer_errors += 1
    return stats, router


# ------------------------------------------------------------------ reconciliation
@dataclass(frozen=True)
class InternalTxn:
    txn_id: str
    psp: str
    amount: float
    status: str  # approved | declined


@dataclass(frozen=True)
class SettlementRow:
    txn_id: str
    psp: str
    amount: float
    fee: float
    status: str  # settled | reversed


@dataclass
class ReconReport:
    matched: int = 0
    amount_mismatch: list[tuple[str, float, float]] = field(default_factory=list)
    status_mismatch: list[tuple[str, str, str]] = field(default_factory=list)
    missing_in_settlement: list[str] = field(default_factory=list)
    unknown_in_settlement: list[str] = field(default_factory=list)
    duplicates_in_settlement: list[str] = field(default_factory=list)
    internal_total: float = 0.0
    settled_total: float = 0.0
    fees_total: float = 0.0

    @property
    def breaks(self) -> int:
        return (
            len(self.amount_mismatch)
            + len(self.status_mismatch)
            + len(self.missing_in_settlement)
            + len(self.unknown_in_settlement)
            + len(self.duplicates_in_settlement)
        )

    @property
    def unexplained_difference(self) -> float:
        return round(self.internal_total - self.settled_total, 2)


def reconcile(
    internal: Iterable[InternalTxn], settlement: Iterable[SettlementRow], tolerance: float = 0.01
) -> ReconReport:
    """Match approved internal transactions to the PSP settlement file by txn id."""
    rep = ReconReport()
    approved = {t.txn_id: t for t in internal if t.status == "approved"}
    rep.internal_total = round(sum(t.amount for t in approved.values()), 2)
    seen: dict[str, SettlementRow] = {}
    for row in settlement:
        if row.txn_id in seen:
            rep.duplicates_in_settlement.append(row.txn_id)
            continue
        seen[row.txn_id] = row
        rep.fees_total += row.fee
        if row.status == "settled":
            rep.settled_total += row.amount
    rep.settled_total = round(rep.settled_total, 2)
    rep.fees_total = round(rep.fees_total, 2)
    for txn_id, t in approved.items():
        row = seen.get(txn_id)
        if row is None:
            rep.missing_in_settlement.append(txn_id)
        elif row.status != "settled":
            rep.status_mismatch.append((txn_id, t.status, row.status))
        elif abs(row.amount - t.amount) > tolerance:
            rep.amount_mismatch.append((txn_id, t.amount, row.amount))
        else:
            rep.matched += 1
    rep.unknown_in_settlement = sorted(set(seen) - set(approved))
    return rep


def read_settlement_csv(path: str | Path) -> list[SettlementRow]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [
            SettlementRow(r["txn_id"], r["psp"], float(r["amount"]), float(r["fee"]), r["status"])
            for r in csv.DictReader(fh)
        ]


def write_settlement_csv(path: str | Path, rows: Iterable[SettlementRow]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["txn_id", "psp", "amount", "fee", "status"])
        for r in rows:
            w.writerow([r.txn_id, r.psp, f"{r.amount:.2f}", f"{r.fee:.2f}", r.status])
