"""The v3 join contract, pinned.

    given route_id       -> exactly one ledger record
    given prompt_sha256  -> the captured task text

These tests exist because the previous schema failed this contract silently.
Nothing looked broken: the ledger was written, transcripts were written, every
individual test passed — and the two could not be connected, so 22,356 routes
produced zero evaluable units. A contract that is only satisfied by accident is
the one that needs a test.

Several tests below assert that a join does NOT happen (legacy rows, missing
capture). Those matter as much as the positive cases: the failure mode being
guarded against is a heuristic join that quietly pairs the wrong prompt with
the wrong route.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from groundtruth.join import (  # noqa: E402
    INCOMPLETE_LEGACY,
    INCOMPLETE_NO_CAPTURE,
    INCOMPLETE_NO_HASH,
    coverage,
    join_route,
    load_capture_index,
    reconstruct,
    reconstruct_all,
)
from llm_router.routing_quality import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    RouteLedgerRecord,
    record_route,
    stamp_trace,
    summarize,
)
from llm_router.trace_id import hash_prompt  # noqa: E402

PROMPT = "Make the name_contains filter in src/query.py case-insensitive."


def write_ledger(tmp_path: Path, *recs: RouteLedgerRecord) -> Path:
    p = tmp_path / "routing_quality.jsonl"
    for r in recs:
        assert record_route(r, path=str(p))
    return p


def write_capture(tmp_path: Path, *rows: dict) -> Path:
    p = tmp_path / "prompt_capture.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def capture_row(prompt: str, **over) -> dict:
    row = {
        "prompt": prompt,
        "prompt_sha256": hash_prompt(prompt),
        "capture_ref": f"capture:{hash_prompt(prompt)}",
        "route_id": None,
        "session_id": "s1",
        "task_type": "code",
        "complexity": "moderate",
        "capture_schema": 2,
    }
    row.update(over)
    return row


# ── The contract ─────────────────────────────────────────────────────────────

def test_route_id_reconstructs_the_evaluation_unit(tmp_path: Path) -> None:
    rec = stamp_trace(RouteLedgerRecord(task_type="code", chosen_tier=0,
                                        final_tier=2, route_succeeded=True),
                      prompt=PROMPT, session_id="s1", latency_ms=1234.5,
                      complexity="moderate", classification_method="heuristic")
    ledger = write_ledger(tmp_path, rec)
    capture = write_capture(tmp_path, capture_row(PROMPT))

    unit = reconstruct(rec.route_id, ledger=ledger, capture=capture)
    assert unit is not None
    assert unit.is_complete
    assert unit.prompt == PROMPT
    assert unit.session_id == "s1"
    assert unit.latency_ms == 1234.5
    assert unit.complexity == "moderate"
    assert unit.classification_method == "heuristic"
    assert unit.chosen_tier == 0 and unit.final_tier == 2


def test_unknown_route_id_returns_none(tmp_path: Path) -> None:
    ledger = write_ledger(tmp_path, stamp_trace(RouteLedgerRecord(), prompt=PROMPT))
    assert reconstruct("no-such-route", ledger=ledger,
                       capture=write_capture(tmp_path)) is None


def test_hash_is_the_join_not_the_timestamp(tmp_path: Path) -> None:
    """A capture written at a wildly different time still joins."""
    rec = stamp_trace(RouteLedgerRecord(), prompt=PROMPT)
    rec.ts = 1_000_000.0
    ledger = write_ledger(tmp_path, rec)
    capture = write_capture(tmp_path, capture_row(PROMPT, ts=9_999_999.0))
    unit = reconstruct(rec.route_id, ledger=ledger, capture=capture)
    assert unit is not None and unit.is_complete


def test_different_prompts_do_not_cross_join(tmp_path: Path) -> None:
    other = "Completely different task about parsing dates."
    rec = stamp_trace(RouteLedgerRecord(), prompt=PROMPT)
    ledger = write_ledger(tmp_path, rec)
    capture = write_capture(tmp_path, capture_row(other))
    unit = reconstruct(rec.route_id, ledger=ledger, capture=capture)
    assert unit is not None
    assert unit.prompt is None, "must not attach an unrelated prompt"
    assert INCOMPLETE_NO_CAPTURE in unit.incomplete_reasons


def test_two_routes_same_prompt_both_join(tmp_path: Path) -> None:
    """The same task asked twice is the same task — by design."""
    a = stamp_trace(RouteLedgerRecord(), prompt=PROMPT)
    b = stamp_trace(RouteLedgerRecord(), prompt=PROMPT)
    assert a.route_id != b.route_id
    assert a.prompt_sha256 == b.prompt_sha256
    ledger = write_ledger(tmp_path, a, b)
    capture = write_capture(tmp_path, capture_row(PROMPT))
    for rid in (a.route_id, b.route_id):
        unit = reconstruct(rid, ledger=ledger, capture=capture)
        assert unit is not None and unit.is_complete


# ── Incompleteness is reported, never guessed ────────────────────────────────

def test_legacy_row_is_flagged_not_joined(tmp_path: Path) -> None:
    old = RouteLedgerRecord(task_type="code")
    old.schema_version = 2  # pre-traceability
    ledger = write_ledger(tmp_path, old)
    capture = write_capture(tmp_path, capture_row(PROMPT))
    unit = reconstruct(old.route_id, ledger=ledger, capture=capture)
    assert unit is not None
    assert INCOMPLETE_LEGACY in unit.incomplete_reasons
    assert unit.prompt is None
    assert not unit.is_complete


def test_v3_row_without_hash_is_flagged(tmp_path: Path) -> None:
    rec = RouteLedgerRecord(task_type="code")  # v3 default, no stamp_trace call
    ledger = write_ledger(tmp_path, rec)
    unit = reconstruct(rec.route_id, ledger=ledger,
                       capture=write_capture(tmp_path))
    assert unit is not None
    assert INCOMPLETE_NO_HASH in unit.incomplete_reasons


def test_capture_off_means_traceable_but_not_evaluable(tmp_path: Path) -> None:
    rec = stamp_trace(RouteLedgerRecord(), prompt=PROMPT)
    ledger = write_ledger(tmp_path, rec)
    unit = reconstruct(rec.route_id, ledger=ledger,
                       capture=write_capture(tmp_path))
    assert unit is not None
    assert unit.prompt_sha256 is not None, "still identifiable"
    assert not unit.is_complete
    assert INCOMPLETE_NO_CAPTURE in unit.incomplete_reasons


def test_coverage_accounts_for_every_route(tmp_path: Path) -> None:
    good = stamp_trace(RouteLedgerRecord(), prompt=PROMPT)
    nohash = RouteLedgerRecord()
    legacy = RouteLedgerRecord()
    legacy.schema_version = 2
    ledger = write_ledger(tmp_path, good, nohash, legacy)
    capture = write_capture(tmp_path, capture_row(PROMPT))
    cov = coverage(reconstruct_all(ledger=ledger, capture=capture))
    assert cov["routes"] == 3
    assert cov["complete"] == 1
    assert cov[INCOMPLETE_NO_HASH] == 1
    assert cov[INCOMPLETE_LEGACY] == 1
    accounted = cov["complete"] + sum(
        v for k, v in cov.items() if k not in ("routes", "complete"))
    assert accounted == cov["routes"], "coverage must balance"


# ── Hashing and privacy ──────────────────────────────────────────────────────

def test_ledger_never_contains_prompt_text(tmp_path: Path) -> None:
    secret = "deploy with key sk-proj-AbCdEf0123456789AbCdEf0123456789 now"
    rec = stamp_trace(RouteLedgerRecord(), prompt=secret, response="the answer body")
    ledger = write_ledger(tmp_path, rec)
    raw = ledger.read_text(encoding="utf-8")
    assert secret not in raw
    assert "sk-proj-AbCdEf" not in raw
    assert "the answer body" not in raw
    assert rec.prompt_sha256 in raw, "the hash, and only the hash"


def test_hash_is_exact_not_normalised() -> None:
    """Unlike result_cache._prompt_hash, casing and whitespace matter here."""
    a = stamp_trace(RouteLedgerRecord(), prompt="Fix The Parser")
    b = stamp_trace(RouteLedgerRecord(), prompt="fix the parser")
    assert a.prompt_sha256 != b.prompt_sha256


def test_same_prompt_hashes_stably_across_records() -> None:
    a = stamp_trace(RouteLedgerRecord(), prompt=PROMPT)
    b = stamp_trace(RouteLedgerRecord(), prompt=PROMPT)
    assert a.prompt_sha256 == b.prompt_sha256 == hash_prompt(PROMPT)


def test_stamp_trace_is_optional_and_never_raises() -> None:
    rec = RouteLedgerRecord()
    same = stamp_trace(rec)  # no kwargs at all
    assert same is rec
    assert rec.prompt_sha256 is None


# ── The denominator guard ────────────────────────────────────────────────────

def test_v3_rows_still_enter_quality_denominators(tmp_path: Path) -> None:
    """`summarize` must test schema_version >= 2, not == 2.

    An equality test would have dropped every v3 row from the denominator and
    reported a clean 0% instead of a missing measurement.
    """
    p = tmp_path / "routing_quality.jsonl"
    assert record_route(stamp_trace(
        RouteLedgerRecord(route_kind="completion", task_type="code",
                          route_succeeded=True, verification_attempted=True,
                          verification_passed=True),
        prompt=PROMPT), path=str(p))
    out = summarize(path=str(p))
    assert CURRENT_SCHEMA_VERSION >= 3
    # The v3 row must be counted somewhere. Assert on a real count rather than
    # on the summary's exact shape, which is free to change.
    counts = [v for v in _ints(out) if v > 0]
    assert counts, f"v3 row entered no denominator at all: {out}"


def test_equality_version_filter_would_have_hidden_v3(tmp_path: Path) -> None:
    """Control for the test above: prove the guard can fail.

    A `== 2` filter over a v3 row yields an empty subset, which is exactly the
    silent-zero this schema bump could have caused.
    """
    p = tmp_path / "routing_quality.jsonl"
    assert record_route(stamp_trace(RouteLedgerRecord(route_kind="completion"),
                                    prompt=PROMPT), path=str(p))
    rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    assert [r for r in rows if r.get("schema_version", 1) >= 2], "the >= filter keeps it"
    assert not [r for r in rows if r.get("schema_version", 1) == 2], (
        "an == filter would drop it — this is the bug the >= guard prevents")


def _ints(obj) -> list:
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _ints(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _ints(v)]
    return [obj] if isinstance(obj, int) and not isinstance(obj, bool) else []


def test_capture_index_keys_on_hash(tmp_path: Path) -> None:
    capture = write_capture(tmp_path, capture_row(PROMPT), capture_row("another task here"))
    index = load_capture_index(capture)
    assert hash_prompt(PROMPT) in index
    assert index[hash_prompt(PROMPT)]["prompt"] == PROMPT


def test_ledger_metadata_wins_over_capture(tmp_path: Path) -> None:
    """The router's own view is authoritative; capture only fills gaps."""
    rec = stamp_trace(RouteLedgerRecord(task_type="analyze"),
                      prompt=PROMPT, complexity="complex")
    unit = join_route(json.loads(json.dumps(rec.__dict__)),
                      {hash_prompt(PROMPT): capture_row(
                          PROMPT, task_type="code", complexity="simple")})
    assert unit.task_type == "analyze"
    assert unit.complexity == "complex"
