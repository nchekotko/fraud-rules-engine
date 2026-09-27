"""Read models (projections) rebuilt purely from the event log."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .events import Event, EventType


@dataclass
class AccountView:
    player_id: str
    registered_ts: float | None = None
    country: str | None = None
    kyc_status: str = "none"
    deposited: float = 0.0
    failed_deposits: int = 0
    withdrawn_requested: float = 0.0
    wagered: float = 0.0
    bonus_claimed: float = 0.0
    chargebacks: int = 0
    chargeback_amount: float = 0.0
    p2p_out: float = 0.0
    devices: set[str] = field(default_factory=set)
    instruments: set[str] = field(default_factory=set)
    last_event_ts: float = 0.0

    @property
    def net_cash(self) -> float:
        return self.deposited - self.withdrawn_requested - self.chargeback_amount


def apply(view: AccountView, event: Event) -> None:
    view.last_event_ts = max(view.last_event_ts, event.ts)
    p = event.payload
    match event.type:
        case EventType.REGISTRATION:
            view.registered_ts = event.ts
            view.country = p.get("country")
            view.devices.add(p["device_id"])
        case EventType.KYC:
            view.kyc_status = str(p["status"])
        case EventType.DEPOSIT_ATTEMPT:
            view.instruments.add(p["instrument_id"])
            view.devices.add(p["device_id"])
            if p["success"]:
                view.deposited += event.amount
            else:
                view.failed_deposits += 1
        case EventType.WITHDRAWAL_REQUEST:
            view.withdrawn_requested += event.amount
        case EventType.CHARGEBACK:
            view.chargebacks += 1
            view.chargeback_amount += event.amount
        case EventType.DEVICE_SEEN | EventType.LOGIN:
            view.devices.add(p["device_id"])
        case EventType.BONUS_CLAIM:
            view.bonus_claimed += event.amount
        case EventType.BET:
            view.wagered += event.amount
        case EventType.P2P_TRANSFER:
            view.p2p_out += event.amount


def build_accounts(events: Iterable[Event]) -> dict[str, AccountView]:
    accounts: dict[str, AccountView] = {}
    for e in events:
        view = accounts.get(e.player_id)
        if view is None:
            view = accounts[e.player_id] = AccountView(e.player_id)
        apply(view, e)
    return accounts
