"""FastAPI service around the decision engine."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .cases import CaseError, CaseManager
from .engine import DecisionEngine
from .events import Event, EventValidationError
from .rules import RulesetManager
from .store import DuplicateEventError, EventStore

RELOAD_CHECK_INTERVAL_S = 2.0


class EventIn(BaseModel):
    event_id: str = Field(min_length=1)
    type: str
    player_id: str = Field(min_length=1)
    ts: float
    payload: dict[str, Any] = Field(default_factory=dict)


class DispositionIn(BaseModel):
    label: Literal["fraud", "not_fraud"]
    analyst: str | None = None
    note: str = ""


class AssignIn(BaseModel):
    analyst: str


def create_app(
    db_path: str | Path = ":memory:",
    production: str | Path = "rules/production.yaml",
    shadow: str | Path | None = None,
) -> FastAPI:
    store = EventStore(db_path)
    cases = CaseManager(store)
    rules = RulesetManager(production, shadow)
    engine = DecisionEngine(store, rules, cases=cases)
    replayed = engine.rehydrate()  # rebuild in-memory state from the existing log
    lock = threading.Lock()
    last_check = [time.monotonic()]

    app = FastAPI(
        title="fraud-rules-engine",
        description="Explainable, event-sourced risk decisioning (synthetic demo, not a certified AML system).",
        version="0.1.0",
    )
    app.state.engine = engine
    app.state.cases = cases
    app.state.rules = rules
    app.state.store = store

    def maybe_reload() -> None:
        now = time.monotonic()
        if now - last_check[0] >= RELOAD_CHECK_INTERVAL_S:
            last_check[0] = now
            rules.reload_if_changed()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "events": store.count(), "rehydrated_events": replayed}

    @app.post("/events")
    def post_event(body: EventIn) -> dict[str, Any]:
        try:
            event = Event.create(body.event_id, body.type, body.player_id, body.ts, body.payload)
        except EventValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with lock:  # decisions must be taken in log order
            maybe_reload()
            try:
                decision = engine.process(event)
            except DuplicateEventError as exc:
                raise HTTPException(status_code=409, detail=f"duplicate event_id {exc}") from exc
        return decision.as_dict()

    @app.get("/cases")
    def list_cases(
        status: Literal["open", "assigned", "closed"] | None = None, limit: int = Query(100, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        return [c.as_dict() for c in cases.list(status, limit)]

    @app.get("/cases/{case_id}")
    def get_case(case_id: int) -> dict[str, Any]:
        try:
            return cases.get(case_id).as_dict()
        except CaseError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/cases/{case_id}/assign")
    def assign(case_id: int, body: AssignIn) -> dict[str, Any]:
        try:
            return cases.assign(case_id, body.analyst).as_dict()
        except CaseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/cases/{case_id}/disposition")
    def disposition(case_id: int, body: DispositionIn) -> dict[str, Any]:
        try:
            cases.get(case_id)
        except CaseError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            return cases.dispose(case_id, body.label, analyst=body.analyst, note=body.note).as_dict()
        except CaseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/rules")
    def get_rules() -> dict[str, Any]:
        return {
            "production": rules.production.label,
            "production_checksum": rules.production.checksum,
            "shadow": rules.shadow.label if rules.shadow else None,
            "last_reload_error": rules.last_error,
        }

    @app.post("/rules/reload")
    def reload_rules() -> dict[str, Any]:
        with lock:
            res = rules.reload_if_changed()
        return {k: {"reloaded": v.reloaded, "version": v.version, "error": v.error} for k, v in res.items()}

    @app.get("/rules/performance")
    def rules_performance() -> dict[str, Any]:
        return {
            "production": rules.production.label,
            "rules": cases.rule_feedback(),
            "shadow_diffs": len(store.shadow_diffs()),
        }

    return app
