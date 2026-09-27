"""End-to-end simulation: synthetic population -> engine -> cases -> metrics -> summary.md."""

from __future__ import annotations

import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cases import CaseManager
from .engine import Decision, DecisionEngine
from .events import Event, EventType
from .features import Features
from .metrics import (
    Confusion,
    DriftResult,
    RulePerformance,
    ThresholdPoint,
    best_threshold,
    drift_report,
    player_confusion,
    rule_performance,
    threshold_curve,
)
from .routing import (
    DEFAULT_PSPS,
    InternalTxn,
    ProviderProfile,
    ReconReport,
    RoutingStats,
    SettlementRow,
    reconcile,
    simulate_routing,
)
from .rules import RulesetManager, load_ruleset
from .simulate import DAY, START_TS, Population, generate_population
from .store import EventStore

MONEY_EVENTS = {EventType.DEPOSIT_ATTEMPT.value, EventType.WITHDRAWAL_REQUEST.value}
DRIFT_FEATURES = ("amount", "deposits_24h", "account_age_hours", "distinct_instruments_24h")


@dataclass
class SimulationResult:
    population: Population
    production_label: str
    shadow_label: str | None
    n_events: int
    decisions: list[Decision]
    actions: Counter[str]
    confusion: Confusion
    recall_by_type: dict[str, tuple[int, int]]
    rules: list[RulePerformance]
    feedback: dict[str, dict[str, float | None]]
    cases_total: int
    analyst_error_rate: float
    shadow_diff_events: int
    shadow_action_changes: Counter[tuple[str, str]]
    shadow_confusion: Confusion | None
    curve: list[ThresholdPoint]
    best: ThresholdPoint | None
    min_approval: float
    drift: list[DriftResult]
    rings: list[set[str]]
    routing: list[RoutingStats]
    breaker_transitions: list[tuple[float, str]]
    recon: ReconReport
    recon_injected: dict[str, int]
    replay_identical: bool
    latency_us: dict[str, float]
    loss_total: float
    loss_flagged: float
    extra: dict[str, Any] = field(default_factory=dict)


def _flagged(decisions: list[Decision]) -> set[str]:
    return {d.player_id for d in decisions if d.action != "allow"}


