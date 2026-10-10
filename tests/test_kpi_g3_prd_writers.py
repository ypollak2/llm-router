"""G3 per writer over the PRD field list (PLAN v16 R8 A.6, MUST P0.8-c; R-EVL-1).

``kpi`` G3 audited the proxy's tier fields only; on 2026-10-08 it read 93.0% while
usage / routing_decisions / DIRECT rows were never checked. The verdict now scores each
writer on its own over ids, model, tier, reason, tokens, cost, latency, outcome
(NULL = missing), with n. Each test here fails on origin/main 1b364e85 (no ``prd`` block).

R8's fixtures: one complete writer and one 80%-complete writer -> FAIL; a writer with 0
rows prints "no traffic" and cannot turn G3 green on its own; harness and headless rows
are excluded from the organic population and shown apart.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from llm_router import cost, session_kind
from llm_router.commands import kpi
from llm_router.proxy import ledger as pl

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    session_kind._FOUND.clear()
    yield
    session_kind._FOUND.clear()


# ── builders ────────────────────────────────────────────────────────────────

def _proxy(i: int, *, kind: str | None = "organic", sid: str | None = "s-org", **kw) -> dict:
    row = {"ts": NOW - 3600 + i, "session_id": sid, "session_kind": kind,
           "task_id": f"task{i:04d}", "trace_id": f"trace{i:04d}",
           "decision": "forwarded", "tier_mode": "conversation",
           "requested_model": "claude-sonnet-4-6", "served_model": "claude-sonnet-4-6",
           "tier": "sonnet", "tier_reason": "policy", "tier_policy_version": "abc123def456",
           "tier_proposed": "sonnet", "tier_retry": None,
           "anthropic_usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
           "anthropic_cost_usd": 0.0001, "upstream_latency_s": 1.2, "upstream_status": 200}
    row.update(kw)
    return row


def _write_proxy(rows: list[dict]) -> None:
    path = pl.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _db(*, usage_reason: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(kpi._prd_db_path()))
    conn.execute(cost.CREATE_TABLE)
    conn.execute(cost.CREATE_ROUTING_DECISIONS_TABLE)
    for sql in ("ALTER TABLE usage ADD COLUMN session_id TEXT",
                "ALTER TABLE usage ADD COLUMN complexity TEXT",
                "ALTER TABLE routing_decisions ADD COLUMN session_id TEXT",
                "ALTER TABLE routing_decisions ADD COLUMN reason_code TEXT"):
        conn.execute(sql)
    for sql in cost.MIGRATE_ADD_TASK_IDENTITY:  # P1.10: task_id / trace_id on both tables
        conn.execute(sql)
    if usage_reason:  # P0.8-d: an old-schema database is _db(usage_reason=False)
        conn.execute(cost.MIGRATE_USAGE_ADD_REASON[0])
    return conn


def _stamp(i: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 3600 + i))


def _decisions(conn, n: int, *, reason_code: str | None = "policy", sid: str | None = "s-org",
               null_model_every: int = 0) -> None:
    for i in range(n):
        model = None if null_model_every and i % null_model_every == 0 else "gpt-4o-mini"
        conn.execute(
            "INSERT INTO routing_decisions (timestamp, session_id, final_model, complexity, "
            "reason_code, input_tokens, output_tokens, cost_usd, latency_ms, success, task_id) "
            "VALUES (?, ?, ?, 'simple', ?, 10, 5, 0.0001, 300.0, 1, ?)",
            (_stamp(i), sid, model, reason_code, f"task{i:04d}"))
    conn.commit()


def _usage(conn, n: int, *, sid: str | None = "s-org", reason: str | None = None,
           with_reason: bool = False) -> None:
    for i in range(n):
        if with_reason:
            conn.execute(
                "INSERT INTO usage (timestamp, session_id, model, provider, task_type, profile, "
                "complexity, input_tokens, output_tokens, cost_usd, latency_ms, success, reason, "
                "task_id) VALUES (?, ?, 'gpt-4o-mini', 'openai', 'code', 'balanced', 'simple', "
                "10, 5, 0.0001, 300.0, 1, ?, ?)", (_stamp(i), sid, reason, f"task{i:04d}"))
        else:
            conn.execute(
                "INSERT INTO usage (timestamp, session_id, model, provider, task_type, profile, "
                "complexity, input_tokens, output_tokens, cost_usd, latency_ms, success, task_id) "
                "VALUES (?, ?, 'gpt-4o-mini', 'openai', 'code', 'balanced', 'simple', 10, 5, "
                "0.0001, 300.0, 1, ?)", (_stamp(i), sid, f"task{i:04d}"))
    conn.commit()


def _card(**kw) -> dict:
    return kpi.compute_scorecard(days=7, now=NOW, **kw)


def _prd(**kw) -> dict:
    return _card(**kw)["kpis"]["G3"]["prd"]


def _g3_text(card: dict) -> str:
    text = kpi.render_scorecard(card)
    return text[text.index("G3  ledger completeness"):text.index("G4  wrongly")]


# ── R8 fixture 1: one complete writer + one 80%-complete writer -> FAIL ─────

def test_one_complete_writer_and_one_80pct_writer_is_fail():
    _write_proxy([_proxy(i) for i in range(120)])
    conn = _db()
    _decisions(conn, 120, null_model_every=5)        # 24 of 120 rows have no model: 80%
    conn.close()

    card = _card()
    prd = card["kpis"]["G3"]["prd"]
    assert prd["verdict"] == "FAIL" and prd["pass"] is False
    assert prd["why"] == "below 99%: routing_decisions"
    rd, px = prd["writers"]["routing_decisions"], prd["writers"]["proxy"]
    assert (rd["state"], rd["n"], rd["complete"], rd["complete_pct"]) == ("fail", 120, 96, 0.8)
    assert rd["fields"]["model"]["missing_pct"] == 0.2
    assert (px["state"], px["n"], px["complete_pct"]) == ("pass", 120, 1.0)
    text = _g3_text(card)
    assert "verdict FAIL (below 99%: routing_decisions)" in text
    assert "routing_decisions: fail 80.0% complete (n=120); missing: model 20.0%" in text
    assert "proxy: pass 100.0% complete (n=120); missing: none" in text


def test_control_both_writers_complete_is_pass_and_empty_writers_print_no_traffic():
    """The same fixture with the 80% writer made complete: PASS. The FAIL above is the 80%."""
    _write_proxy([_proxy(i) for i in range(120)])
    conn = _db()
    _decisions(conn, 120)
    conn.close()

    card = _card()
    prd = card["kpis"]["G3"]["prd"]
    assert prd["verdict"] == "PASS" and prd["pass"] is True
    assert prd["writers"]["usage"]["state"] == prd["writers"]["direct"]["state"] == "no traffic"
    text = _g3_text(card)
    assert "  usage: no traffic (0 organic rows)" in text
    assert "  direct: no traffic (0 organic rows)" in text


# ── R8 fixture 2: a writer with 0 rows cannot turn G3 green on its own ──────

def test_no_writer_with_traffic_is_not_informative_never_pass():
    card = _card()
    prd = card["kpis"]["G3"]["prd"]
    assert prd["verdict"] == "NOT INFORMATIVE" and prd["pass"] is False
    assert prd["why"] == "no writer has traffic in the window"
    assert {w: r["state"] for w, r in prd["writers"].items()} == {
        "usage": "no traffic", "routing_decisions": "no traffic",
        "direct": "no traffic", "proxy": "no traffic"}
    assert "verdict NOT INFORMATIVE (no writer has traffic in the window)" in _g3_text(card)


@pytest.mark.parametrize("n,verdict", [(99, "NOT INFORMATIVE"), (100, "PASS")])
def test_min_n_per_writer_with_traffic(n, verdict):
    _write_proxy([_proxy(i) for i in range(n)])
    prd = _prd()
    assert prd["verdict"] == verdict
    assert prd["writers"]["proxy"]["informative"] is (n >= 100)


@pytest.mark.parametrize("missing,verdict", [(1, "PASS"), (2, "FAIL")])
def test_bar_is_99pct_of_rows_complete_and_null_is_missing(missing, verdict):
    rows = [_proxy(i) for i in range(100)]
    for r in rows[:missing]:
        r["anthropic_usage"] = None                    # usage unknown: NULL, never 0
    _write_proxy(rows)
    prd = _prd()
    assert prd["verdict"] == verdict
    assert prd["writers"]["proxy"]["fields"]["tokens"]["missing"] == missing


# ── R8 fixture 3: harness and headless rows excluded, shown apart ───────────

def test_harness_and_headless_rows_are_excluded_and_counted_apart():
    incomplete = {"anthropic_cost_usd": None, "tier": None}
    rows = ([_proxy(i) for i in range(100)]
            + [_proxy(200 + i, kind="harness", sid=f"h-{i}", **incomplete) for i in range(30)]
            + [_proxy(300 + i, kind="headless", sid=f"x-{i}", **incomplete) for i in range(20)])
    _write_proxy(rows)
    card = _card()
    px = card["kpis"]["G3"]["prd"]["writers"]["proxy"]
    assert (px["state"], px["n"], px["excluded"]) == ("pass", 100, {"harness": 30, "headless": 20})
    assert card["kpis"]["G3"]["prd"]["verdict"] == "PASS"
    assert "excluded apart: harness 30, headless 20" in _g3_text(card)


def test_a_writer_with_only_harness_rows_is_no_traffic():
    _write_proxy([_proxy(i, kind="harness", sid="h") for i in range(150)])
    card = _card()
    prd = card["kpis"]["G3"]["prd"]
    assert prd["writers"]["proxy"]["state"] == "no traffic"
    assert prd["verdict"] == "NOT INFORMATIVE"
    assert "  proxy: no traffic (0 organic rows; excluded apart: harness 150)" in _g3_text(card)


def test_sql_writer_rows_are_excluded_by_the_sessions_tag():
    """usage.db rows carry no kind: the session's tag decides, as for NS/D3."""
    session_kind.tag_session("h-sess", "/tmp/x", env={"LLM_ROUTER_SESSION_KIND": "harness"})
    conn = _db()
    _decisions(conn, 100)
    _decisions(conn, 40, sid="h-sess", null_model_every=1)
    conn.close()
    rd = _prd()["writers"]["routing_decisions"]
    assert (rd["state"], rd["n"], rd["excluded"]) == ("pass", 100, {"harness": 40})


