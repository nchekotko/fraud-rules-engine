from __future__ import annotations

from fastapi.testclient import TestClient

from fraud_rules_engine.api import create_app

from .conftest import CANDIDATE_RULES, PROD_RULES, T0


def ev(i: int, etype: str, player: str, ts: float, **payload):
    return {"event_id": f"api{i}", "type": etype, "player_id": player, "ts": ts, "payload": payload}


def card_burst(player: str = "u1", start: int = 0) -> list[dict]:
    out = [ev(start, "registration", player, T0, name="Velo Sadan", country="XA", device_id="dv", ip="10.3.3.3")]
    for i in range(1, 5):
        out.append(
            ev(
                start + i,
                "deposit_attempt",
                player,
                T0 + 60 * i,
                amount=1.5,
                instrument_id=f"k{i}",
                device_id="dv",
                success=False,
            )
        )
    return out


def test_event_decision_case_disposition_flow() -> None:
    client = TestClient(create_app(":memory:", PROD_RULES, CANDIDATE_RULES))
    responses = [client.post("/events", json=e) for e in card_burst()]
    assert all(r.status_code == 200 for r in responses)
    last = responses[-1].json()
    assert last["action"] == "block"
    assert {"CARD_TESTING_PATTERN"} <= {r["reason_code"] for r in last["reasons"]}
    assert last["ruleset_version"] == "2026.09.1" and last["shadow"]["ruleset_version"] == "2026.10.0-rc1"
    assert "failed_deposits_1h" in last["evidence"]

    cases = client.get("/cases", params={"status": "open"}).json()
    assert len(cases) == 1 and cases[0]["player_id"] == "u1"
    cid = cases[0]["id"]
    assert client.post(f"/cases/{cid}/assign", json={"analyst": "bob"}).json()["status"] == "assigned"
    r = client.post(f"/cases/{cid}/disposition", json={"label": "fraud", "analyst": "bob", "note": "ok"})
    assert r.status_code == 200 and r.json()["disposition"] == "fraud"
    assert client.post(f"/cases/{cid}/disposition", json={"label": "fraud"}).status_code == 409
    assert client.post("/cases/999/disposition", json={"label": "fraud"}).status_code == 404
    assert client.post(f"/cases/{cid}/disposition", json={"label": "meh"}).status_code == 422

    perf = client.get("/rules/performance").json()
    assert perf["rules"]["CARD_TESTING"]["tp_cases"] == 1
    assert perf["rules"]["CARD_TESTING"]["precision"] == 1.0
    assert perf["shadow_diffs"] >= 1
    assert client.get("/rules").json()["production"] == "production@2026.09.1"


def test_validation_and_duplicates() -> None:
    client = TestClient(create_app(":memory:", PROD_RULES))
    e = ev(1, "login", "u", T0, device_id="d", ip="10.0.0.1", success=True)
    assert client.post("/events", json=e).status_code == 200
    assert client.post("/events", json=e).status_code == 409
    bad = ev(2, "deposit_attempt", "u", T0, amount=10)
    assert client.post("/events", json=bad).status_code == 422
    assert client.post("/events", json=ev(3, "teleport", "u", T0)).status_code == 422


def test_restart_rehydrates_from_event_log(tmp_path) -> None:
    db = tmp_path / "fre.sqlite"
    client = TestClient(create_app(db, PROD_RULES))
    for e in card_burst()[:3]:
        client.post("/events", json=e)
    client2 = TestClient(create_app(db, PROD_RULES))
    assert client2.get("/health").json()["rehydrated_events"] == 3
    # two failures already in the log + this one -> card testing fires only if state was rebuilt
    third = ev(10, "deposit_attempt", "u1", T0 + 400, amount=1.5, instrument_id="k9", device_id="dv", success=False)
    assert client2.post("/events", json=third).json()["action"] == "block"


def test_hot_reload_endpoint(tmp_path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text(PROD_RULES.read_text(encoding="utf-8"), encoding="utf-8")
    client = TestClient(create_app(":memory:", rules))
    text = rules.read_text(encoding="utf-8").replace('version: "2026.09.1"', 'version: "2026.09.2"')
    rules.write_text(text, encoding="utf-8")
    res = client.post("/rules/reload").json()
    assert res["production"] == {"reloaded": True, "version": "2026.09.2", "error": None}
    rules.write_text(text.replace("deposits_1h > 6", "deposits_1hour > 6"), encoding="utf-8")
    res = client.post("/rules/reload").json()
    assert res["production"]["reloaded"] is False and "unknown feature" in res["production"]["error"]
    assert client.get("/rules").json()["production"] == "production@2026.09.2"
