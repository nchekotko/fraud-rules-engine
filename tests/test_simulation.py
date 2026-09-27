from __future__ import annotations

from fraud_rules_engine.cli import main
from fraud_rules_engine.report import run_simulation, write_summary
from fraud_rules_engine.simulate import generate_population

from .conftest import CANDIDATE_RULES, PROD_RULES


def test_population_contains_all_segments() -> None:
    pop = generate_population(100, seed=1)
    assert set(pop.labels.values()) == {
        "legit",
        "multi_account_ring",
        "bonus_abuser",
        "card_tester",
        "chargeback_fraud",
        "collusion",
        "cash_cycling",
        "sanctioned",
    }
    ts = [e.ts for e in pop.events]
    assert ts == sorted(ts)
    assert all(pop.loss[p] > 0 for p in pop.fraud_players)


def test_end_to_end_simulation(tmp_path) -> None:
    r = run_simulation(production=PROD_RULES, shadow=CANDIDATE_RULES, n_legit=300, seed=11)
    assert r.replay_identical
    assert r.confusion.recall > 0.6 and r.confusion.precision > 0.4
    assert r.recall_by_type["card_tester"][0] == r.recall_by_type["card_tester"][1]
    assert r.recall_by_type["sanctioned"] == (3, 3)
    # every injected reconciliation break is found, nothing extra
    assert r.recon.breaks == sum(r.recon_injected.values())
    smart = next(s for s in r.routing if s.strategy == "smart")
    naive = next(s for s in r.routing if s.strategy == "naive")
    assert smart.approval_rate > naive.approval_rate
    assert any(d.feature == "amount" and d.psi > 0.1 for d in r.drift)
    assert r.cases_total > 0 and r.feedback
    out = write_summary(r, tmp_path / "summary.md")
    text = out.read_text(encoding="utf-8")
    assert "Rule performance" in text and "identical" in text


def test_cli_validate_and_simulate(tmp_path, capsys) -> None:
    assert main(["validate", str(PROD_RULES), str(CANDIDATE_RULES)]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("ruleset: x\nversion: 1\nrules: [{id: A}]", encoding="utf-8")
    assert main(["validate", str(bad)]) == 1
    db = tmp_path / "sim.sqlite"
    out = tmp_path / "s.md"
    assert (
        main(
            [
                "simulate",
                "--players",
                "80",
                "--seed",
                "2",
                "--rules",
                str(PROD_RULES),
                "--shadow",
                str(CANDIDATE_RULES),
                "--db",
                str(db),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert out.exists()
    assert main(["replay", "--db", str(db), "--rules", str(PROD_RULES)]) == 0
    assert "replayed" in capsys.readouterr().out
