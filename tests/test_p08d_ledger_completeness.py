"""P0.8-d: every ``usage`` and ``routing_decisions`` writer records session_id and reason.

PLAN v16 D-29 = A (R-EVL-1, NFR-NUM). On 2026-10-09 the live per-writer G3 (P0.8-c) FAILED:
``usage`` had no ``reason`` column (0% complete, n=645), and ``routing_decisions`` rows
reached the ledger with ``reason_code`` NULL. Nothing in the test suite noticed a writer
that skipped a field, so these tests enumerate the writers instead of trusting a list.

* The enumeration scans src/, hooks/ and scripts/ for every ``INSERT ... INTO usage`` /
  ``INTO routing_decisions`` and compares it with the known set. A new writer fails the
  set equality until it is listed, and fails the column check unless it names the fields.
* An AST pass checks every call into the two ``cost`` writers passes the field.
* Migration, writer and completeness behaviour run on real SQLite files.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCAN = [_REPO / "src", _REPO / "hooks", _REPO / "scripts"]

_INSERT = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(usage|routing_decisions)\s*\(([^)]*)\)", re.I)

#: (path relative to the repo, table) -> the columns its INSERT must name.
_USAGE_FIELDS = {"session_id", "reason"}
_ROUTING_FIELDS = {"session_id", "reason_code"}
EXPECTED_WRITERS = {
    ("src/llm_router/cost.py", "usage"): _USAGE_FIELDS,                       # log_usage
    ("hooks/cc-usage-track.py", "usage"): _USAGE_FIELDS,
    ("src/llm_router/hooks/cc-usage-track.py", "usage"): _USAGE_FIELDS,
    ("src/llm_router/cost.py", "routing_decisions"): _ROUTING_FIELDS,         # log_routing_decision
    ("scripts/backfill_sidecars.py", "routing_decisions"): _ROUTING_FIELDS,
    ("scripts/judge_discrimination_eval.py", "routing_decisions"): _ROUTING_FIELDS,
}
#: lineage.db has its own ``routing_decisions`` table (decision_id, operation, ...), not
#: the usage.db table the G3 check reads. Listed so it is a decision, not an oversight.
OTHER_DATABASE = {("src/llm_router/lineage/lineage_store.py", "routing_decisions")}


def _found() -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    for root in _SCAN:
        for path in root.rglob("*.py"):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            for m in _INSERT.finditer(path.read_text(encoding="utf-8")):
                cols = {c.strip() for c in m.group(2).split(",") if c.strip()}
                out.setdefault((str(path.relative_to(_REPO)), m.group(1).lower()), set()).update(cols)
    return out


def test_the_writer_scan_finds_something():
    """An empty scan would pass every assertion below."""
    assert len(_found()) >= 7


def test_every_usage_and_routing_decisions_writer_is_known():
    found = _found()
    unknown = set(found) - set(EXPECTED_WRITERS) - OTHER_DATABASE
    assert not unknown, (
        f"new writer(s) into usage / routing_decisions: {sorted(unknown)}. Add them to "
        "EXPECTED_WRITERS with the session and reason columns, or P0.8-d's G3 check fails live.")
    missing = set(EXPECTED_WRITERS) - set(found)
    assert not missing, f"a listed writer no longer exists: {sorted(missing)}"


@pytest.mark.parametrize("key", sorted(EXPECTED_WRITERS))
def test_each_writer_names_session_and_reason(key):
    absent = EXPECTED_WRITERS[key] - _found()[key]
    assert not absent, f"{key[0]} INSERT INTO {key[1]} does not write {sorted(absent)}"


def test_the_other_database_exemption_is_really_another_schema():
    cols = _found()[("src/llm_router/lineage/lineage_store.py", "routing_decisions")]
    assert "decision_id" in cols and "session_id" not in cols


def _calls(path: Path, names: set[str]) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in names:
                hits.append(node)
    return hits


def test_every_call_into_the_cost_writers_passes_the_field():
    usage_names = {"log_usage", "_cost_log_usage"}
    routing_names = {"_cost_log_routing_decision"}
    seen_usage = seen_routing = 0
    for path in (_REPO / "src").rglob("*.py"):
        if path.name == "cost.py":
            continue
        rel = path.relative_to(_REPO)
        for call in _calls(path, usage_names):
            seen_usage += 1
            kws = {k.arg for k in call.keywords}
            assert "reason" in kws, f"{rel}:{call.lineno} log_usage() without reason="
        for call in _calls(path, routing_names):
            seen_routing += 1
            kws = {k.arg for k in call.keywords}
            assert {"reason_code", "session_id"} <= kws, f"{rel}:{call.lineno} {kws}"
    # router.py calls cost.log_routing_decision as an attribute of ``cost``.
    for call in _calls(_REPO / "src/llm_router/router.py", {"log_routing_decision"}):
        seen_routing += 1
        kws = {k.arg for k in call.keywords}
        assert {"reason_code", "session_id"} <= kws, kws
        reason = next(k.value for k in call.keywords if k.arg == "reason_code")
        assert "REASON_" in ast.unparse(reason)  # a fallback code, never bare .get()
    assert seen_usage >= 6 and seen_routing >= 2


def test_reason_codes_are_short_identifiers_never_prompt_text():
    from llm_router import cost

    codes = {v for k, v in vars(cost).items() if k.startswith("REASON_")}
    assert len(codes) >= 10
    assert all(re.fullmatch(r"[a-z][a-z_]{2,40}", c) for c in codes), codes


# ── migration: an old-schema database ────────────────────────────────────────

_OLD_USAGE = """CREATE TABLE usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
    model TEXT NOT NULL, provider TEXT NOT NULL, task_type TEXT, profile TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
    latency_ms REAL NOT NULL, success INTEGER NOT NULL DEFAULT 1)"""


def _cols(db: Path, table: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def test_migration_adds_usage_reason_to_an_old_database_and_reports_old_rows_unknown(
        tmp_path, monkeypatch):
    from llm_router import cost
    import llm_router.config as config_module

    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    con.execute(_OLD_USAGE)
    for i in range(3):
        con.execute("INSERT INTO usage (model, provider, task_type, profile, input_tokens, "
                    "output_tokens, cost_usd, latency_ms) VALUES ('m','p','code','balanced',1,1,0,1)")
    con.commit()
    con.close()
    assert "reason" not in _cols(db_path, "usage")

    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    config_module._config = None

    async def _open_twice():
        for _ in range(2):
            db = await cost._get_db()
            await db.close()

    asyncio.run(_open_twice())
    assert _cols(db_path, "usage").count("reason") == 1
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT count(*), sum(reason IS NULL) FROM usage").fetchone() == (3, 3)
    con.close()
    config_module._config = None


# ── the writers, end to end, then the completeness check ─────────────────────

def _resp(model="gpt-4o-mini"):
    from llm_router.types import LLMResponse

    return LLMResponse(content="ok", model=model, input_tokens=37, output_tokens=11,
                       cost_usd=0.0002, latency_ms=50.0, provider="openai")


def test_log_usage_writes_reason_and_none_is_null(temp_db):
    from llm_router import cost
    from llm_router.types import RoutingProfile, TaskType

    async def _go():
        await cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.BALANCED,
                             reason=cost.REASON_ROUTER_CHAIN)
        await cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.BALANCED)

    asyncio.run(_go())
    con = sqlite3.connect(temp_db)
    assert con.execute("SELECT reason FROM usage ORDER BY id").fetchall() == [
        ("router_chain",), (None,)]
    con.close()


def test_log_routing_decision_via_the_router_records_reason_and_session(temp_db, monkeypatch):
    """The MCP router's writer, called the way router.py calls it, with the fallback code."""
    from llm_router import cost

    async def _go():
        await cost.log_routing_decision(
            prompt="SECRET PROMPT TEXT", task_type="code", profile="balanced",
            classifier_type="heuristic", classifier_model=None, classifier_confidence=0.9,
            classifier_latency_ms=1.0, complexity="simple", recommended_model="m",
            base_model="m", was_downshifted=False, budget_pct_used=0.0, quality_mode="balanced",
            final_model="m", final_provider="openai", success=True, input_tokens=1,
            output_tokens=1, cost_usd=0.0, latency_ms=1.0,
            reason_code=cost.REASON_ROUTER_CHAIN, session_id="sess-1")

    asyncio.run(_go())
    con = sqlite3.connect(temp_db)
    row = con.execute("SELECT session_id, reason_code FROM routing_decisions").fetchone()
    assert row == ("sess-1", "router_chain")
    # No prompt text in any column of either table.
    for table in ("usage", "routing_decisions"):
        for r in con.execute(f"SELECT * FROM {table}"):
            assert not any(isinstance(v, str) and "SECRET PROMPT" in v for v in r)
    con.close()


