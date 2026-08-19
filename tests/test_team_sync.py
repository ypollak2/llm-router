"""Tests for the team-sync export/import round-trip.

Three contracts to lock in:

1. Round-trip preserves cost arithmetic exactly. Exported total spend
   must equal imported total spend; otherwise a manager-level rollup
   can't be trusted.
2. Import is idempotent — re-running with the same JSONL must NOT
   double-count spend. The product promise depends on it.
3. The export schema excludes prompt/response text by default so
   user content never leaves the originating install.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from llm_router.team_sync import (
    EXPORT_REQUIRED,
    EXPORT_SCHEMA,
    export_rows,
    import_rows,
    read_jsonl,
    write_jsonl,
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_source_db(tmp_path: Path) -> Path:
    """Create a routing_decisions table with three sample rows."""
    path = tmp_path / "source.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE routing_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id  TEXT,
                timestamp       TEXT,
                task_type       TEXT,
                profile         TEXT,
                complexity      TEXT,
                subject         TEXT,
                final_model     TEXT,
                final_provider  TEXT,
                input_tokens    INTEGER,
                output_tokens   INTEGER,
                cost_usd        REAL,
                latency_ms      REAL,
                success         INTEGER,
                user_id         TEXT,
                project_id      TEXT,
                prompt_hash     TEXT
            )
            """
        )
        rows = [
            ("c-001", "2026-06-06T10:00:00Z", "query", "balanced", "simple",
             "general", "gemini-2.5-flash", "gemini", 50, 100, 0.0010, 500,
             1, "alice", "ducks", "h1"),
            ("c-002", "2026-06-06T10:05:00Z", "code", "balanced", "complex",
             "code", "qwen-coder", "openrouter", 200, 800, 0.0250, 1500,
             1, "bob", "ducks", "h2"),
            ("c-003", "2026-06-06T10:10:00Z", "analyze", "premium", "complex",
             "reasoning", "claude-sonnet-4-6", "anthropic", 300, 600, 0.0400,
             2000, 1, "alice", "geese", "h3"),
        ]
        conn.executemany(
            "INSERT INTO routing_decisions (correlation_id, timestamp, task_type, "
            "profile, complexity, subject, final_model, final_provider, "
            "input_tokens, output_tokens, cost_usd, latency_ms, success, "
            "user_id, project_id, prompt_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


# ── Export contract ─────────────────────────────────────────────────────


def test_export_emits_one_dict_per_row(tmp_path):
    source = _make_source_db(tmp_path)
    rows = list(export_rows(source))
    assert len(rows) == 3
    # Every row carries the required minimum fields
    for r in rows:
        for required in EXPORT_REQUIRED:
            assert r.get(required) is not None, (
                f"required field {required} missing from row {r}"
            )


def test_export_schema_excludes_prompt_text(tmp_path):
    """The default schema must not surface anything containing prompt
    text or model responses across the install boundary."""
    forbidden = {"prompt", "prompt_text", "response", "response_text",
                 "prompt_hash"}
    assert forbidden.isdisjoint(set(EXPORT_SCHEMA)), (
        f"EXPORT_SCHEMA contains content fields: "
        f"{forbidden & set(EXPORT_SCHEMA)}"
    )


def test_export_since_filter_skips_old_rows(tmp_path):
    source = _make_source_db(tmp_path)
    rows = list(export_rows(source, since="2026-06-06T10:07:00Z"))
    ids = [r["correlation_id"] for r in rows]
    assert ids == ["c-003"], (
        f"since filter should leave only c-003, got {ids}"
    )


def test_export_skips_rows_without_correlation_id(tmp_path):
    """Sidecar-backfilled rows that lack a real correlation_id mustn't
    leak through to the team rollup — their cost is approximate."""
    source = _make_source_db(tmp_path)
    conn = sqlite3.connect(source)
    try:
        conn.execute(
            "INSERT INTO routing_decisions (timestamp, final_model, cost_usd) "
            "VALUES ('2026-06-06T11:00:00Z', 'gemini-2.5-flash', 0.0005)"
        )
        conn.commit()
    finally:
        conn.close()
    rows = list(export_rows(source))
    # Original three rows still present; the corr_id-less row is skipped.
    assert len(rows) == 3


# ── Import round-trip ───────────────────────────────────────────────────


def test_roundtrip_preserves_total_spend(tmp_path):
    source = _make_source_db(tmp_path)
    export_path = tmp_path / "export.jsonl"
    write_jsonl(export_rows(source), export_path)

    team_db = tmp_path / "team.db"
    report = import_rows(team_db, read_jsonl(export_path))
    assert report.scanned == 3
    assert report.inserted == 3
    assert report.duplicate == 0
    assert report.invalid == 0

    # Total spend across all 3 sample rows = 0.0010 + 0.0250 + 0.0400
    expected = 0.0010 + 0.0250 + 0.0400
    conn = sqlite3.connect(team_db)
    try:
        actual = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM team_routing_decisions"
        ).fetchone()[0]
    finally:
        conn.close()
    assert abs(actual - expected) < 1e-9, (
        f"round-trip lost spend: expected {expected}, got {actual}"
    )


def test_import_is_idempotent(tmp_path):
    """Re-running with the same JSONL must NOT double-count spend."""
    source = _make_source_db(tmp_path)
    export_path = tmp_path / "export.jsonl"
    write_jsonl(export_rows(source), export_path)

    team_db = tmp_path / "team.db"
    first = import_rows(team_db, read_jsonl(export_path))
    second = import_rows(team_db, read_jsonl(export_path))

    assert first.inserted == 3
    assert second.inserted == 0
    assert second.duplicate == 3

    conn = sqlite3.connect(team_db)
    try:
        rowcount = conn.execute(
            "SELECT COUNT(*) FROM team_routing_decisions"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rowcount == 3


def test_import_skips_malformed_jsonl_lines(tmp_path):
    """Network blips can chop a JSONL line in half; the importer must
    survive them with `invalid` recorded but everything else preserved."""
    source = _make_source_db(tmp_path)
    export_path = tmp_path / "export.jsonl"
    write_jsonl(export_rows(source), export_path)
    # Inject a half-truncated line at the end + a bare-int line.
    with export_path.open("a", encoding="utf-8") as f:
        f.write('{"correlation_id": "c-999", "timesta')
        f.write("\n")
        f.write("12345\n")

    team_db = tmp_path / "team.db"
    report = import_rows(team_db, read_jsonl(export_path))
    # Malformed line silently dropped by read_jsonl; bare int gets
    # caught by import_rows (no required field → invalid bucket).
    assert report.inserted == 3
    assert report.invalid == 1


def test_import_rejects_rows_missing_required_fields(tmp_path):
    """A row lacking required fields must NOT silently apply a partial
    insert — that would corrupt the team rollup."""
    team_db = tmp_path / "team.db"
    rows_iter = iter([
        # Missing correlation_id
        {"timestamp": "2026-06-06T10:00:00Z",
         "final_model": "gemini-2.5-flash", "cost_usd": 0.001},
        # Missing cost_usd
        {"correlation_id": "c-x", "timestamp": "2026-06-06T10:00:00Z",
         "final_model": "gpt-4o-mini"},
    ])
    report = import_rows(team_db, rows_iter)
    assert report.scanned == 2
    assert report.inserted == 0
    assert report.invalid == 2
