"""Domain events. Events are immutable facts; everything else is derived from them."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    REGISTRATION = "registration"
    KYC = "kyc"
    DEPOSIT_ATTEMPT = "deposit_attempt"
    WITHDRAWAL_REQUEST = "withdrawal_request"
    CHARGEBACK = "chargeback"
    DEVICE_SEEN = "device_seen"
    LOGIN = "login"
    BONUS_CLAIM = "bonus_claim"
    BET = "bet"
    P2P_TRANSFER = "p2p_transfer"


# Required payload keys per event type. Extra keys are allowed and preserved.
REQUIRED_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.REGISTRATION: ("name", "country", "device_id", "ip"),
    EventType.KYC: ("status",),
    EventType.DEPOSIT_ATTEMPT: ("amount", "instrument_id", "device_id", "success"),
    EventType.WITHDRAWAL_REQUEST: ("amount", "instrument_id"),
    EventType.CHARGEBACK: ("amount",),
    EventType.DEVICE_SEEN: ("device_id", "ip"),
    EventType.LOGIN: ("device_id", "ip", "success"),
    EventType.BONUS_CLAIM: ("amount",),
    EventType.BET: ("amount", "market_id", "selection"),
    EventType.P2P_TRANSFER: ("amount", "counterparty_id"),
}

NUMERIC_FIELDS = ("amount",)


class EventValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    type: EventType
    player_id: str
    ts: float  # event time, epoch seconds; all windows use event time, never wall clock
    payload: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    @property
    def amount(self) -> float:
        return float(self.payload.get("amount", 0.0))

    def payload_json(self) -> str:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def create(
        cls,
        event_id: str,
        type: str | EventType,
        player_id: str,
        ts: float,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        try:
            etype = EventType(type)
        except ValueError as exc:
            raise EventValidationError(f"unknown event type: {type!r}") from exc
        payload = dict(payload or {})
        missing = [k for k in REQUIRED_FIELDS[etype] if k not in payload]
        if missing:
            raise EventValidationError(f"{etype}: missing payload fields {missing}")
        for key in NUMERIC_FIELDS:
            if key in payload:
                try:
                    value = float(payload[key])
                except (TypeError, ValueError) as exc:
                    raise EventValidationError(f"{key} must be numeric") from exc
                if value < 0:
                    raise EventValidationError(f"{key} must be non-negative")
                payload[key] = value
        if not event_id or not player_id:
            raise EventValidationError("event_id and player_id are required")
        return cls(event_id=event_id, type=etype, player_id=player_id, ts=float(ts), payload=payload)
