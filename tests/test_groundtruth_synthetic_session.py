"""is_synthetic_session() must catch fixture ids STRUCTURALLY, not by word list.

Measured 2026-09-27: of 1,559 test rows in savings_stats, is_synthetic_session()
caught 972 via its old word list (test/demo/mock/...); 587 more — session ids
like "wiring-sess", "phase0-quota-sub-sess", "finalsess", "a01sess" — slipped
past it because none of them contains a listed word. A name list will always
be one fixture behind (CLAUDE.md); the structural fact that survives every new
benchmark author's naming choice is that a real id from this harness (a UUID,
or an 8-hex-character prefix of one) can only contain 0-9, a-f and hyphens.

CHZ-176: that structural rule is itself a false-positive hazard. Measured the
same day against the same table: "gateway" (262 rows) and "sdk" (38 rows) are
REAL production traffic — src/llm_router/route_server.py:80 and
src/llm_router/sdk.py:70 stamp them on purpose — but both are non-hex words,
so the structural rule alone would drop them from every money figure right
alongside the fixtures it was built to catch. `_PRODUCTION_WRITER_SESSION_IDS`
is the fix: the small, closed set of ids OUR OWN code emits, checked first.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth.sources import (  # noqa: E402
    _PRODUCTION_WRITER_SESSION_IDS,
    is_synthetic_session,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact 587-style ids the old word list missed.
_MISSED_FIXTURE_IDS = (
    "wiring-sess",
    "phase0-quota-sub-sess",
    "finalsess",
    "a01sess",
)

# The router's own confirmed production writers of a non-hex session id, and
# the exact source line each is measured against (see sources.py's
# `_PRODUCTION_WRITER_SESSION_IDS` comment for the full trail: file, the
# import chain into savings_stats, and the row counts).
_PRODUCTION_WRITER_IDS = ("gateway", "sdk")


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


def test_production_writer_ids_are_not_synthetic() -> None:
    """The router's own real, non-hex session ids must survive the structural
    rule. Measured 2026-09-27: "gateway" (262 rows) and "sdk" (38 rows) in
    savings_stats are real traffic from src/llm_router/route_server.py:80 and
    src/llm_router/sdk.py:70 respectively — real UUIDs and hex ids were never
    the risk here, a REAL non-hex id was."""
    for sid in _PRODUCTION_WRITER_IDS:
        assert is_synthetic_session(sid) is False, (
            f"{sid!r} is a real production writer id (see "
            f"_PRODUCTION_WRITER_SESSION_IDS in scripts/groundtruth/sources.py) "
            f"and must not be excluded from money figures as a fixture"
        )
        # Case-insensitivity: is_synthetic_session lowercases before comparing.
        assert is_synthetic_session(sid.upper()) is False


def test_production_writer_allowlist_is_exactly_the_measured_set() -> None:
    """Pin the allowlist itself. Growing it silently (without the file:line +
    row-count evidence CLAUDE.md requires) is exactly the failure mode the
    fixture word list had — this makes adding an id to it a diffed, reviewed
    change rather than a drive-by edit."""
    assert _PRODUCTION_WRITER_SESSION_IDS == frozenset(_PRODUCTION_WRITER_IDS)


# ── Guard: a production writer must not silently start emitting a NEW
# non-hex session id. Each regex below finds the exact literal the named
# writer emits TODAY; if that literal ever changes (or a maintainer edits the
# writer to emit a different word) without updating the allowlist in
# sources.py, `is_synthetic_session` will flag it and this test goes red —
# forcing the allowlist to be extended deliberately instead of the row
# silently vanishing from every money figure. ──────────────────────────────

_GATEWAY_WRITER_RE = re.compile(
    r'_log_route_savings\(resp, task_type\.value,\s*'
    r'payload\.get\("complexity"\) or resp\.complexity or "moderate",\s*'
    r'str\(payload\.get\("host"\) or "([a-zA-Z0-9_-]+)"\)',
)

_SDK_WRITER_RE = re.compile(
    r'log_direct_savings\(result=result, task_type=task_type, complexity=complexity,\s*'
    r'session_id="([a-zA-Z0-9_-]+)", host="[a-zA-Z0-9_-]+"\)',
)


def test_gateway_writer_literal_is_still_exempted() -> None:
    path = _REPO_ROOT / "src" / "llm_router" / "route_server.py"
    src = path.read_text()
    m = _GATEWAY_WRITER_RE.search(src)
    assert m, (
        "route_server.py's gateway host-fallback idiom changed shape — update "
        "_GATEWAY_WRITER_RE in this test, then re-check the literal it emits"
    )
    literal = m.group(1)
    assert is_synthetic_session(literal) is False, (
        f"route_server.py now stamps gateway traffic with session_id={literal!r}, "
        f"a NEW non-hex id not in _PRODUCTION_WRITER_SESSION_IDS — add it to the "
        f"allowlist in scripts/groundtruth/sources.py with its row count"
    )


def test_sdk_writer_literal_is_still_exempted() -> None:
    path = _REPO_ROOT / "src" / "llm_router" / "sdk.py"
    src = path.read_text()
    m = _SDK_WRITER_RE.search(src)
    assert m, (
        "sdk.py's log_direct_savings call changed shape — update _SDK_WRITER_RE "
        "in this test, then re-check the literal it emits"
    )
    literal = m.group(1)
    assert is_synthetic_session(literal) is False, (
        f"sdk.py now stamps SDK traffic with session_id={literal!r}, a NEW "
        f"non-hex id not in _PRODUCTION_WRITER_SESSION_IDS — add it to the "
        f"allowlist in scripts/groundtruth/sources.py with its row count"
    )