def run_simulation(
    *,
    production: str | Path,
    shadow: str | Path | None = None,
    n_legit: int = 2000,
    seed: int = 7,
    db_path: str | Path = ":memory:",
    analyst_error_rate: float = 0.05,
    review_cost: float = 15.0,
    min_approval: float = 0.99,
) -> SimulationResult:
    pop = generate_population(n_legit, seed=seed)
    store = EventStore(db_path)
    cases = CaseManager(store)
    manager = RulesetManager(production, shadow)
    engine = DecisionEngine(store, manager, cases=cases)

    ref: dict[str, list[float]] = defaultdict(list)
    cur: dict[str, list[float]] = defaultdict(list)
    latencies: list[float] = []

    def observe(e: Event, f: Features, d: Decision) -> None:
        if e.type == EventType.DEPOSIT_ATTEMPT and pop.labels[e.player_id] == "legit":
            bucket = cur if e.ts >= pop.drift_start_ts else ref
            for name in DRIFT_FEATURES:
                bucket[name].append(float(f[name]))

    # timed loop (single commit at the end, like process_many)
    decisions: list[Decision] = []
    for e in pop.events:
        t0 = time.perf_counter()
        store.append(e, commit=False)
        d = engine.decide(e)
        latencies.append(time.perf_counter() - t0)
        observe(e, engine.last_features, d)
        decisions.append(d)
    store.commit()

    # ---- player-level detection
    flagged = _flagged(decisions)
    conf = player_confusion(flagged, pop.fraud_players, pop.all_players)
    by_type: dict[str, tuple[int, int]] = {}
    for label in sorted(set(pop.labels.values())):
        members = {p for p, t in pop.labels.items() if t == label}
        by_type[label] = (len(members & flagged), len(members))
    loss_total = sum(pop.loss[p] for p in pop.fraud_players)
    loss_flagged = sum(pop.loss[p] for p in pop.fraud_players & flagged)

    # ---- analysts work the queue (synthetic ground truth + label noise) -> feedback loop
    rng = random.Random(seed + 1)
    queue = cases.queue()
    for i, case in enumerate(queue):
        analyst = f"analyst_{i % 3}"
        cases.assign(case.id, analyst)
        truth = "fraud" if case.player_id in pop.fraud_players else "not_fraud"
        if rng.random() < analyst_error_rate:
            truth = "not_fraud" if truth == "fraud" else "fraud"
        cases.dispose(case.id, truth, analyst=analyst, note="simulated review")

    # ---- shadow comparison + back-test of the candidate as if it were production
    diffs = store.shadow_diffs()
    changes = Counter(
        (d["production_action"], d["shadow_action"]) for d in diffs if d["production_action"] != d["shadow_action"]
    )
    shadow_conf = None
    if shadow is not None:
        shadow_decisions = DecisionEngine.replay(store, load_ruleset(shadow))
        shadow_conf = player_confusion(_flagged(shadow_decisions), pop.fraud_players, pop.all_players)

    # ---- determinism: replay the log with a fresh engine
    replayed = DecisionEngine.replay(store, load_ruleset(production))
    identical = [d.key() for d in replayed] == [d.key() for d in decisions]

    # ---- threshold trade-off on money-moving events
    items = [
        (d.score, float(d.evidence.get("amount", 0.0)), d.player_id in pop.fraud_players)
        for d in decisions
        if d.event_type in MONEY_EVENTS
    ]
    curve = threshold_curve(items, [float(t) for t in range(5, 101, 5)] + [101.0])
    best = best_threshold(curve, min_approval)

    # ---- drift
    drift = drift_report(dict(ref), dict(cur))

    # ---- routing with a simulated outage of the primary PSP
    deposits = [
        (e.amount, e.ts)
        for e in pop.events
        if e.type == EventType.DEPOSIT_ATTEMPT and pop.labels[e.player_id] == "legit"
    ]
    outage = (START_TS + 10 * DAY, START_TS + 12 * DAY)
    profiles = {
        "psp_alpha": ProviderProfile(approval_rate=0.90, error_rate=0.01, outage=outage),
        "psp_beta": ProviderProfile(approval_rate=0.88, error_rate=0.01),
        "psp_gamma": ProviderProfile(approval_rate=0.93, error_rate=0.02),
    }
    routing = []
    transitions: list[tuple[float, str]] = []
    for strategy in ("naive", "smart"):
        stats, router = simulate_routing(deposits, profiles, strategy=strategy, seed=seed)
        routing.append(stats)
        if strategy == "smart":
            transitions = [(ts, f"psp_alpha:{s.value}") for ts, s in router.breakers["psp_alpha"].transitions]

    # ---- reconciliation: internal ledger vs synthetic PSP settlement with injected breaks
    recon, injected = _reconciliation_demo(deposits, seed)

    lat = sorted(latencies)
    latency_us = {
        "p50": 1e6 * lat[len(lat) // 2],
        "p99": 1e6 * lat[int(len(lat) * 0.99)],
        "mean": 1e6 * statistics.fmean(lat),
        "events_per_s": len(lat) / sum(lat),
    }
    return SimulationResult(
        population=pop,
        production_label=manager.production.label,
        shadow_label=manager.shadow.label if manager.shadow else None,
        n_events=len(pop.events),
        decisions=decisions,
        actions=Counter(d.action for d in decisions),
        confusion=conf,
        recall_by_type=by_type,
        rules=rule_performance(decisions, pop.fraud_players, pop.all_players, pop.loss, review_cost=review_cost),
        feedback=cases.rule_feedback(),
        cases_total=len(queue),
        analyst_error_rate=analyst_error_rate,
        shadow_diff_events=len(diffs),
        shadow_action_changes=changes,
        shadow_confusion=shadow_conf,
        curve=curve,
        best=best,
        min_approval=min_approval,
        drift=drift,
        rings=engine.graph.rings(),
        routing=routing,
        breaker_transitions=transitions,
        recon=recon,
        recon_injected=injected,
        replay_identical=identical,
        latency_us=latency_us,
        loss_total=loss_total,
        loss_flagged=loss_flagged,
    )


def _reconciliation_demo(deposits: list[tuple[float, float]], seed: int) -> tuple[ReconReport, dict[str, int]]:
    rng = random.Random(seed + 2)
    psps = [p.name for p in DEFAULT_PSPS]
    internal = [
        InternalTxn(f"tx{i:06d}", rng.choice(psps), round(a, 2), "approved" if rng.random() < 0.9 else "declined")
        for i, (a, _) in enumerate(deposits)
    ]
    fee = {p.name: p for p in DEFAULT_PSPS}
    rows: list[SettlementRow] = []
    injected = Counter[str]()
    for t in internal:
        if t.status != "approved":
            continue
        r = rng.random()
        if r < 0.004:
            injected["missing_in_settlement"] += 1
            continue
        amount, status = t.amount, "settled"
        if r < 0.007:
            amount = round(t.amount + rng.choice([-1, 1]) * rng.uniform(0.05, 5.0), 2)
            injected["amount_mismatch"] += 1
        elif r < 0.009:
            status = "reversed"
            injected["status_mismatch"] += 1
        rows.append(SettlementRow(t.txn_id, t.psp, amount, round(fee[t.psp].cost(t.amount), 2), status))
        if rng.random() < 0.001:
            rows.append(rows[-1])
            injected["duplicates_in_settlement"] += 1
    for i in range(3):
        rows.append(SettlementRow(f"psp_only_{i}", "psp_beta", 10.0 + i, 0.3, "settled"))
        injected["unknown_in_settlement"] += 1
    return reconcile(internal, rows), dict(injected)


# ------------------------------------------------------------------ markdown summary
def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def render_summary(r: SimulationResult) -> str:
    pop = r.population
    c = r.confusion
    lines: list[str] = []
    add = lines.append
    add("# Simulation summary (synthetic data)")
    add("")
    add(
        "> Все данные синтетические и обезличенные; результат описывает поведение движка на "
        "сгенерированной популяции, а не реальный fraud rate. Не является сертифицированной AML-системой."
    )
    add("")
    add(
        f"- seed: `{pop.meta['seed']}`, период: {pop.days} дней, событий: **{r.n_events:,}**, "
        f"игроков: **{len(pop.labels):,}** (fraud: {len(pop.fraud_players)})"
    )
    add(f"- production: `{r.production_label}`, shadow: `{r.shadow_label}`")
    add(
        f"- decision latency (in-process, SQLite in-memory): p50 {r.latency_us['p50']:.0f} µs, "
        f"p99 {r.latency_us['p99']:.0f} µs, ~{r.latency_us['events_per_s']:,.0f} events/s"
    )
    add(
        f"- deterministic replay: **{'identical' if r.replay_identical else 'MISMATCH'}** "
        f"({len(r.decisions):,} decisions re-derived from the event log)"
    )
    add("")
    add("## Decisions")
    add("")
    add("| action | events | share |")
    add("|---|---:|---:|")
    for a in ("allow", "review", "step_up_kyc", "block"):
        add(f"| {a} | {r.actions.get(a, 0):,} | {_pct(r.actions.get(a, 0) / r.n_events)} |")
    add("")
    add("## Player-level detection (any non-allow decision)")
    add("")
    add(
        f"- precision **{_pct(c.precision)}**, recall **{_pct(c.recall)}**, "
        f"false positive rate **{_pct(c.fpr)}** (TP {c.tp}, FP {c.fp}, FN {c.fn}, TN {c.tn})"
    )
    add(
        f"- synthetic loss covered by flagged players: {_money(r.loss_flagged)} of {_money(r.loss_total)} "
        f"({_pct(r.loss_flagged / r.loss_total if r.loss_total else 0)})"
    )
    add("")
    add("| segment | flagged | total | recall |")
    add("|---|---:|---:|---:|")
    for label, (hit, tot) in r.recall_by_type.items():
        add(f"| {label} | {hit} | {tot} | {_pct(hit / tot if tot else 0)} |")
    add("")
    add(
        "For `legit` the column is the false-positive count (includes genuine PEP customers "
        "sent to EDD and namesakes of sanctions entries — intended behaviour, not fraud)."
    )
    add("")
    add("## Rule performance (offline, vs ground truth)")
    add("")
    add("Review EV = 0.9 × loss of true-positive players − 15 × flagged players.")
    add("")
    add("| rule | reason code | fired events | flagged players | precision | recall | FPR | review EV |")
    add("|---|---|---:|---:|---:|---:|---:|---:|")
    for p in sorted(r.rules, key=lambda x: -x.review_ev):
        add(
            f"| {p.rule_id} | `{p.reason_code}` | {p.fired_events:,} | {p.flagged_players} | "
            f"{_pct(p.precision)} | {_pct(p.recall)} | {_pct(p.false_positive_rate)} | {_money(p.review_ev)} |"
        )
    add("")
    add("## Analyst feedback loop")
    add("")
    add(
        f"{r.cases_total} cases worked by 3 simulated analysts with {_pct(r.analyst_error_rate)} label noise. "
        "Precision below is what the online feedback loop (`GET /rules/performance`) reports, "
        "i.e. without access to ground truth."
    )
    add("")
    add("| rule | alerts | reviewed cases | TP | FP | feedback precision |")
    add("|---|---:|---:|---:|---:|---:|")
    for rid, s in r.feedback.items():
        prec = s["precision"]
        add(
            f"| {rid} | {s['alerts']} | {s['reviewed_cases']} | {s['tp_cases']} | {s['fp_cases']} | "
            f"{'n/a' if prec is None else _pct(prec)} |"
        )
    add("")
    add("## Shadow mode")
    add("")
    add(
        f"- events where `{r.shadow_label}` differs from production (action or reason codes): "
        f"**{r.shadow_diff_events:,}**"
    )
    if r.shadow_action_changes:
        add(
            "- action changes (production → shadow): "
            + ", ".join(f"{a}→{b}: {n}" for (a, b), n in r.shadow_action_changes.most_common())
        )
    if r.shadow_confusion is not None:
        s = r.shadow_confusion
        add(
            f"- back-test of the shadow rule set: precision {_pct(s.precision)}, recall {_pct(s.recall)}, "
            f"FPR {_pct(s.fpr)} (production: {_pct(c.precision)} / {_pct(c.recall)} / {_pct(c.fpr)})"
        )
    add("")
    add("## Approval rate vs fraud loss (score threshold on deposits and withdrawals)")
    add("")
    add("A money-moving event is approved when `score < threshold` (score = sum of fired rule scores, capped at 100).")
    add("")
    add(
        '"Fraud-linked amount" = amounts of deposits/withdrawals made by players labelled as fraud '
        "(a proxy for exposure, not realised loss)."
    )
    add("")
    add("| threshold | approval (all) | approval (legit players) | fraud-linked amount approved | share |")
    add("|---:|---:|---:|---:|---:|")
    for p in r.curve:
        if p.threshold in (10, 20, 25, 30, 35, 40, 50, 60, 70, 90, 101):
            add(
                f"| {p.threshold:.0f} | {_pct(p.approval_rate)} | {_pct(p.good_approval_rate)} | "
                f"{_money(p.fraud_loss)} | {_pct(p.fraud_loss_share)} |"
            )
    if r.best:
        add("")
        add(
            f"Lowest fraud-linked amount with legit approval ≥ {_pct(r.min_approval)}: threshold "
            f"**{r.best.threshold:.0f}** (legit approval {_pct(r.best.good_approval_rate)}, overall approval "
            f"{_pct(r.best.approval_rate)}, fraud-linked amount approved {_money(r.best.fraud_loss)} = "
            f"{_pct(r.best.fraud_loss_share)})."
        )
    add("")
    add("## Drift (PSI, legit deposits, first vs second half of the period)")
    add("")
    add("| feature | PSI | level | mean ref | mean current |")
    add("|---|---:|---|---:|---:|")
    for d in r.drift:
        flag = " ⚠ alert" if d.alert else ""
        add(f"| {d.feature} | {d.psi:.3f} | {d.level}{flag} | {d.ref_mean:.2f} | {d.cur_mean:.2f} |")
    add("")
    add(
        "The `amount` shift is injected by the simulator (x1.6 from mid-period) to verify that the monitor "
        "fires. `account_age_hours` drifts structurally (the population ages as the period goes on), which is "
        "a reminder that raw age-like features need a stable reference window or should not be monitored raw."
    )
    add("")
    add("## Device / payment graph")
    add("")
    ring_fraud = sum(1 for ring in r.rings if all(p in pop.fraud_players for p in ring))
    sizes = Counter(len(x) for x in r.rings)
    add(
        f"- connected components with ≥2 players: **{len(r.rings)}** "
        f"(size distribution: {', '.join(f'{k}: {v}' for k, v in sorted(sizes.items()))})"
    )
    add(
        f"- components consisting only of fraud players: {ring_fraud}; the rest are households sharing a "
        "device (legit hard case)"
    )
    add("")
    add("## Payment routing under a PSP outage")
    add("")
    add("psp_alpha returns technical errors 90% of the time between day 10 and day 12.")
    add("")
    add("| strategy | transactions | approval rate | customer-visible errors | fees, bps | PSP calls |")
    add("|---|---:|---:|---:|---:|---:|")
    for s in r.routing:
        add(
            f"| {s.strategy} | {s.transactions:,} | {_pct(s.approval_rate)} | {s.customer_errors:,} | "
            f"{s.fee_bps:.0f} | {s.attempts:,} |"
        )
    add("")
    if r.breaker_transitions:
        opens = [ts for ts, s in r.breaker_transitions if s.endswith(":open")]
        closes = [ts for ts, s in r.breaker_transitions if s.endswith(":closed")]
        add(
            f"Circuit breaker for psp_alpha: first opened at day {(opens[0] - START_TS) / DAY:.2f}, "
            f"{len(opens)} open cycles (half-open probe every 5 min during the outage), "
            + (f"finally closed at day {(closes[-1] - START_TS) / DAY:.2f}." if closes else "never closed.")
        )
        add("")
    add("## Settlement reconciliation")
    add("")
    rc = r.recon
    add(f"- matched: {rc.matched:,}; breaks found: **{rc.breaks}**")
    add(
        f"- missing in settlement: {len(rc.missing_in_settlement)}, amount mismatch: {len(rc.amount_mismatch)}, "
        f"status mismatch: {len(rc.status_mismatch)}, unknown in settlement: {len(rc.unknown_in_settlement)}, "
        f"duplicates: {len(rc.duplicates_in_settlement)}"
    )
    add(f"- injected by the simulator: {', '.join(f'{k}: {v}' for k, v in sorted(r.recon_injected.items()))}")
    add(
        f"- internal approved total {_money(rc.internal_total)} vs settled {_money(rc.settled_total)} "
        f"(difference {rc.unexplained_difference:,.2f}), PSP fees {_money(rc.fees_total)}"
    )
    add("")
    return "\n".join(lines)


def write_summary(r: SimulationResult, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_summary(r), encoding="utf-8")
    return path
