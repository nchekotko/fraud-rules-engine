"""YAML rule DSL: parsing, static validation, evaluation, hot-reload.

A rule set is a versioned YAML document::

    ruleset: production
    version: "2026.09.1"
    thresholds: {review: 50, block: 90}     # optional score-based escalation
    rules:
      - id: CARD_TESTING
        version: 2
        when:
          event: deposit_attempt             # or a list of event types
          all:
            - "failed_deposits_1h >= 3"
            - any: ["distinct_instruments_24h >= 3", "amount < 5"]
        action: block                        # allow | review | step_up_kyc | block
        severity: high
        reason: CARD_TESTING_PATTERN
        score: 60

Leaf conditions are either compact strings ``"<feature> <op> <value>"`` or mappings
``{feature, op, value}``. Ops: ``> >= < <= == != in not_in between``. Every feature name
is checked against the feature registry at load time, so a typo fails the reload instead
of silently never firing in production.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .events import EventType
from .features import FEATURE_REGISTRY, Features

ACTIONS: tuple[str, ...] = ("allow", "review", "step_up_kyc", "block")
ACTION_RANK = {a: i for i, a in enumerate(ACTIONS)}
SEVERITIES: tuple[str, ...] = ("info", "low", "medium", "high", "critical")
OPS = {">", ">=", "<", "<=", "==", "!=", "in", "not_in", "between"}
NUMERIC_OPS = {">", ">=", "<", "<=", "between"}
MAX_SCORE = 100.0

_LEAF_RE = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*(>=|<=|==|!=|>|<|not[ _]in\b|in\b|between\b)\s*(.+?)\s*$")


class RuleValidationError(ValueError):
    pass


# ---------------------------------------------------------------------------- conditions
@dataclass(frozen=True)
class Leaf:
    feature: str
    op: str
    value: Any

    def evaluate(self, f: Features) -> bool:
        if self.feature not in f:
            return False
        x = f[self.feature]
        v = self.value
        match self.op:
            case ">":
                return float(x) > v
            case ">=":
                return float(x) >= v
            case "<":
                return float(x) < v
            case "<=":
                return float(x) <= v
            case "==":
                return bool(x == v)
            case "!=":
                return bool(x != v)
            case "in":
                return x in v
            case "not_in":
                return x not in v
            case "between":
                return bool(v[0] <= float(x) <= v[1])
        raise AssertionError(self.op)

    def describe(self) -> str:
        return f"{self.feature} {self.op} {self.value!r}"

    def features(self) -> set[str]:
        return {self.feature}


@dataclass(frozen=True)
class AllOf:
    children: tuple[Condition, ...]

    def evaluate(self, f: Features) -> bool:
        return all(c.evaluate(f) for c in self.children)

    def describe(self) -> str:
        return "(" + " AND ".join(c.describe() for c in self.children) + ")"

    def features(self) -> set[str]:
        return set().union(*(c.features() for c in self.children))


@dataclass(frozen=True)
class AnyOf:
    children: tuple[Condition, ...]

    def evaluate(self, f: Features) -> bool:
        return any(c.evaluate(f) for c in self.children)

    def describe(self) -> str:
        return "(" + " OR ".join(c.describe() for c in self.children) + ")"

    def features(self) -> set[str]:
        return set().union(*(c.features() for c in self.children))


@dataclass(frozen=True)
class Not:
    child: Condition

    def evaluate(self, f: Features) -> bool:
        return not self.child.evaluate(f)

    def describe(self) -> str:
        return f"NOT {self.child.describe()}"

    def features(self) -> set[str]:
        return self.child.features()


Condition = Leaf | AllOf | AnyOf | Not


def _check_leaf(feature: str, op: str, value: Any, where: str) -> Leaf:
    if feature not in FEATURE_REGISTRY:
        raise RuleValidationError(f"{where}: unknown feature {feature!r}")
    op = op.replace(" ", "_")
    if op not in OPS:
        raise RuleValidationError(f"{where}: unknown operator {op!r}")
    kind = FEATURE_REGISTRY[feature][0]
    if op in NUMERIC_OPS and kind != "num":
        raise RuleValidationError(f"{where}: operator {op} needs a numeric feature, {feature} is {kind}")
    if op == "between":
        if not (isinstance(value, list) and len(value) == 2 and all(_is_num(x) for x in value)):
            raise RuleValidationError(f"{where}: between needs [low, high]")
        value = (float(value[0]), float(value[1]))
    elif op in NUMERIC_OPS:
        if not _is_num(value):
            raise RuleValidationError(f"{where}: {feature} {op} needs a number, got {value!r}")
        value = float(value)
    elif op in ("in", "not_in"):
        if not isinstance(value, list):
            raise RuleValidationError(f"{where}: {op} needs a list")
        value = frozenset(value)
    return Leaf(feature, op, value)


def _is_num(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def parse_condition(node: Any, where: str) -> Condition:
    if isinstance(node, str):
        m = _LEAF_RE.match(node)
        if not m:
            raise RuleValidationError(f"{where}: cannot parse condition {node!r}")
        feature, op, raw = m.groups()
        return _check_leaf(feature, op, yaml.safe_load(raw), where)
    if isinstance(node, dict):
        keys = set(node)
        if keys == {"all"} or keys == {"any"}:
            key = next(iter(keys))
            items = node[key]
            if not isinstance(items, list) or not items:
                raise RuleValidationError(f"{where}: '{key}' needs a non-empty list")
            children = tuple(parse_condition(c, f"{where}.{key}[{i}]") for i, c in enumerate(items))
            return AllOf(children) if key == "all" else AnyOf(children)
        if keys == {"not"}:
            return Not(parse_condition(node["not"], f"{where}.not"))
        if {"feature", "op"} <= keys <= {"feature", "op", "value"}:
            return _check_leaf(str(node["feature"]), str(node["op"]), node.get("value"), where)
    raise RuleValidationError(f"{where}: invalid condition {node!r}")


# ---------------------------------------------------------------------------- rules
@dataclass(frozen=True)
class Rule:
    id: str
    version: int
    description: str
    events: frozenset[str] | None
    condition: Condition
    action: str
    severity: str
    reason_code: str
    score: float
    enabled: bool = True

    def applies_to(self, event_type: str) -> bool:
        return self.events is None or event_type in self.events

    def matches(self, f: Features) -> bool:
        return self.enabled and self.applies_to(str(f.get("event_type"))) and self.condition.evaluate(f)


@dataclass(frozen=True)
class FiredRule:
    rule_id: str
    rule_version: int
    reason_code: str
    severity: str
    action: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "reason_code": self.reason_code,
            "severity": self.severity,
            "action": self.action,
            "score": self.score,
        }


@dataclass(frozen=True)
class Evaluation:
    action: str
    score: float
    fired: tuple[FiredRule, ...]

    @property
    def reason_codes(self) -> list[str]:
        return [r.reason_code for r in self.fired]


@dataclass(frozen=True)
class RuleSet:
    name: str
    version: str
    rules: tuple[Rule, ...]
    thresholds: dict[str, float] = field(default_factory=dict)
    checksum: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"

    def evaluate(self, f: Features) -> Evaluation:
        fired = [
            FiredRule(r.id, r.version, r.reason_code, r.severity, r.action, r.score) for r in self.rules if r.matches(f)
        ]
        score = min(MAX_SCORE, sum(r.score for r in fired))
        action = "allow"
        for r in fired:
            if ACTION_RANK[r.action] > ACTION_RANK[action]:
                action = r.action
        # score-based escalation: explicit pseudo-rule so the reason is still explainable
        for thr_action in sorted(self.thresholds, key=lambda a: ACTION_RANK[a], reverse=True):
            thr = self.thresholds[thr_action]
            if score >= thr:
                if ACTION_RANK[thr_action] > ACTION_RANK[action]:
                    action = thr_action
                    fired.append(FiredRule("__score__", 0, f"SCORE_GE_{int(thr)}", "medium", thr_action, 0.0))
                break
        return Evaluation(action=action, score=score, fired=tuple(fired))


def parse_ruleset(doc: Any, checksum: str = "") -> RuleSet:
    if not isinstance(doc, dict):
        raise RuleValidationError("rule set must be a mapping")
    for key in ("ruleset", "version", "rules"):
        if key not in doc:
            raise RuleValidationError(f"missing top-level key {key!r}")
    thresholds = doc.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        raise RuleValidationError("thresholds must be a mapping")
    for a, t in thresholds.items():
        if a not in ACTIONS or a == "allow" or not _is_num(t):
            raise RuleValidationError(f"invalid threshold {a}: {t}")
    if not isinstance(doc["rules"], list):
        raise RuleValidationError("rules must be a list")
    seen: set[str] = set()
    rules: list[Rule] = []
    valid_events = {e.value for e in EventType}
    for i, raw in enumerate(doc["rules"]):
        where = f"rules[{i}]"
        if not isinstance(raw, dict):
            raise RuleValidationError(f"{where}: must be a mapping")
        rid = raw.get("id")
        if not rid or not isinstance(rid, str):
            raise RuleValidationError(f"{where}: missing id")
        where = f"rule {rid}"
        if rid in seen:
            raise RuleValidationError(f"{where}: duplicate id")
        seen.add(rid)
        for key in ("when", "action", "reason"):
            if key not in raw:
                raise RuleValidationError(f"{where}: missing {key!r}")
        when = dict(raw["when"])
        ev = when.pop("event", None)
        events: frozenset[str] | None = None
        if ev is not None:
            evs = [ev] if isinstance(ev, str) else list(ev)
            bad = [e for e in evs if e not in valid_events]
            if bad:
                raise RuleValidationError(f"{where}: unknown event types {bad}")
            events = frozenset(evs)
        if not when:
            raise RuleValidationError(f"{where}: 'when' needs a condition besides 'event'")
        condition = parse_condition(when, where)
        action = raw["action"]
        if action not in ACTIONS:
            raise RuleValidationError(f"{where}: unknown action {action!r}")
        severity = raw.get("severity", "medium")
        if severity not in SEVERITIES:
            raise RuleValidationError(f"{where}: unknown severity {severity!r}")
        score = raw.get("score", 0)
        if not _is_num(score) or not 0 <= score <= MAX_SCORE:
            raise RuleValidationError(f"{where}: score must be in [0, 100]")
        reason = str(raw["reason"])
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", reason):
            raise RuleValidationError(f"{where}: reason code must be UPPER_SNAKE_CASE")
        rules.append(
            Rule(
                id=rid,
                version=int(raw.get("version", 1)),
                description=str(raw.get("description", "")),
                events=events,
                condition=condition,
                action=action,
                severity=severity,
                reason_code=reason,
                score=float(score),
                enabled=bool(raw.get("enabled", True)),
            )
        )
    return RuleSet(
        name=str(doc["ruleset"]),
        version=str(doc["version"]),
        rules=tuple(rules),
        thresholds={k: float(v) for k, v in thresholds.items()},
        checksum=checksum,
    )


def load_ruleset_text(text: str) -> RuleSet:
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleValidationError(f"YAML error: {exc}") from exc
    return parse_ruleset(doc, checksum)


def load_ruleset(path: str | Path) -> RuleSet:
    return load_ruleset_text(Path(path).read_text(encoding="utf-8"))


@dataclass
class ReloadResult:
    reloaded: bool
    version: str
    error: str | None = None


class RulesetManager:
    """Holds the production rule set and an optional shadow rule set.

    ``reload_if_changed`` re-reads files whose content hash changed. An invalid file never
    replaces a valid rule set: the old one stays active and the error is reported.
    """

    def __init__(self, production: str | Path | RuleSet, shadow: str | Path | RuleSet | None = None):
        self.production_path: Path | None = None
        self.shadow_path: Path | None = None
        self.last_error: str | None = None
        if isinstance(production, RuleSet):
            self.production = production
        else:
            self.production_path = Path(production)
            self.production = load_ruleset(self.production_path)
        self.shadow: RuleSet | None
        if shadow is None or isinstance(shadow, RuleSet):
            self.shadow = shadow
        else:
            self.shadow_path = Path(shadow)
            self.shadow = load_ruleset(self.shadow_path)

    def _reload_one(self, path: Path, current: RuleSet | None) -> tuple[RuleSet | None, ReloadResult]:
        text = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        cur_version = current.version if current else ""
        if current is not None and checksum == current.checksum:
            return current, ReloadResult(False, cur_version)
        try:
            new = load_ruleset_text(text)
        except RuleValidationError as exc:
            self.last_error = f"{path.name}: {exc}"
            return current, ReloadResult(False, cur_version, self.last_error)
        self.last_error = None
        return new, ReloadResult(True, new.version)

    def reload_if_changed(self) -> dict[str, ReloadResult]:
        out: dict[str, ReloadResult] = {}
        if self.production_path is not None:
            new, res = self._reload_one(self.production_path, self.production)
            assert new is not None
            self.production = new
            out["production"] = res
        if self.shadow_path is not None:
            self.shadow, out["shadow"] = self._reload_one(self.shadow_path, self.shadow)
        return out

    def set_production_text(self, text: str) -> RuleSet:
        """Validate and activate a rule set from text (e.g. via an admin API)."""
        self.production = load_ruleset_text(text)
        return self.production

    def set_shadow(self, ruleset: RuleSet | None) -> None:
        self.shadow = ruleset

    def promote_shadow(self) -> None:
        if self.shadow is None:
            raise RuleValidationError("no shadow rule set to promote")
        self.production, self.shadow = self.shadow, None
