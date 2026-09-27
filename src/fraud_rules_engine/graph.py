"""Device / payment-instrument graph for multi-accounting and collusion signals.

Strong links (shared device, shared payment instrument) form the "identity" graph whose
connected components approximate multi-accounting rings. IP /24 subnets are a weak link
(NAT, mobile carriers) so they are only counted, never merged into components.
Peer-to-peer transfers and opposite bets on the same market between linked players are
exposed as collusion hints. An opposite bet only counts when it is placed close in time
(``OPPOSITE_BET_WINDOW_S``) with a similar stake (``OPPOSITE_BET_STAKE_TOL``): that is the
chip-dumping shape, and it keeps households that share a tablet from lighting up simply
because they follow the same matches.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import networkx as nx

from .events import Event, EventType
from .features import Features

OPPOSITE_BET_WINDOW_S = 3600.0
OPPOSITE_BET_STAKE_TOL = 0.2


def subnet_of(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else ip


class PlayerGraph:
    def __init__(self) -> None:
        self.g: nx.Graph = nx.Graph()  # strong links: player <-> device / instrument
        self.p2p: nx.Graph = nx.Graph()  # player <-> player transfers
        self.subnet_players: dict[str, set[str]] = defaultdict(set)
        self.player_subnets: dict[str, set[str]] = defaultdict(set)
        # market -> player -> [(selection, ts, stake)]
        self.market_bets: dict[str, dict[str, list[tuple[str, float, float]]]] = defaultdict(lambda: defaultdict(list))
        self.opposite_hits: Counter[str] = Counter()
        self._opposite_pairs: set[tuple[str, str, str]] = set()

    @staticmethod
    def _p(player_id: str) -> str:
        return f"player:{player_id}"

    def _link(self, player_id: str, kind: str, value: str) -> None:
        self.g.add_edge(self._p(player_id), f"{kind}:{value}")

    def linked_players(self, player_id: str) -> set[str]:
        node = self._p(player_id)
        linked: set[str] = set()
        if node in self.g:
            linked |= {n[7:] for n in nx.node_connected_component(self.g, node) if n.startswith("player:")}
        if player_id in self.p2p:
            linked |= set(self.p2p.neighbors(player_id))
        linked.discard(player_id)
        return linked

    def ring_size(self, player_id: str) -> int:
        node = self._p(player_id)
        if node not in self.g:
            return 1
        return sum(1 for n in nx.node_connected_component(self.g, node) if n.startswith("player:"))

    def process(self, event: Event) -> Features:
        pid = event.player_id
        p = event.payload
        self.g.add_node(self._p(pid))
        if "device_id" in p:
            self._link(pid, "device", str(p["device_id"]))
        if "instrument_id" in p:
            self._link(pid, "instrument", str(p["instrument_id"]))
        if "ip" in p:
            sn = subnet_of(str(p["ip"]))
            self.subnet_players[sn].add(pid)
            self.player_subnets[pid].add(sn)
        if event.type == EventType.P2P_TRANSFER:
            self.p2p.add_edge(pid, str(p["counterparty_id"]))
        if event.type == EventType.BET:
            self._record_bet(pid, str(p["market_id"]), str(p["selection"]), event.ts, event.amount)

        shared_subnet = (
            set().union(*(self.subnet_players[s] for s in self.player_subnets[pid]))
            if self.player_subnets[pid]
            else set()
        )
        shared_subnet.discard(pid)
        return {
            "ring_size": self.ring_size(pid),
            "shared_subnet_players": len(shared_subnet),
            "p2p_counterparties": self.p2p.degree(pid) if pid in self.p2p else 0,
            "opposite_bets_linked": self.opposite_hits[pid],
        }

    @staticmethod
    def _mirrors(sel: str, ts: float, stake: float, other: tuple[str, float, float]) -> bool:
        o_sel, o_ts, o_stake = other
        return (
            o_sel != sel
            and abs(o_ts - ts) <= OPPOSITE_BET_WINDOW_S
            and abs(o_stake - stake) <= OPPOSITE_BET_STAKE_TOL * max(stake, o_stake, 1e-9)
        )

    def _record_bet(self, pid: str, market: str, selection: str, ts: float, stake: float) -> None:
        bets = self.market_bets[market]
        bets[pid].append((selection, ts, stake))
        others = [o for o in bets if o != pid]
        if not others:
            return
        linked = self.linked_players(pid)
        for other in others:
            if other in linked and any(self._mirrors(selection, ts, stake, b) for b in bets[other]):
                a, b = sorted((pid, other))
                key = (market, a, b)
                if key not in self._opposite_pairs:
                    self._opposite_pairs.add(key)
                    self.opposite_hits[pid] += 1
                    self.opposite_hits[other] += 1

    def rings(self, min_size: int = 2) -> list[set[str]]:
        out = []
        for comp in nx.connected_components(self.g):
            players = {n[7:] for n in comp if n.startswith("player:")}
            if len(players) >= min_size:
                out.append(players)
        return sorted(out, key=lambda s: (-len(s), sorted(s)))