def test_untagged_rows_stay_in_so_a_missing_session_id_is_counted():
    """Dropping untagged rows would drop exactly the rows with no session id."""
    _write_proxy([_proxy(i) for i in range(100)]
                 + [_proxy(500 + i, kind=None, sid=None) for i in range(5)])
    px = _prd()["writers"]["proxy"]
    assert (px["state"], px["n"], px["complete"]) == ("fail", 105, 100)
    assert px["fields"]["session_id"]["missing"] == 5


def test_research_rows_excluded_unless_included():
    _write_proxy([_proxy(i) for i in range(100)]
                 + [_proxy(500 + i, kind="research", sid="r", tier=None) for i in range(10)])
    assert _prd()["writers"]["proxy"]["excluded"] == {"research": 10}
    px = _prd(include_research=True)["writers"]["proxy"]
    assert (px["n"], px["state"], px["excluded"]) == (110, "fail", {})


# ── the field list against the real schemas ─────────────────────────────────

def test_usage_on_an_old_schema_has_no_reason_column_so_it_cannot_pass():
    """A database from before P0.8-d has no usage.reason. That is scored as missing
    (named "[no column]"), never left out of the list."""
    conn = _db(usage_reason=False)
    _usage(conn, 120)
    conn.close()
    card = _card()
    u = card["kpis"]["G3"]["prd"]["writers"]["usage"]
    assert (u["state"], u["n"], u["complete"]) == ("fail", 120, 0)
    assert u["fields"]["reason"] == {"recorded": 0, "missing": 120, "missing_pct": 1.0,
                                     "scored": True, "no_column": True}
    assert "usage: fail 0.0% complete (n=120); missing: reason 100.0% [no column]" in _g3_text(card)