def test_cc_usage_track_hook_writes_reason_on_an_old_database(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    spec = importlib.util.spec_from_file_location(
        "_p08d_cc", _REPO / "src/llm_router/hooks/cc-usage-track.py")
    hook = importlib.util.module_from_spec(spec)
    sys.modules["_p08d_cc"] = hook
    spec.loader.exec_module(hook)
    # The table as the hook created it before P0.8-d: no reason column.
    con = sqlite3.connect(home / "usage.db")
    hook._ensure_table(con)
    con.execute("ALTER TABLE usage DROP COLUMN reason")
    con.commit()
    con.close()
    payload = {"session_id": "9b1d7c2e-1111-4000-8000-000000000002", "tool_name": "Agent",
               "tool_input": {"subagent_type": "Explore", "prompt": "p" * 40},
               "tool_response": {"output": "r" * 40}, "duration_ms": 1200}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit):
        hook.main()
    con = sqlite3.connect(home / "usage.db")
    assert con.execute("SELECT session_id, reason FROM usage").fetchall() == [
        ("9b1d7c2e-1111-4000-8000-000000000002", "claude_code_subscription")]
    con.close()


def test_sidecar_backfill_and_judge_eval_rows_carry_session_and_reason(temp_db, monkeypatch):
    sys.path.insert(0, str(_REPO / "scripts"))
    try:
        import backfill_sidecars as bs
        import judge_discrimination_eval as jde
    finally:
        sys.path.pop(0)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-judge")
    asyncio.run(jde._insert_decision("code", "ollama/x"))
    d = temp_db.parent / "sidecars"
    d.mkdir()
    (d / "last_route_sess-bf.json").write_text(json.dumps(
        {"task_type": "code", "complexity": "simple", "tool": "llm_code",
         "saved_at": 1_800_000_000.0}))
    asyncio.run(bs.backfill(d, dry_run=False))
    con = sqlite3.connect(temp_db)
    rows = con.execute("SELECT session_id, reason_code FROM routing_decisions ORDER BY id").fetchall()
    con.close()
    assert ("sess-judge", "judge_eval") in rows and ("sess-bf", "sidecar_backfill") in rows


