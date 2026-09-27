"""Decision engine: event -> features -> rules -> explainable decision.

Pipeline per event (strictly in log order):

1. append the event to the event store (source of truth);
2. update streaming features, the device/payment graph and cached screening scores;
3. evaluate the production rule set -> action, score, reason codes, rule versions;
4. evaluate the shadow rule set (if any) on the same features, log differences only;
5. persist the decision to the audit log and raise an alert/case if action != allow.

Replaying the same event log through a fresh engine reproduces identical decisions
(no wall clock, no randomness, no dependence on previous decisions).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .cases import CaseManager
from .events import Event, EventType
from .features import Features, FeatureStore
from .graph import PlayerGraph
from .rules import Evaluation, FiredRule, RuleSet, RulesetManager
from .screening import Screener
from .store import EventStore

ALWAYS_EVIDENCE = ("amount", "account_age_hours", "ring_size")


@dataclass(frozen=True)
class ShadowResult:
    ruleset_version: str
    action: str
    score: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    event_id: str
    player_id: str
    event_type: str
    ts: float
    action: str
    score: float
    reasons: tuple[FiredRule, ...]
    ruleset: str
    ruleset_version: str
    evidence: dict[str, Any]
    shadow: ShadowResult | None = None

    @property
    def reason_codes(self) -> list[str]:
        return [r.reason_code for r in self.reasons]

    def key(self) -> tuple[Any, ...]:
        """Comparable identity of the decision (used by replay tests)."""
        return (self.event_id, self.action, self.score, tuple(self.reason_codes), self.ruleset_version)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "player_id": self.player_id,
            "event_type": self.event_type,
            "action": self.action,
            "score": self.score,
            "reasons": [r.as_dict() for r in self.reasons],
            "ruleset": self.ruleset,
            "ruleset_version": self.ruleset_version,
            "evidence": self.evidence,
            "shadow": None
            if self.shadow is None
            else {
                "ruleset_version": self.shadow.ruleset_version,
                "action": self.shadow.action,
                "score": self.shadow.score,
                "reason_codes": list(self.shadow.reason_codes),
            },
        }


def _evidence(ruleset: RuleSet, ev: Evaluation, f: Features) -> dict[str, Any]:
    fired_ids = {r.rule_id for r in ev.fired}
    names: set[str] = set(ALWAYS_EVIDENCE)
    for rule in ruleset.rules:
        if rule.id in fired_ids:
            names |= rule.condition.features()
    return {k: f[k] for k in sorted(names) if k in f}


class DecisionEngine:
    def __init__(
        self,
        store: EventStore,
        rules: RulesetManager,
        screener: Screener | None = None,
        cases: CaseManager | None = None,
        *,
        persist: bool = True,
    ) -> None:
        self.store = store
        self.rules = rules
        self.screener = screener or Screener()
        self.cases = cases
        self.persist = persist
        self.features = FeatureStore()
        self.graph = PlayerGraph()
        self.screen_scores: dict[str, dict[str, float]] = {}
        self.last_features: Features = {}

    # -- public API -------------------------------------------------------------------
    def process(self, event: Event) -> Decision:
        """Append ``event`` to the log and decide on it."""
        self.store.append(event, commit=False)
        try:
            decision = self.decide(event)
        except Exception:
            self.store.conn.rollback()
            raise
        self.store.commit()
        return decision

    def process_many(
        self,
        events: Iterable[Event],
        observer: Callable[[Event, Features, Decision], None] | None = None,
    ) -> list[Decision]:
        """Batch variant of :meth:`process` (single commit). ``observer`` receives the full
        feature vector of each event, e.g. for drift monitoring."""
        out = []
        for e in events:
            self.store.append(e, commit=False)
            d = self.decide(e)
            if observer is not None:
                observer(e, self.last_features, d)
            out.append(d)
        self.store.commit()
        return out

    @classmethod
    def replay(
        cls, store: EventStore, rules: RulesetManager | RuleSet, screener: Screener | None = None
    ) -> list[Decision]:
        """Rebuild all state from the log with a fresh engine and return its decisions.

        Nothing is written: no decisions, no cases. Pass a different ``RuleSet`` to
        back-test a candidate rule set against history.
        """
        manager = rules if isinstance(rules, RulesetManager) else RulesetManager(rules)
        engine = cls(store, manager, screener, cases=None, persist=False)
        return [engine.decide(e) for e in store.iter_events()]

    def rehydrate(self) -> int:
        """Rebuild in-memory state (features, graph, screening) from the existing log
        after a restart, without re-logging decisions or re-raising alerts."""
        persist, self.persist = self.persist, False
        n = 0
        try:
            for e in self.store.iter_events():
                self.decide(e)
                n += 1
        finally:
            self.persist = persist
        return n

    # -- internals ----------------------------------------------------------------------
    def _screen(self, event: Event) -> dict[str, float]:
        if event.type == EventType.REGISTRATION:
            res = self.screener.screen(str(event.payload["name"]), event.payload.get("dob"))
            self.screen_scores[event.player_id] = {
                "sanctions_score": res["SANCTIONS"].score if "SANCTIONS" in res else 0.0,
                "pep_score": res["PEP"].score if "PEP" in res else 0.0,
            }
        return self.screen_scores.get(event.player_id, {"sanctions_score": 0.0, "pep_score": 0.0})

    def decide(self, event: Event) -> Decision:
        """Decide on an event that is already in the log (does not append it)."""
        f = self.features.process(event)
        f.update(self.graph.process(event))
        f.update(self._screen(event))
        self.last_features = f

        prod = self.rules.production
        ev = prod.evaluate(f)
        shadow_res: ShadowResult | None = None
        shadow = self.rules.shadow
        sev: Evaluation | None = None
        if shadow is not None:
            sev = shadow.evaluate(f)
            shadow_res = ShadowResult(shadow.version, sev.action, sev.score, tuple(sev.reason_codes))

        decision = Decision(
            event_id=event.event_id,
            player_id=event.player_id,
            event_type=event.type.value,
            ts=event.ts,
            action=ev.action,
            score=ev.score,
            reasons=ev.fired,
            ruleset=prod.name,
            ruleset_version=prod.version,
            evidence=_evidence(prod, ev, f),
            shadow=shadow_res,
        )
        if self.persist:
            self._persist(event, decision, sev)
        return decision

    def _persist(self, event: Event, d: Decision, sev: Evaluation | None) -> None:
        reasons = [r.as_dict() for r in d.reasons]
        self.store.log_decision(
            event_id=d.event_id,
            player_id=d.player_id,
            ruleset=d.ruleset,
            ruleset_version=d.ruleset_version,
            action=d.action,
            score=d.score,
            reasons=reasons,
            commit=False,
        )
        shadow = self.rules.shadow
        if sev is not None and shadow is not None:
            prod_codes, sh_codes = set(d.reason_codes), set(sev.reason_codes)
            if sev.action != d.action or prod_codes != sh_codes:
                self.store.log_shadow_diff(
                    event_id=d.event_id,
                    production_version=d.ruleset_version,
                    shadow_version=shadow.version,
                    production_action=d.action,
                    shadow_action=sev.action,
                    added=sorted(sh_codes - prod_codes),
                    removed=sorted(prod_codes - sh_codes),
                    commit=False,
                )
        if self.cases is not None and d.action != "allow":
            self.cases.raise_alert(
                player_id=d.player_id,
                event_id=d.event_id,
                ts=event.ts,
                action=d.action,
                score=d.score,
                fired=reasons,
                commit=False,
            )
