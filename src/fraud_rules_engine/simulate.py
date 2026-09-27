"""Synthetic population generator (all data is fake and anonymised).

Produces a globally time-ordered event log plus ground-truth labels:

* legit players (incl. deliberate hard cases: high rollers, family members sharing a
  device, PEP-like names, names close to sanctions entries but with different DOB);
* multi-accounting bonus rings sharing devices/cards;
* solo bonus abusers;
* card testers (bursts of micro deposits over many cards);
* chargeback fraud (normal history, big deposits, withdraw, then chargebacks);
* collusion pairs (P2P transfers + opposite bets on the same markets);
* cash cyclers / mules (deposit -> withdraw with ~no wagering);
* sanctioned persons (name variants of synthetic list entries).

``loss`` per fraudulent player is a synthetic proxy for what the operator loses if the
player is never stopped (bonus cost, chargebacks + fees, dumped chips, regulatory
exposure). It is used to compute the expected value of manual review.

In the second half of the period legit deposit amounts are scaled by 1.6 (a simulated
payment-method launch) so that the drift monitor has something real to detect.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .events import Event, EventType
from .screening import load_watchlist

START_TS = 1_767_225_600.0  # 2026-01-01T00:00:00Z
DAY = 86_400.0
HOUR = 3_600.0
MINUTE = 60.0

SYLLABLES = ["ka", "lo", "mi", "ren", "sa", "to", "vel", "dan", "ora", "pek", "zu", "nil", "ter", "bo", "ria"]


@dataclass
class Population:
    events: list[Event]
    labels: dict[str, str]  # player_id -> "legit" | fraud type
    loss: dict[str, float]  # player_id -> synthetic loss proxy if never stopped
    days: int
    drift_start_ts: float
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def fraud_players(self) -> set[str]:
        return {p for p, t in self.labels.items() if t != "legit"}

    @property
    def all_players(self) -> set[str]:
        return set(self.labels)


class _Gen:
    def __init__(self, seed: int, days: int) -> None:
        self.rng = random.Random(seed)
        self.days = days
        self.end_ts = START_TS + days * DAY
        self.raw: list[tuple[float, int, str, str, dict[str, Any]]] = []
        self.n = 0
        self.labels: dict[str, str] = {}
        self.loss: dict[str, float] = {}
        self.counters: dict[str, int] = {}
        self.drift_start_ts = START_TS + days * DAY / 2
        self.markets = [f"m{i:04d}" for i in range(600)]

    # -- helpers ----------------------------------------------------------------------
    def uid(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}{self.counters[prefix]:05d}"

    def emit(self, ts: float, etype: EventType, player: str, **payload: Any) -> None:
        ts = min(ts, self.end_ts - 1)
        self.raw.append((round(ts, 3), self.n, etype.value, player, payload))
        self.n += 1

    def name(self) -> str:
        r = self.rng

        def word() -> str:
            return "".join(r.choice(SYLLABLES) for _ in range(r.randint(2, 3))).capitalize()

        return f"{word()} {word()}"

    def dob(self) -> str:
        return f"{self.rng.randint(1955, 2004)}-{self.rng.randint(1, 12):02d}-{self.rng.randint(1, 28):02d}"

    def ip(self, subnet: str | None = None) -> str:
        r = self.rng
        subnet = subnet or f"10.{r.randint(0, 40)}.{r.randint(0, 255)}"
        return f"{subnet}.{r.randint(2, 254)}"

    def amount(self, ts: float, median: float = 40.0, sigma: float = 0.8) -> float:
        a = self.rng.lognormvariate(0, sigma) * median
        if ts >= self.drift_start_ts:
            a *= 1.6  # simulated payment-method launch shifts the amount distribution
        return round(max(5.0, a), 2)

    def register(
        self,
        player: str,
        ts: float,
        label: str,
        *,
        name: str | None = None,
        dob: str | None = None,
        device: str | None = None,
        ip: str | None = None,
        country: str = "XA",
    ) -> None:
        self.labels[player] = label
        self.emit(
            ts,
            EventType.REGISTRATION,
            player,
            name=name or self.name(),
            dob=dob or self.dob(),
            country=country,
            device_id=device or self.uid("dev"),
            ip=ip or self.ip(),
        )

    def bets(self, player: str, ts: float, total: float, span: float, device: str, ip: str) -> float:
        """Spread ``total`` stake over several bets; returns the time of the last bet."""
        r = self.rng
        k = max(1, min(25, int(total // 15) + 1))
        t = ts
        for _ in range(k):
            t += r.uniform(0.02, 1.0) * span / k
            self.emit(
                t,
                EventType.BET,
                player,
                amount=round(total / k, 2),
                market_id=r.choice(self.markets),
                selection=r.choice(["home", "away", "draw"]),
                odds=round(r.uniform(1.3, 4.0), 2),
                device_id=device,
                ip=ip,
            )
        return t

    # -- populations ----------------------------------------------------------------
    def legit(self, n: int) -> None:
        r = self.rng
        watch = load_watchlist()
        pep_like = [e for e in watch if e.list_type == "PEP"][:2]
        sanction_like = [e for e in watch if e.list_type == "SANCTIONS"][:2]
        shared_devices: list[str] = []
        for i in range(n):
            p = self.uid("p")
            reg_ts = START_TS + r.uniform(0, self.days * 0.85) * DAY
            ip = self.ip()
            device = self.uid("dev")
            if i % 40 == 1 and shared_devices:  # family member / flatmate on the same device
                device = shared_devices.pop()
            elif i % 40 == 0:
                shared_devices.append(device)
            name, dob = self.name(), self.dob()
            if i < len(pep_like):  # genuine PEP customers: EDD, not fraud
                name, dob = pep_like[i].name, pep_like[i].dob or dob
            elif i - len(pep_like) < len(sanction_like):  # namesakes with different DOB
                e = sanction_like[i - len(pep_like)]
                name = e.name.replace("o", "a", 1)
            self.register(p, reg_ts, "legit", name=name, dob=dob, device=device, ip=ip)
            if r.random() < 0.9:
                self.emit(
                    reg_ts + r.uniform(0.2, 30) * HOUR,
                    EventType.KYC,
                    p,
                    status="passed" if r.random() < 0.93 else "pending",
                )
            high_roller = r.random() < 0.03
            median = 400.0 if high_roller else 40.0
            cards = [self.uid("card") for _ in range(1 if r.random() < 0.8 else 2)]
            t = reg_ts + r.uniform(0.05, 5) * HOUR
            n_dep = 1 + min(12, int(r.expovariate(1 / 3.5)))
            deposited = wagered = 0.0
            bonus_taken = False
            for _ in range(n_dep):
                if t >= self.end_ts:
                    break
                self.emit(t, EventType.LOGIN, p, device_id=device, ip=ip, success=r.random() > 0.05)
                amt = self.amount(t, median)
                ok = r.random() > 0.07
                self.emit(
                    t + 30,
                    EventType.DEPOSIT_ATTEMPT,
                    p,
                    amount=amt,
                    instrument_id=r.choice(cards),
                    device_id=device,
                    ip=ip,
                    success=ok,
                    currency="EUR",
                )
                if not ok:  # retry once, usually succeeds
                    ok = r.random() > 0.3
                    self.emit(
                        t + 90,
                        EventType.DEPOSIT_ATTEMPT,
                        p,
                        amount=amt,
                        instrument_id=r.choice(cards),
                        device_id=device,
                        ip=ip,
                        success=ok,
                        currency="EUR",
                    )
                if ok:
                    deposited += amt
                    if not bonus_taken and r.random() < 0.35:
                        bonus_taken = True
                        self.emit(
                            t + 120,
                            EventType.BONUS_CLAIM,
                            p,
                            amount=round(min(100.0, amt), 2),
                            bonus_code="WELCOME100",
                            device_id=device,
                            ip=ip,
                        )
                    stake = amt * r.uniform(1.0, 6.0)
                    wagered += stake
                    t = self.bets(p, t + 150, stake, r.uniform(1, 48) * HOUR, device, ip)
                t += r.uniform(1, 6) * DAY
            if deposited > 0 and r.random() < 0.3 and t < self.end_ts:
                self.emit(
                    t + r.uniform(1, 72) * HOUR,
                    EventType.WITHDRAWAL_REQUEST,
                    p,
                    amount=round(deposited * r.uniform(0.2, 1.3), 2),
                    instrument_id=cards[0],
                    device_id=device,
                    ip=ip,
                )

    def bonus_rings(self, n_rings: int) -> None:
        r = self.rng
        for _ in range(n_rings):
            size = r.randint(3, 8)
            devices = [self.uid("dev") for _ in range(r.randint(1, 2))]
            cards = [self.uid("card") for _ in range(r.randint(1, 3))]
            subnet = f"10.{r.randint(41, 60)}.{r.randint(0, 255)}"
            t0 = START_TS + r.uniform(1, self.days - 5) * DAY
            for _ in range(size):
                p = self.uid("p")
                dev, card, ip = r.choice(devices), r.choice(cards), self.ip(subnet)
                if r.random() < 0.25:  # careful member: fresh device and card, only the subnet is shared
                    dev, card = self.uid("dev"), self.uid("card")
                t = t0 + r.uniform(0, 36) * HOUR
                self.register(p, t, "multi_account_ring", device=dev, ip=ip)
                if r.random() < 0.5:
                    self.emit(t + HOUR, EventType.KYC, p, status="pending")
                dep = round(r.uniform(20, 50), 2)
                self.emit(
                    t + 0.2 * HOUR,
                    EventType.DEPOSIT_ATTEMPT,
                    p,
                    amount=dep,
                    instrument_id=card,
                    device_id=dev,
                    ip=ip,
                    success=True,
                    currency="EUR",
                )
                self.emit(
                    t + 0.25 * HOUR, EventType.BONUS_CLAIM, p, amount=dep, bonus_code="WELCOME100", device_id=dev, ip=ip
                )
                last = self.bets(p, t + 0.3 * HOUR, dep * r.uniform(1.5, 4.0), 6 * HOUR, dev, ip)
                self.emit(
                    last + r.uniform(0.5, 6) * HOUR,
                    EventType.WITHDRAWAL_REQUEST,
                    p,
                    amount=round(dep * r.uniform(1.6, 2.0), 2),
                    instrument_id=card,
                    device_id=dev,
                    ip=ip,
                )
                self.loss[p] = dep  # the bonus paid out

    def bonus_abusers(self, n: int) -> None:
        r = self.rng
        for _ in range(n):
            p = self.uid("p")
            dev, ip, card = self.uid("dev"), self.ip(), self.uid("card")
            t = START_TS + r.uniform(0, self.days - 3) * DAY
            self.register(p, t, "bonus_abuser", device=dev, ip=ip)
            self.emit(t + 2 * HOUR, EventType.KYC, p, status="passed")
            dep = round(r.uniform(50, 100), 2)
            self.emit(
                t + 3 * HOUR,
                EventType.DEPOSIT_ATTEMPT,
                p,
                amount=dep,
                instrument_id=card,
                device_id=dev,
                ip=ip,
                success=True,
                currency="EUR",
            )
            self.emit(
                t + 3.1 * HOUR, EventType.BONUS_CLAIM, p, amount=dep, bonus_code="WELCOME100", device_id=dev, ip=ip
            )
            last = self.bets(p, t + 3.2 * HOUR, dep * r.uniform(1.5, 4.5), 24 * HOUR, dev, ip)
            self.emit(
                last + r.uniform(1, 30) * HOUR,
                EventType.WITHDRAWAL_REQUEST,
                p,
                amount=round(dep * r.uniform(1.7, 2.0), 2),
                instrument_id=card,
                device_id=dev,
                ip=ip,
            )
            self.loss[p] = dep

    def card_testers(self, n: int) -> None:
        r = self.rng
        for _ in range(n):
            p = self.uid("p")
            dev, ip = self.uid("dev"), self.ip()
            t = START_TS + r.uniform(0, self.days - 1) * DAY
            self.register(p, t, "card_tester", device=dev, ip=ip)
            loss = 0.0
            for _ in range(r.randint(8, 20)):
                t += r.uniform(30, 240)
                ok = r.random() < 0.2
                amt = round(r.uniform(1, 5), 2)
                self.emit(
                    t,
                    EventType.DEPOSIT_ATTEMPT,
                    p,
                    amount=amt,
                    instrument_id=self.uid("card"),
                    device_id=dev,
                    ip=ip,
                    success=ok,
                    currency="EUR",
                )
                if ok:
                    loss += amt + 25.0  # later chargeback + scheme fee
            self.loss[p] = loss + 50.0  # monitoring-programme penalty proxy

    def chargeback_fraud(self, n: int) -> None:
        r = self.rng
        for _ in range(n):
            p = self.uid("p")
            dev, ip, card = self.uid("dev"), self.ip(), self.uid("card")
            t = START_TS + r.uniform(0, self.days - 12) * DAY
            self.register(p, t, "chargeback_fraud", device=dev, ip=ip)
            self.emit(t + HOUR, EventType.KYC, p, status="passed")
            t += 2 * HOUR
            patient = r.random() < 0.4  # escalates slowly: larger history, no obvious spike
            for _ in range(r.randint(3, 5)):  # build a normal-looking history
                amt = round(r.uniform(150, 350) if patient else r.uniform(20, 60), 2)
                self.emit(
                    t,
                    EventType.DEPOSIT_ATTEMPT,
                    p,
                    amount=amt,
                    instrument_id=card,
                    device_id=dev,
                    ip=ip,
                    success=True,
                    currency="EUR",
                )
                t = self.bets(p, t + 60, amt * r.uniform(1, 3), 12 * HOUR, dev, ip) + r.uniform(0.5, 2) * DAY
            big = [round(r.uniform(400, 1500), 2) for _ in range(r.randint(1, 3))]
            for amt in big:
                self.emit(
                    t,
                    EventType.DEPOSIT_ATTEMPT,
                    p,
                    amount=amt,
                    instrument_id=card,
                    device_id=dev,
                    ip=ip,
                    success=True,
                    currency="EUR",
                )
                t += r.uniform(0.5, 3) * HOUR
            t = self.bets(p, t, sum(big) * r.uniform(0.3, 0.8), 6 * HOUR, dev, ip)
            self.emit(
                t + HOUR,
                EventType.WITHDRAWAL_REQUEST,
                p,
                amount=round(sum(big) * 0.9, 2),
                instrument_id=card,
                device_id=dev,
                ip=ip,
            )
            for amt in big:
                t += r.uniform(3, 8) * DAY
                self.emit(t, EventType.CHARGEBACK, p, amount=amt, reason_code="10.4")
            if r.random() < 0.5:  # tries again after the chargebacks
                self.emit(
                    t + HOUR,
                    EventType.DEPOSIT_ATTEMPT,
                    p,
                    amount=round(r.uniform(300, 800), 2),
                    instrument_id=card,
                    device_id=dev,
                    ip=ip,
                    success=True,
                    currency="EUR",
                )
            self.loss[p] = sum(big) + 25.0 * len(big)

    def collusion(self, n_pairs: int) -> None:
        r = self.rng
        for _ in range(n_pairs):
            a, b = self.uid("p"), self.uid("p")
            t = START_TS + r.uniform(0, self.days - 6) * DAY
            devs = {a: self.uid("dev"), b: self.uid("dev")}
            ips = {a: self.ip(), b: self.ip()}
            cards = {a: self.uid("card"), b: self.uid("card")}
            for p in (a, b):
                self.register(p, t, "collusion", device=devs[p], ip=ips[p])
                self.emit(t + HOUR, EventType.KYC, p, status="passed")
                self.emit(
                    t + 2 * HOUR,
                    EventType.DEPOSIT_ATTEMPT,
                    p,
                    amount=round(r.uniform(200, 500), 2),
                    instrument_id=cards[p],
                    device_id=devs[p],
                    ip=ips[p],
                    success=True,
                    currency="EUR",
                )
            self.emit(t + 3 * HOUR, EventType.P2P_TRANSFER, a, amount=round(r.uniform(50, 150), 2), counterparty_id=b)
            t += 4 * HOUR
            dumped = 0.0
            for _ in range(r.randint(1, 6)):
                m = r.choice(self.markets)
                stake = round(r.uniform(50, 150), 2)
                self.emit(
                    t,
                    EventType.BET,
                    a,
                    amount=stake,
                    market_id=m,
                    selection="home",
                    odds=1.9,
                    device_id=devs[a],
                    ip=ips[a],
                )
                self.emit(
                    t + 40,
                    EventType.BET,
                    b,
                    amount=stake,
                    market_id=m,
                    selection="away",
                    odds=1.9,
                    device_id=devs[b],
                    ip=ips[b],
                )
                dumped += stake
                t += r.uniform(2, 10) * HOUR
            self.emit(
                t + HOUR,
                EventType.WITHDRAWAL_REQUEST,
                b,
                amount=round(dumped * 1.2, 2),
                instrument_id=cards[b],
                device_id=devs[b],
                ip=ips[b],
            )
            self.loss[a] = self.loss[b] = round(0.5 * dumped, 2)

    def cash_cyclers(self, n: int) -> None:
        r = self.rng
        for _ in range(n):
            p = self.uid("p")
            dev, ip = self.uid("dev"), self.ip()
            cards = [self.uid("card") for _ in range(r.randint(1, 4))]
            t = START_TS + r.uniform(0, self.days - 3) * DAY
            self.register(p, t, "cash_cycling", device=dev, ip=ip)
            self.emit(t + HOUR, EventType.KYC, p, status="passed" if r.random() < 0.6 else "pending")
            total = 0.0
            for c in cards:
                amt = round(r.uniform(200, 900), 2)
                total += amt
                t += r.uniform(5, 60) * MINUTE
                self.emit(
                    t,
                    EventType.DEPOSIT_ATTEMPT,
                    p,
                    amount=amt,
                    instrument_id=c,
                    device_id=dev,
                    ip=ip,
                    success=True,
                    currency="EUR",
                )
            t = self.bets(p, t, total * r.uniform(0.02, 0.5), 2 * HOUR, dev, ip)
            self.emit(
                t + r.uniform(0.5, 6) * HOUR,
                EventType.WITHDRAWAL_REQUEST,
                p,
                amount=round(total * 0.95, 2),
                instrument_id=self.uid("wallet"),
                device_id=dev,
                ip=ip,
            )
            self.loss[p] = round(0.2 * total, 2)  # regulatory exposure proxy, not a direct loss

    def sanctioned(self) -> None:
        r = self.rng
        entries = [e for e in load_watchlist() if e.list_type == "SANCTIONS"][2:5]
        variants = [lambda s: s.upper(), lambda s: " ".join(reversed(s.split())), lambda s: s.replace("v", "w")]
        for e, variant in zip(entries, variants, strict=True):
            p = self.uid("p")
            dev, ip = self.uid("dev"), self.ip()
            t = START_TS + r.uniform(0, self.days - 2) * DAY
            self.register(p, t, "sanctioned", name=variant(e.name), dob=e.dob, device=dev, ip=ip)
            self.emit(
                t + HOUR,
                EventType.DEPOSIT_ATTEMPT,
                p,
                amount=round(r.uniform(500, 3000), 2),
                instrument_id=self.uid("card"),
                device_id=dev,
                ip=ip,
                success=True,
                currency="EUR",
            )
            self.loss[p] = 5000.0  # regulatory exposure proxy


def generate_population(
    n_legit: int = 2000,
    *,
    seed: int = 7,
    days: int = 30,
    n_rings: int = 12,
    n_bonus_abusers: int = 40,
    n_card_testers: int = 25,
    n_chargeback: int = 30,
    n_collusion_pairs: int = 10,
    n_cash_cyclers: int = 20,
) -> Population:
    g = _Gen(seed, days)
    g.legit(n_legit)
    g.bonus_rings(n_rings)
    g.bonus_abusers(n_bonus_abusers)
    g.card_testers(n_card_testers)
    g.chargeback_fraud(n_chargeback)
    g.collusion(n_collusion_pairs)
    g.cash_cyclers(n_cash_cyclers)
    g.sanctioned()
    g.raw.sort(key=lambda x: (x[0], x[1]))
    events = [
        Event.create(f"e{i:07d}", etype, player, ts, payload) for i, (ts, _, etype, player, payload) in enumerate(g.raw)
    ]
    for p in g.labels:
        g.loss.setdefault(p, 0.0)
    return Population(
        events=events,
        labels=g.labels,
        loss=g.loss,
        days=days,
        drift_start_ts=g.drift_start_ts,
        meta={"seed": seed, "n_legit": n_legit},
    )