def test_g3_counts_the_new_fields_on_rows_from_the_real_writers(temp_db):
    """100 usage rows from log_usage with a reason and 100 routing rows from
    log_routing_decision with session and reason: both writers complete (PASS). The
    same rows with reason / session missing: FAIL. And no traffic stays not-green."""
    from llm_router import cost
    from llm_router.commands import kpi
    from llm_router.types import RoutingProfile, TaskType

    def _rd(**kw):
        return cost.log_routing_decision(
            prompt="p", task_type="code", profile="balanced", classifier_type="heuristic",
            classifier_model=None, classifier_confidence=0.9, classifier_latency_ms=1.0,
            complexity="simple", recommended_model="m", base_model="m", was_downshifted=False,
            budget_pct_used=0.0, quality_mode="balanced", final_model="m", final_provider="openai",
            success=True, input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0, **kw)

    async def _fill(reason, sid):
        for _ in range(100):
            await cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.BALANCED,
                                 session_id=sid or "", reason=reason)
            await _rd(reason_code=reason, session_id=sid)

    def _prd():
        return kpi.compute_scorecard(days=1)["kpis"]["G3"]["prd"]["writers"]

    assert _prd()["usage"]["state"] == _prd()["routing_decisions"]["state"] == "no traffic"
    asyncio.run(_fill("router_chain", "s-org"))
    w = _prd()
    assert (w["usage"]["state"], w["usage"]["n"]) == ("pass", 100)
    assert (w["routing_decisions"]["state"], w["routing_decisions"]["n"]) == ("pass", 100)

    con = sqlite3.connect(temp_db)
    con.execute("UPDATE usage SET reason = NULL WHERE id <= 10")
    con.execute("UPDATE routing_decisions SET session_id = NULL WHERE id <= 10")
    con.commit()
    con.close()
    w = _prd()
    assert w["usage"]["state"] == "fail" and w["usage"]["fields"]["reason"]["missing"] == 10
    assert w["routing_decisions"]["state"] == "fail"
    assert w["routing_decisions"]["fields"]["session_id"]["missing"] == 10