def test_usage_reason_is_counted_and_null_reason_rows_are_missing():
    """P0.8-d: the migrated column is scored. 120 rows with a reason pass; 120 rows of the
    same shape without one (rows from before the column, or a writer that skips it) fail,
    and nothing says "[no column]" because the column exists."""
    _write_proxy([_proxy(i) for i in range(120)])
    conn = _db()
    _usage(conn, 120, reason="router_chain", with_reason=True)
    conn.close()
    u = _prd()["writers"]["usage"]
    assert (u["state"], u["n"], u["complete"]) == ("pass", 120, 120)
    assert u["fields"]["reason"]["no_column"] is False

    conn = sqlite3.connect(str(kpi._prd_db_path()))
    conn.execute("DELETE FROM usage")
    conn.commit()
    _usage(conn, 120)  # reason NULL
    conn.close()
    card = _card()
    u = card["kpis"]["G3"]["prd"]["writers"]["usage"]
    assert (u["state"], u["complete"]) == ("fail", 0)
    assert u["fields"]["reason"] == {"recorded": 0, "missing": 120, "missing_pct": 1.0,
                                     "scored": True, "no_column": False}
    assert card["kpis"]["G3"]["prd"]["verdict"] == "FAIL"
    assert "usage: fail 0.0% complete (n=120); missing: reason 100.0%" in _g3_text(card)
    assert "reason 100.0% [no column]" not in _g3_text(card)


