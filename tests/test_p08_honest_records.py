"""P0.8 honest records (R-EVL-1, NFR-NUM).

Each test here failed on da31df7:

* DIRECT rows wrote ``classifier_confidence=0.0``, ``classifier_latency_ms=0.0``,
  ``budget_pct_used=0.0``, ``was_downshifted=0`` and ``quality_mode='balanced'``
  for values the hook never measured (live copy: 63/63 DIRECT rows at 0.0), and an
  unknown task type was logged as ``query``.
* ``usage`` had no ``session_id`` column, so no usage row could be scoped to a session.
* The Stop line's north star counted the heuristic ``outcome == used``, while
  ``llm-router kpi`` counts the strict rule (``northstar.is_strict_used``).
* The status bar priced its baseline at Opus and labelled it "vs Sonnet".
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from llm_router.hooks.direct_executor import DirectResult, ModelSpec

_REPO = Path(__file__).resolve().parent.parent
_HOOKS = _REPO / "src" / "llm_router" / "hooks"


def _ollama_result() -> DirectResult:
    return DirectResult(
        text="some answer",
        model=ModelSpec(provider="ollama", model="qwen3.5:latest"),
        latency_ms=6500,
        input_tokens=120,
        output_tokens=60,
    )


def _rows(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ── task 1: DIRECT unknowns are NULL, not defaults ──────────────────────────

def test_direct_row_records_unmeasured_fields_as_null(temp_db):
    from llm_router.hooks.savings_logger import log_direct_to_db

    log_direct_to_db(_ollama_result(), prompt="hello", task_type="code",
                     complexity="simple", classifier_type="heuristic")

    rows = _rows(temp_db, "SELECT classifier_confidence, classifier_latency_ms, budget_pct_used, "
                          "was_downshifted, quality_mode, task_type, reason_code FROM routing_decisions")
    assert rows == [(None, None, None, None, None, "code", "direct")]


def test_direct_unknown_task_type_is_null_with_raw_value(temp_db):
    from llm_router.hooks.savings_logger import log_direct_to_db

    log_direct_to_db(_ollama_result(), prompt="x", task_type="not-a-real-task-type",
                     complexity="moderate", classifier_type="heuristic")

    assert _rows(temp_db, "SELECT task_type, task_type_raw FROM routing_decisions") == [
        (None, "not-a-real-task-type")]
    assert _rows(temp_db, "SELECT task_type, task_type_raw FROM usage") == [
        (None, "not-a-real-task-type")]


def test_direct_known_task_type_leaves_raw_empty(temp_db):
    from llm_router.hooks.savings_logger import log_direct_to_db

    log_direct_to_db(_ollama_result(), prompt="x", task_type="analyze", complexity="moderate")

    assert _rows(temp_db, "SELECT task_type, task_type_raw FROM usage") == [("analyze", None)]


# ── task 2: usage.session_id ────────────────────────────────────────────────

def _columns(db: Path, table: str) -> list[str]:
    return [r[1] for r in _rows(db, f"PRAGMA table_info({table})")]


def test_usage_session_id_migration_is_idempotent(temp_db):
    from llm_router import cost

    async def _open_twice() -> None:
        for _ in range(2):
            db = await cost._get_db()
            await db.close()

    asyncio.run(_open_twice())
    cols = _columns(temp_db, "usage")
    assert cols.count("session_id") == 1
    assert cols.count("task_type_raw") == 1
    assert _columns(temp_db, "routing_decisions").count("task_type_raw") == 1


def test_direct_row_carries_the_payload_session_on_usage(temp_db):
    from llm_router.hooks.savings_logger import log_direct_to_db

    log_direct_to_db(_ollama_result(), prompt="x", task_type="code", complexity="simple",
                     session_id="3f2a9c1e-0000-4000-8000-000000000001")
    log_direct_to_db(_ollama_result(), prompt="x", task_type="code", complexity="simple",
                     session_id="sdk")  # a placeholder, not a session

    assert _rows(temp_db, "SELECT session_id FROM usage ORDER BY id") == [
        ("3f2a9c1e-0000-4000-8000-000000000001",), (None,)]


def test_log_usage_stamps_the_mcp_call_session(temp_db, monkeypatch):
    from llm_router import call_identity, cost
    from llm_router.types import LLMResponse, RoutingProfile, TaskType

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-mcp-42")
    resp = LLMResponse(content="ok", model="gpt-4o-mini", input_tokens=37, output_tokens=11,
                       cost_usd=0.0002, latency_ms=50.0, provider="openai")

    async def _write() -> None:
        await cost.log_usage(resp, TaskType.CODE, RoutingProfile.BALANCED)   # no tool call bound
        token = call_identity.bind("toolu_01ABC")
        try:
            await cost.log_usage(resp, TaskType.CODE, RoutingProfile.BALANCED)
        finally:
            call_identity.reset(token)

    asyncio.run(_write())
    assert _rows(temp_db, "SELECT session_id FROM usage ORDER BY id") == [(None,), ("sess-mcp-42",)]


def test_legacy_usage_table_accepts_null_task_type_after_migration(tmp_path, monkeypatch):
    """A database created before this change has ``usage.task_type NOT NULL``; the
    migration relaxes it once, keeps every row and its id, and is idempotent."""
    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db_path))
    con.execute("""CREATE TABLE usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
        model TEXT NOT NULL, provider TEXT NOT NULL, task_type TEXT NOT NULL,
        profile TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL, latency_ms REAL NOT NULL, success INTEGER NOT NULL DEFAULT 1)""")
    for i in range(5):
        con.execute("INSERT INTO usage (model, provider, task_type, profile, input_tokens, "
                    "output_tokens, cost_usd, latency_ms) VALUES ('m', 'p', 'code', 'balanced', ?, 1, 0, 1)",
                    (i,))
    con.execute("DELETE FROM usage WHERE id = 5")   # the sequence must survive the rebuild
    con.commit()
    con.close()
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    from llm_router import cost

    async def _open_twice() -> None:
        for _ in range(2):
            db = await cost._get_db()
            await db.close()

    asyncio.run(_open_twice())
    info = {r[1]: r[3] for r in _rows(db_path, "PRAGMA table_info(usage)")}
    assert info["task_type"] == 0 and info["model"] == 1
    assert _rows(db_path, "SELECT id, input_tokens FROM usage ORDER BY id") == [
        (1, 0), (2, 1), (3, 2), (4, 3)]
    assert _rows(db_path, "SELECT name FROM sqlite_master WHERE name LIKE 'usage%' AND type='table'") == [
        ("usage",)]
    idx = {r[0] for r in _rows(db_path, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='usage'")}
    assert {"idx_usage_provider_ts", "idx_usage_model_ts"} <= idx
    con = sqlite3.connect(str(db_path))
    con.execute("INSERT INTO usage (model, provider, task_type, profile, input_tokens, output_tokens, "
                "cost_usd, latency_ms) VALUES ('m', 'p', NULL, 'balanced', 9, 1, 0, 1)")
    con.commit()
    assert con.execute("SELECT max(id) FROM usage").fetchone() == (6,)
    con.close()


def _load_hook(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, _HOOKS / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cc_usage_track_writes_the_payload_session(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    hook = _load_hook("cc-usage-track.py", "_p08_cc_usage_track")
    payload = {"session_id": "9b1d7c2e-1111-4000-8000-000000000002", "tool_name": "Agent",
               "tool_input": {"subagent_type": "Explore", "prompt": "p" * 40},
               "tool_response": {"output": "r" * 40}, "duration_ms": 1200}
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit):
        hook.main()
    assert _rows(home / "usage.db", "SELECT provider, session_id FROM usage") == [
        ("cc", "9b1d7c2e-1111-4000-8000-000000000002")]


# ── task 4: one NS rule on user surfaces; status-bar label ──────────────────

def _unit(sid: str, i: int, *, verified: bool) -> dict:
    return {"session_id": sid, "kind": "routed_mcp", "lever": "mcp", "outcome": "used",
            "model": "qwen3-coder:30b", "task_type": "code", "ts": 1_800_000_000.0 + i,
            "verify": {"verify_status": "pass_f2p"} if verified else None}


def test_stop_line_north_star_uses_the_strict_rule(monkeypatch):
    from llm_router import northstar

    sid = "sess-ns"
    units = [_unit(sid, i, verified=i < 10) for i in range(northstar.MIN_UNITS)]
    monkeypatch.setattr(northstar, "units", lambda **kw: iter(units))
    # 50 units, all heuristic "used", 10 pass the strict rule: 20%, not 100%.
    assert northstar.current_session_line(sid) == "north star (strict) 20% (n=50)"
    strict = sum(northstar.is_strict_used(u) for u in units)
    assert strict == 10


def test_status_bar_baseline_label_names_the_priced_model():
    hook = _load_hook("status-bar.py", "_p08_status_bar")
    out = hook._savings_str_full({"today": (1.0, 2.0), "week": (1.0, 2.0), "month": (5.0, 58.0),
                                  "session": (0.0, 0.0)})
    assert "vs Sonnet" not in out
    assert f"vs {hook.HOST_BASELINE_LABEL}:$58" in out
    assert hook.HOST_BASELINE_LABEL == "Opus" and hook.HOST_BASELINE_TIER == "opus"


def test_cc_usage_track_session_rule_matches_call_identity():
    from llm_router import call_identity

    hook = _load_hook("cc-usage-track.py", "_p08_cc_usage_track_rule")
    for value in ("abc-123", "  9b1d7c2e-1111-4000-8000-000000000002 ", "sdk", "Unknown", "",
                  "has space", "x" * 129, None, 42, "toolu_01:ABC.def"):
        assert hook._ledger_session_id(value) == call_identity.ledger_session_id(value), value


# ── G3 counts NULL as missing (P0.8 task 3) ──────────────────────────────────


def test_g3_counts_null_as_missing_on_every_required_field():
    """A NULL in a field G3 requires is "missing", never "recorded".

    Only the outcome fields that are null by design (``tier_retry``: no retry
    happened; ``cls_ms``/``cls_arm``: classifier not run yet) are presence-only.
    This already held on da31df7; the test pins it now that P0.8 writes NULL for
    unknowns instead of defaults.
    """
    from llm_router.commands import kpi

    presence_only = kpi._G3_PRESENCE_ONLY | kpi._G3_LATE_PRESENCE_ONLY
    value_fields = [f for f in kpi.G3_FIELDS + kpi.G3_LATE_FIELDS if f not in presence_only]
    assert len(value_fields) >= 9, value_fields  # the check found something to check
    for f in value_fields:
        assert not kpi._g3_recorded({f: None}, f, presence_only=presence_only), f
        assert not kpi._g3_recorded({}, f, presence_only=presence_only), f
        assert kpi._g3_recorded({f: "x"}, f, presence_only=presence_only), f
    for f in presence_only:
        assert kpi._g3_recorded({f: None}, f, presence_only=presence_only), f
        assert not kpi._g3_recorded({}, f, presence_only=presence_only), f


# ── replay renders NULL as unknown (BUGS.md 26) ──────────────────────────────


def test_replay_renders_null_confidence_and_task_type_as_unknown():
    """P0.8 stores unmeasured confidence and unknown task types as NULL.

    ``format_decision_line`` multiplied ``classifier_confidence`` by 100 and raised
    TypeError on a NULL row (and printed ``None`` for a NULL task type).
    """
    from llm_router.commands import replay

    row = {"timestamp": "2026-10-07 17:29:30", "task_type": None, "complexity": "moderate",
           "final_model": "fake-smoke:1b", "classifier_confidence": None,
           "reason_code": "direct", "cost_usd": 0.0}
    text = replay.format_decision_line(row)
    assert "Confidence: unknown" in text
    assert "(unknown/moderate)" in text
    assert "None" not in text

    measured = dict(row, task_type="code", classifier_confidence=0.87)
    text = replay.format_decision_line(measured)
    assert "87%" in text and "(code/moderate)" in text


# ── readers of task_type survive NULL (review of #310, round 1) ─────────────


def test_quality_report_renders_null_task_type_as_unknown(temp_db):
    """P0.8 writes NULL task_type for an unknown DIRECT task type.

    ``llm_quality_report`` formatted each task type with ``{task:<16}`` and raised
    ``TypeError: unsupported format string passed to NoneType.__format__`` as soon
    as one such row was in its window (reproduced on a copy of the live usage.db).
    """
    from llm_router.hooks.savings_logger import log_direct_to_db
    from llm_router.tools.admin import llm_quality_report

    log_direct_to_db(_ollama_result(), prompt="x", task_type="not_a_task_type_xyz",
                     complexity="moderate", classifier_type="heuristic")
    log_direct_to_db(_ollama_result(), prompt="y", task_type="code",
                     complexity="simple", classifier_type="heuristic")
    assert _rows(temp_db, "SELECT COUNT(*) FROM routing_decisions WHERE task_type IS NULL") == [(1,)]
    # Under pytest the writer stamps provenance='test', which the report filters
    # out. Mark the rows as real traffic, as they are on the live copy.
    con = sqlite3.connect(str(temp_db))
    con.execute("UPDATE routing_decisions SET provenance = 'runtime'")
    con.commit()
    con.close()

    text = asyncio.run(llm_quality_report(days=7))
    assert "BY TASK TYPE" in text  # the check found the rows it was meant to render
    assert "  unknown " in text and "  code " in text
    assert "None" not in text


def test_session_end_routing_panels_render_null_task_type(monkeypatch):
    """The Stop summary's routing panels read ``usage.task_type`` and formatted it
    with ``{tool:<12}`` / ``{top_task:<12}``: a NULL row raised TypeError."""
    hook = _load_hook("session-end.py", "_p08_session_end")
    row = {"task_type": None, "model": "openai/gpt-4o", "provider": "openai",
           "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}

    tools = hook._aggregate([row])
    assert list(tools) == ["unknown"]
    paid = "\n".join(hook._format_routing_section(tools, subscription=False))
    assert "unknown" in paid and "None" not in paid

    cc = "\n".join(hook._format_cc_model_section([dict(row, provider="subscription")]))
    assert "gpt-4o" in cc and "None" not in cc


def test_northstar_cli_shows_the_strict_rule_as_the_north_star(monkeypatch, capsys):
    """Plan §1.2: the heuristic NS leaves user surfaces. ``llm-router northstar``
    printed the heuristic ``outcome == used`` share as the North Star."""
    from llm_router import northstar
    from llm_router.commands import northstar as cli

    sid = "sess-cli"
    units = [_unit(sid, i, verified=i < 10) for i in range(northstar.MIN_UNITS)]
    monkeypatch.setattr(northstar, "units", lambda **kw: iter(list(units)))
    monkeypatch.setattr("llm_router.quality_breaker.open_classes", lambda: [])

    assert cli.cmd_northstar(["--days", "7"]) == 0
    out = capsys.readouterr().out
    ns_lines = [ln for ln in out.splitlines() if ln.startswith("  sess-cli")]
    assert len(ns_lines) == 1
    # 50 units, all heuristic "used", 10 pass the strict rule: the NS is 20%.
    assert "verified offload 20.0%" in ns_lines[0]
    assert "median=20.0%" in out and "max=20.0%" in out
    # The heuristic is still printed, but only under a diagnostic label.
    assert "100.0% used" not in ns_lines[0]
    assert "diagnostic: heuristic used=100.0%" in ns_lines[0]
    assert "diagnostic (heuristic routed-and-used, not the NS): median=100.0%" in out
    assert "routed-and-used share" not in out.splitlines()[0]

    data = northstar.report(days=7)
    assert data["aggregate"]["strict_median"] == pytest.approx(0.2)
    assert data["aggregate"]["median"] == pytest.approx(1.0)


# ── NULL task type in the claw-code Stop hook and the dashboard (BUGS.md 29) ──

def _seed_usage(db: Path, rows: list[tuple]) -> None:
    """A minimal ``usage`` table with rows stamped now (UTC), as the readers query it."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY, timestamp TEXT, success INTEGER, "
                "task_type TEXT, model TEXT, provider TEXT, input_tokens INTEGER, "
                "output_tokens INTEGER, cost_usd REAL)")
    con.executemany("INSERT INTO usage (timestamp, success, task_type, model, provider, "
                    "input_tokens, output_tokens, cost_usd) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                    [(now, *r) for r in rows])
    con.commit()
    con.close()


def test_clawcode_stop_hook_renders_null_task_type(tmp_path, monkeypatch, capsys):
    """``session-end-clawcode.py`` read ``r.get("task_type", "unknown")`` and formatted it
    with ``{tool:<12}``: a paid row with a NULL task type made the Stop hook exit 1."""
    import time

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    (home / "session_start.txt").write_text(str(time.time() - 600))
    _seed_usage(home / "usage.db", [
        (None, "openai/gpt-4o", "openai", 100, 50, 0.01),
        (None, "openai/gpt-4o", "openai", 100, 50, 0.01),
        ("code", "openai/gpt-4o", "openai", 100, 50, 0.01),
    ])
    hook = _load_hook("session-end-clawcode.py", "_p08_session_end_clawcode")

    paid, _free = hook._query_session_data(hook._read_session_start())
    assert sum(r["task_type"] is None for r in paid) == 2  # the check sees the NULL rows

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    hook.main()  # raised TypeError before the fix
    summary = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "  unknown " in summary and "  code " in summary
    assert "None" not in summary


def test_dashboard_last_prompt_calls_render_null_task_type(tmp_path):
    """``query_last_prompt_calls`` passed a NULL ``usage.task_type`` through as None, and
    the cyber-grid models panel formats it with ``{c['task_type']:<10}``."""
    from rich.console import Console

    from llm_router.hooks import cyber_grid
    from llm_router.hooks.dashboard_enhanced import query_last_prompt_calls

    db = tmp_path / "usage.db"
    _seed_usage(db, [(None, "openai/gpt-4o", "openai", 100, 50, 0.01)])

    calls = query_last_prompt_calls(db_path=db)
    assert [c["task_type"] for c in calls] == ["unknown"]

    panel = cyber_grid._build_models_panel({"db_path": str(db)})
    assert panel is not None
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(panel)
    text = console.export_text()
    assert "unknown" in text and "None" not in text
