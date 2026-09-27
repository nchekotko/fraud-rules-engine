from __future__ import annotations

import pytest

from fraud_rules_engine.rules import (
    AllOf,
    AnyOf,
    Leaf,
    RulesetManager,
    RuleValidationError,
    load_ruleset,
    load_ruleset_text,
    parse_condition,
)

from .conftest import CANDIDATE_RULES, PROD_RULES

BASE = """
ruleset: test
version: "1"
thresholds: {review: 50, block: 90}
rules:
  - id: BIG
    when:
      event: deposit_attempt
      all: ["amount >= 100"]
    action: allow
    reason: BIG_DEPOSIT
    score: 30
  - id: FAST
    version: 7
    when:
      event: [deposit_attempt]
      any:
        - "deposits_1h > 3"
        - {feature: failed_deposits_1h, op: ">=", value: 2}
    action: review
    severity: high
    reason: FAST_DEPOSITS
    score: 25
  - id: GEO
    when:
      not: {all: ["country in [XA, XB]"]}
    action: block
    reason: GEO_OUTSIDE
    score: 0
"""


def f(**kw):
    base = {"event_type": "deposit_attempt", "amount": 10.0, "deposits_1h": 1, "failed_deposits_1h": 0, "country": "XA"}
    base.update(kw)
    return base


def test_shipped_rulesets_are_valid() -> None:
    prod, cand = load_ruleset(PROD_RULES), load_ruleset(CANDIDATE_RULES)
    assert prod.version != cand.version
    assert len({r.id for r in prod.rules}) == len(prod.rules)


def test_compact_and_mapping_conditions() -> None:
    c = parse_condition({"all": ["amount between [1, 5]", {"any": ["kyc_status != passed", "chargebacks >= 1"]}]}, "t")
    assert isinstance(c, AllOf) and isinstance(c.children[1], AnyOf)
    assert c.evaluate({"amount": 3.0, "kyc_status": "none", "chargebacks": 0})
    assert not c.evaluate({"amount": 6.0, "kyc_status": "none", "chargebacks": 0})
    leaf = parse_condition("country not in [XR, XS]", "t")
    assert isinstance(leaf, Leaf) and leaf.op == "not_in"
    assert leaf.evaluate({"country": "XA"}) and not leaf.evaluate({"country": "XR"})
    # a missing feature never matches
    assert not parse_condition("amount > 1", "t").evaluate({})


def test_evaluation_actions_reasons_and_versions() -> None:
    rs = load_ruleset_text(BASE)
    ev = rs.evaluate(f())
    assert ev.action == "allow" and ev.fired == ()
    ev = rs.evaluate(f(amount=150, deposits_1h=5))
    assert ev.score == 55
    assert ev.action == "review"
    assert ev.reason_codes == ["BIG_DEPOSIT", "FAST_DEPOSITS"]
    fast = next(r for r in ev.fired if r.rule_id == "FAST")
    assert fast.rule_version == 7 and fast.severity == "high"
    assert rs.evaluate(f(country="XZ")).action == "block"
    # event scoping: same features on a withdrawal do not trigger deposit rules
    assert rs.evaluate(f(event_type="withdrawal_request", amount=150, deposits_1h=5)).fired == ()


def test_score_threshold_escalation_is_explained() -> None:
    ev = load_ruleset_text(BASE.replace("score: 30", "score: 95")).evaluate(f(amount=150))
    assert ev.action == "block"
    assert "SCORE_GE_90" in ev.reason_codes
    ev = load_ruleset_text(BASE.replace("score: 30", "score: 60")).evaluate(f(amount=150))
    assert ev.action == "review" and "SCORE_GE_50" in ev.reason_codes


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ('all: ["amout >= 100"]', "unknown feature"),
        ('all: ["country > 3"]', "numeric feature"),
        ('all: ["amount >= lots"]', "needs a number"),
        ('all: ["amount ~ 3"]', "cannot parse"),
        ('all: ["amount between [3]"]', "between"),
    ],
)
def test_validation_errors_in_conditions(snippet: str, message: str) -> None:
    bad = BASE.replace('all: ["amount >= 100"]', snippet)
    with pytest.raises(RuleValidationError, match=message):
        load_ruleset_text(bad)


def test_validation_errors_structural() -> None:
    with pytest.raises(RuleValidationError, match="duplicate"):
        load_ruleset_text(BASE.replace("id: FAST", "id: BIG"))
    with pytest.raises(RuleValidationError, match="unknown action"):
        load_ruleset_text(BASE.replace("action: review", "action: freeze"))
    with pytest.raises(RuleValidationError, match="UPPER_SNAKE"):
        load_ruleset_text(BASE.replace("reason: GEO_OUTSIDE", "reason: geo"))
    with pytest.raises(RuleValidationError, match="unknown event"):
        load_ruleset_text(BASE.replace("event: deposit_attempt", "event: deposit"))
    with pytest.raises(RuleValidationError, match="score"):
        load_ruleset_text(BASE.replace("score: 25", "score: 250"))
    with pytest.raises(RuleValidationError, match="missing top-level"):
        load_ruleset_text("ruleset: x\nrules: []")
    with pytest.raises(RuleValidationError, match="YAML"):
        load_ruleset_text("rules: [")


def test_hot_reload_keeps_last_good(tmp_path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(BASE, encoding="utf-8")
    mgr = RulesetManager(path)
    assert mgr.reload_if_changed()["production"].reloaded is False  # unchanged content
    path.write_text(BASE.replace('version: "1"', 'version: "2"'), encoding="utf-8")
    res = mgr.reload_if_changed()["production"]
    assert res.reloaded and mgr.production.version == "2"
    broken = BASE.replace('version: "1"', 'version: "3"').replace("amount >=", "amout >=")
    path.write_text(broken, encoding="utf-8")
    res = mgr.reload_if_changed()["production"]
    assert not res.reloaded and "unknown feature" in (res.error or "")
    assert mgr.production.version == "2"  # an invalid file never replaces a valid rule set
    assert mgr.last_error is not None


def test_shadow_promotion() -> None:
    mgr = RulesetManager(load_ruleset(PROD_RULES), load_ruleset(CANDIDATE_RULES))
    mgr.promote_shadow()
    assert mgr.production.name == "candidate" and mgr.shadow is None
    with pytest.raises(RuleValidationError):
        mgr.promote_shadow()
