from __future__ import annotations

from fraud_rules_engine.screening import (
    HIT_THRESHOLD,
    REVIEW_THRESHOLD,
    Screener,
    load_watchlist,
    normalize_name,
    reason_code,
)


def test_watchlist_is_synthetic() -> None:
    entries = load_watchlist()
    assert entries and all(e.entry_id.startswith("SYN-") for e in entries)


def test_normalization() -> None:
    assert normalize_name("  Hendrík  VAN-Oosterbrug ") == "hendrik van oosterbrug"


def test_hits_variants_and_misses() -> None:
    s = Screener()
    assert s.screen("Oskar Brennholt")["SANCTIONS"].reason_code == "SCREEN_SANCTIONS_HIT"
    swapped = s.screen("BRENNHOLT, Oskar")["SANCTIONS"]
    assert swapped.score >= HIT_THRESHOLD and swapped.entry_id == "SYN-S-004"
    assert s.screen("Oskar Brenholt")["SANCTIONS"].score >= HIT_THRESHOLD  # typo
    assert s.screen("Maria Lopez")["SANCTIONS"].reason_code is None
    assert s.screen("Svetlana Orlitskaya")["PEP"].reason_code == "SCREEN_PEP_HIT"


def test_dob_mismatch_penalty_demotes_hit() -> None:
    s = Screener()
    same = s.screen("Oskar Brennholt", dob="1980-01-30")["SANCTIONS"]
    other = s.screen("Oskar Brennholt", dob="1999-05-05")["SANCTIONS"]
    assert same.score == 100
    assert REVIEW_THRESHOLD <= other.score < HIT_THRESHOLD
    assert other.reason_code == "SCREEN_SANCTIONS_POTENTIAL"


def test_reason_code_bands() -> None:
    assert reason_code("PEP", 95) == "SCREEN_PEP_HIT"
    assert reason_code("PEP", 85) == "SCREEN_PEP_POTENTIAL"
    assert reason_code("PEP", 50) is None
