"""Offline evaluation: rule performance on labelled synthetic ground truth, the
approval-rate vs fraud-loss trade-off for a score threshold, and PSI drift monitoring."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from .engine import Decision


# ------------------------------------------------------------------ rule performance
@dataclass(frozen=True)
class RulePerformance:
    rule_id: str
    reason_code: str
    fired_events: int
    flagged_players: int
    tp_players: int
    fp_players: int
    precision: float
    recall: float
    false_positive_rate: float
    loss_avoided: float
    review_cost: float

    @property
    def review_ev(self) -> float:
        """Expected value of manually reviewing this rule's alerts:
        expected loss avoided minus analyst cost."""
        return self.loss_avoided - self.review_cost


def rule_performance(
    decisions: Iterable[Decision],
    fraud_players: set[str],
    all_players: set[str],
    loss_by_player: dict[str, float],
    *,
    review_cost: float = 15.0,
    analyst_catch_rate: float = 0.9,
) -> list[RulePerformance]:
    """Player-level precision/recall per rule.

    A rule "flags" a player if it fired on any of their events. ``loss_by_player`` is the
    realised loss the player would cause if never stopped (synthetic ground truth);
    reviewing a true positive avoids ``analyst_catch_rate`` of it. Each flagged player
    costs one review.
    """
    fired: dict[str, set[str]] = {}
    fired_events: dict[str, int] = {}
    reason: dict[str, str] = {}
    for d in decisions:
        for r in d.reasons:
            fired.setdefault(r.rule_id, set()).add(d.player_id)
            fired_events[r.rule_id] = fired_events.get(r.rule_id, 0) + 1
            reason[r.rule_id] = r.reason_code
    n_fraud = len(fraud_players)
    n_legit = len(all_players - fraud_players)
    out = []
    for rid in sorted(fired):
        players = fired[rid]
        tp = players & fraud_players
        fp = players - fraud_players
        out.append(
            RulePerformance(
                rule_id=rid,
                reason_code=reason[rid],
                fired_events=fired_events[rid],
                flagged_players=len(players),
                tp_players=len(tp),
                fp_players=len(fp),
                precision=len(tp) / len(players) if players else 0.0,
                recall=len(tp) / n_fraud if n_fraud else 0.0,
                false_positive_rate=len(fp) / n_legit if n_legit else 0.0,
                loss_avoided=analyst_catch_rate * sum(loss_by_player.get(p, 0.0) for p in tp),
                review_cost=review_cost * len(players),
            )
        )
    return out


@dataclass(frozen=True)
class Confusion:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def fpr(self) -> float:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else 0.0


def player_confusion(flagged: set[str], fraud_players: set[str], all_players: set[str]) -> Confusion:
    tp = len(flagged & fraud_players)
    fp = len(flagged - fraud_players)
    fn = len(fraud_players - flagged)
    tn = len(all_players) - tp - fp - fn
    return Confusion(tp, fp, fn, tn)


# ------------------------------------------------------------------ threshold trade-off
@dataclass(frozen=True)
class ThresholdPoint:
    threshold: float
    approval_rate: float  # all money-moving events
    good_approval_rate: float  # events of legitimate players only (what the business watches)
    fraud_loss: float
    fraud_loss_share: float
    good_declined_rate: float
    good_volume_declined: float


def threshold_curve(items: Sequence[tuple[float, float, bool]], thresholds: Iterable[float]) -> list[ThresholdPoint]:
    """``items`` = (score, amount, is_fraud) for money-moving events.
    A transaction is approved if ``score < threshold``."""
    scores = np.array([s for s, _, _ in items], dtype=float)
    amounts = np.array([a for _, a, _ in items], dtype=float)
    fraud = np.array([f for _, _, f in items], dtype=bool)
    total_fraud = float(amounts[fraud].sum()) or 1.0
    n_good = int((~fraud).sum()) or 1
    out = []
    for t in thresholds:
        approved = scores < t
        loss = float(amounts[approved & fraud].sum())
        good_declined = ~approved & ~fraud
        out.append(
            ThresholdPoint(
                threshold=float(t),
                approval_rate=float(approved.mean()) if len(items) else 0.0,
                good_approval_rate=1.0 - float(good_declined.sum()) / n_good,
                fraud_loss=loss,
                fraud_loss_share=loss / total_fraud,
                good_declined_rate=float(good_declined.sum()) / n_good,
                good_volume_declined=float(amounts[good_declined].sum()),
            )
        )
    return out


def best_threshold(curve: Sequence[ThresholdPoint], min_good_approval: float) -> ThresholdPoint | None:
    """Lowest fraud loss subject to good-customer approval rate >= ``min_good_approval``."""
    feasible = [p for p in curve if p.good_approval_rate >= min_good_approval]
    return min(feasible, key=lambda p: (p.fraud_loss, -p.good_approval_rate)) if feasible else None


# ------------------------------------------------------------------ drift (PSI)
PSI_MODERATE = 0.1
PSI_SIGNIFICANT = 0.25


def psi(reference: Sequence[float], current: Sequence[float], bins: int = 10, eps: float = 1e-4) -> float:
    """Population Stability Index with quantile bins taken from the reference window."""
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    if len(ref) == 0 or len(cur) == 0:
        return 0.0
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:
        edges = np.array([ref.min() - 0.5, ref.min() + 0.5])
    inner = edges[1:-1]
    ref_counts = np.bincount(np.searchsorted(inner, ref, side="right"), minlength=len(edges) - 1)
    cur_counts = np.bincount(np.searchsorted(inner, cur, side="right"), minlength=len(edges) - 1)
    p = np.clip(ref_counts / len(ref), eps, None)
    q = np.clip(cur_counts / len(cur), eps, None)
    return float(np.sum((q - p) * np.log(q / p)))


def psi_level(value: float) -> str:
    if value >= PSI_SIGNIFICANT:
        return "significant"
    if value >= PSI_MODERATE:
        return "moderate"
    return "stable"


@dataclass(frozen=True)
class DriftResult:
    feature: str
    psi: float
    level: str
    ref_mean: float
    cur_mean: float

    @property
    def alert(self) -> bool:
        return self.level == "significant"


def drift_report(
    reference: dict[str, Sequence[float]], current: dict[str, Sequence[float]], bins: int = 10
) -> list[DriftResult]:
    out = []
    for name in sorted(reference):
        ref, cur = reference[name], current.get(name, [])
        value = psi(ref, cur, bins)
        out.append(
            DriftResult(
                feature=name,
                psi=round(value, 4),
                level=psi_level(value),
                ref_mean=float(np.mean(ref)) if len(ref) else math.nan,
                cur_mean=float(np.mean(cur)) if len(cur) else math.nan,
            )
        )
    return out
