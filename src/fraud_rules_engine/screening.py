"""Sanctions / PEP screening stub.

Screens names against a SMALL SYNTHETIC watchlist shipped with the package
(``data/watchlist_synthetic.csv``). None of the entries are real people and this is not
a substitute for a licensed screening provider or real consolidated lists.

Matching: Unicode/diacritics normalisation, punctuation removal, then rapidfuzz
``token_sort_ratio`` (robust to name-order swaps). If both sides have a date of birth and
they differ, a fixed penalty is applied to reduce false positives on common names.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from rapidfuzz import fuzz, process

HIT_THRESHOLD = 90.0  # >= : treat as a hit (block pending manual confirmation)
REVIEW_THRESHOLD = 80.0  # >= : potential match, manual review
DOB_MISMATCH_PENALTY = 15.0


@dataclass(frozen=True)
class WatchlistEntry:
    entry_id: str
    list_type: str  # SANCTIONS | PEP
    name: str
    dob: str | None
    country: str
    normalized: str


@dataclass(frozen=True)
class ScreeningResult:
    list_type: str
    score: float
    entry_id: str | None
    matched_name: str | None
    reason_code: str | None


def normalize_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", ascii_only.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def load_watchlist(path: str | Path | None = None) -> list[WatchlistEntry]:
    if path is None:
        text = (
            resources.files("fraud_rules_engine").joinpath("data/watchlist_synthetic.csv").read_text(encoding="utf-8")
        )
    else:
        text = Path(path).read_text(encoding="utf-8")
    rows = csv.DictReader(text.splitlines())
    return [
        WatchlistEntry(
            entry_id=r["entry_id"],
            list_type=r["list_type"].upper(),
            name=r["name"],
            dob=r.get("dob") or None,
            country=r.get("country", ""),
            normalized=normalize_name(r["name"]),
        )
        for r in rows
    ]


def reason_code(list_type: str, score: float) -> str | None:
    if score >= HIT_THRESHOLD:
        return f"SCREEN_{list_type}_HIT"
    if score >= REVIEW_THRESHOLD:
        return f"SCREEN_{list_type}_POTENTIAL"
    return None


class Screener:
    def __init__(self, entries: list[WatchlistEntry] | None = None) -> None:
        self.entries = entries if entries is not None else load_watchlist()
        self.by_type: dict[str, list[WatchlistEntry]] = {}
        for e in self.entries:
            self.by_type.setdefault(e.list_type, []).append(e)

    def screen(self, name: str, dob: str | None = None) -> dict[str, ScreeningResult]:
        """Best match per list type."""
        query = normalize_name(name)
        out: dict[str, ScreeningResult] = {}
        for list_type, entries in self.by_type.items():
            best: tuple[float, WatchlistEntry | None] = (0.0, None)
            candidates = process.extract(query, [e.normalized for e in entries], scorer=fuzz.token_sort_ratio, limit=3)
            for _, raw_score, idx in candidates:
                entry = entries[idx]
                score = float(raw_score)
                if dob and entry.dob and dob != entry.dob:
                    score = max(0.0, score - DOB_MISMATCH_PENALTY)
                if score > best[0]:
                    best = (score, entry)
            score, entry = best
            out[list_type] = ScreeningResult(
                list_type=list_type,
                score=round(score, 2),
                entry_id=entry.entry_id if entry else None,
                matched_name=entry.name if entry else None,
                reason_code=reason_code(list_type, score),
            )
        return out
