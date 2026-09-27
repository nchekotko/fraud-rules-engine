from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fraud_rules_engine.events import Event

ROOT = Path(__file__).resolve().parents[1]
PROD_RULES = ROOT / "rules" / "production.yaml"
CANDIDATE_RULES = ROOT / "rules" / "candidate.yaml"
T0 = 1_767_225_600.0


@pytest.fixture
def make_event() -> Callable[..., Event]:
    counter = {"n": 0}

    def _make(etype: str, player: str = "p1", ts: float = T0, **payload: Any) -> Event:
        counter["n"] += 1
        return Event.create(f"e{counter['n']:05d}", etype, player, ts, payload)

    return _make
