"""is_synthetic_session() must catch fixture ids STRUCTURALLY, not by word list.

Measured 2026-09-27: of 1,559 test rows in savings_stats, is_synthetic_session()
caught 972 via its old word list (test/demo/mock/...); 587 more — session ids
like "wiring-sess", "phase0-quota-sub-sess", "finalsess", "a01sess" — slipped
past it because none of them contains a listed word. A name list will always
be one fixture behind (CLAUDE.md); the structural fact that survives every new
benchmark author's naming choice is that a real id from this harness (a UUID,
or an 8-hex-character prefix of one) can only contain 0-9, a-f and hyphens.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth.sources import is_synthetic_session  # noqa: E402

# The exact 587-style ids the old word list missed.
_MISSED_FIXTURE_IDS = (
    "wiring-sess",
    "phase0-quota-sub-sess",
    "finalsess",
    "a01sess",
)


def test_previously_missed_fixture_ids_are_now_caught() -> None:
    for sid in _MISSED_FIXTURE_IDS:
        assert is_synthetic_session(sid) is True, (
            f"{sid!r} contains a letter no hex digit can be — it cannot be a "
            f"real UUID/hex-prefix session id and must be caught structurally"
        )


def test_real_uuid_session_ids_are_not_flagged() -> None:
    """Negative control: genuine UUIDs (all hex + hyphens) must survive."""
    real_ids = (
        "b9f04425-6176-4bea-b46e-1cfdfd44785e",
        "550e8400-e29b-41d4-a716-446655440000",
    )
    for sid in real_ids:
        assert is_synthetic_session(sid) is False, (
            f"{sid!r} is a real UUID (only hex digits + hyphens) and must "
            f"not be misclassified as synthetic"
        )


def test_hex_spelled_fixture_words_still_caught() -> None:
    """Existing behaviour preserved: all-hex placeholder words (deadbeef,
    cafebabe) are not caught by the new "contains a non-hex char" rule (they
    ARE all hex), so the dedicated hex-stem check must still run."""
    assert is_synthetic_session("deadbeef-0000-0000-0000-000000000000") is True
    assert is_synthetic_session("cafebabe1234") is True


def test_empty_and_none_are_not_synthetic() -> None:
    assert is_synthetic_session(None) is False
    assert is_synthetic_session("") is False