def test_no_traffic_on_every_sql_writer_is_never_green():
    """An empty usage.db (schema present, zero rows) and no proxy rows: every writer says
    no traffic and the verdict is NOT INFORMATIVE, not PASS."""
    conn = _db()
    conn.close()
    prd = _prd()
    assert {w: r["state"] for w, r in prd["writers"].items()} == {
        "usage": "no traffic", "routing_decisions": "no traffic", "direct": "no traffic",
        "proxy": "no traffic"}
    assert prd["verdict"] == "NOT INFORMATIVE" and prd["pass"] is False


def test_a_complete_writer_beside_a_no_traffic_writer_is_pass_only_for_the_writer_with_traffic():
    conn = _db()
    _usage(conn, 120, reason="router_chain", with_reason=True)
    conn.close()
    prd = _prd()
    assert prd["writers"]["routing_decisions"]["state"] == "no traffic"
    assert prd["verdict"] == "PASS" and prd["why"] == "every writer with traffic: usage"
    conn = sqlite3.connect(str(kpi._prd_db_path()))
    conn.execute("DELETE FROM usage")
    conn.commit()
    _usage(conn, 99, reason="router_chain", with_reason=True)  # below n >= 100
    conn.close()
    assert _prd()["verdict"] == "NOT INFORMATIVE"


def test_task_id_is_reported_not_scored_until_the_merge_date_is_set():
    _write_proxy([_proxy(i) for i in range(100)])
    card = _card()
    px = card["kpis"]["G3"]["prd"]["writers"]["proxy"]
    assert px["state"] == "pass"
    assert px["fields"]["task_id"] == {"recorded": 100, "missing": 0, "missing_pct": 0.0,
                                       "scored": False, "no_column": False}
    assert card["kpis"]["G3"]["prd"]["unscored"] == {"task_id": "not scored until P1.10 merges"}


def test_direct_and_mcp_rows_are_separate_writers():
    conn = _db()
    _decisions(conn, 120, reason_code="direct")
    _decisions(conn, 30)
    conn.close()
    prd = _prd()
    assert (prd["writers"]["direct"]["n"], prd["writers"]["routing_decisions"]["n"]) == (120, 30)
    assert prd["writers"]["routing_decisions"]["state"] == "not informative"
    assert prd["verdict"] == "NOT INFORMATIVE"


def test_real_direct_writer_row_carries_every_scored_field(temp_db):
    """One row through the real DIRECT writer (hooks/savings_logger): the field map
    reads the real schema."""
    from llm_router.hooks.direct_executor import DirectResult, ModelSpec
    from llm_router.hooks.savings_logger import log_direct_to_db

    log_direct_to_db(DirectResult(text="a", model=ModelSpec(provider="ollama", model="qwen3.5:latest"),
                                  latency_ms=6500, input_tokens=120, output_tokens=60),
                     prompt="x", task_type="code", complexity="simple",
                     session_id="3f2a9c1e-0000-4000-8000-000000000001")
    prd = kpi.compute_scorecard(days=1)["kpis"]["G3"]["prd"]
    d, u = prd["writers"]["direct"], prd["writers"]["usage"]
    assert (d["state"], d["n"], d["complete"]) == ("not informative", 1, 1)
    # P0.8-d: the DIRECT writer's usage twin now records reason too.
    assert (u["n"], u["complete"]) == (1, 1)


def test_an_unreadable_database_is_not_informative_never_pass():
    _write_proxy([_proxy(i) for i in range(120)])
    kpi._prd_db_path().write_bytes(b"not a sqlite database" * 100)
    prd = _prd()
    assert prd["writers"]["usage"]["state"] == "unreadable"
    assert prd["verdict"] == "NOT INFORMATIVE"
