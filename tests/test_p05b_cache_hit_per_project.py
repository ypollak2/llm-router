"""P0.5-b (AMEND-R8 A.5, R-CTX-7): the semantic cache -- which is the PRD's
result cache (D-R8-5) -- reports its hit rate PER PROJECT with the n behind it.

`test_p05_semantic_cache_key.py` covers the headline: one key, context in the
key, and a session-level hit rate with n. It cannot tell two projects apart:
``semantic_cache_lookups`` had no project column, so a per-project rate was not
derivable from the schema at all. These tests keep three things closed:

1. every lookup ``check`` writes carries its ``project_scope`` (real schema, not
   a fixture-only column);
2. ``kpi`` and ``llm_router_status(view="cache")`` print ``{project, lookups,
   hits, n}`` per project, ``n`` always shown;
3. a project with fewer than 20 lookups prints "not informative" and no
   percentage; at 20 it prints a rate. Two projects x two fixtures (19/20 and
   20/19), so the threshold is tested on each project in each position.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_router import semantic_cache as sc
from llm_router.types import LLMResponse, TaskType


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)  # exact-match pass only
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_CACHE", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "no-transcripts"))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    import llm_router.config as cfg_mod

    cfg_mod._config = None
    from llm_router.config import get_config

    cfg = get_config()
    if str(cfg.llm_router_db_path) != str(db):
        object.__setattr__(cfg, "llm_router_db_path", db)
    yield db
    cfg_mod._config = None


def _project(tmp_path: Path, name: str, monkeypatch) -> str:
    """Point the process at project *name* and return its scope key."""
    root = tmp_path / name
    (root / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(root))
    return sc._project_scope()


def _seed(db: Path, per_scope: dict[str, tuple[int, int]], ts: float | None = None) -> None:
    """Write lookups into the module's own lookups DDL: {scope: (lookups, hits)}."""
    ts = time.time() if ts is None else ts
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sc.CREATE_SEMANTIC_CACHE_LOOKUPS_TABLE)
        for scope, (n, hits) in per_scope.items():
            for i in range(n):
                conn.execute(
                    "INSERT INTO semantic_cache_lookups "
                    "(ts, task_type, hit, saved_usd, project_scope) VALUES (?, 'query', ?, 0, ?)",
                    (ts, 1 if i < hits else 0, scope),
                )
        conn.commit()
    finally:
        conn.close()


def _line(lines: list[str], scope: str) -> str:
    hits = [ln for ln in lines if f"project {scope[:12]}" in ln]
    assert len(hits) == 1, lines
    return hits[0]


# ── the threshold: 2 projects x 2 fixtures ────────────────────────────────────

FIXTURES = [
    pytest.param({"A": (19, 7), "B": (20, 5)}, id="A19-B20"),
    pytest.param({"A": (20, 5), "B": (19, 7)}, id="A20-B19"),
]


def _check_threshold(lines: list[str], scopes: dict[str, str], fixture: dict) -> None:
    for name, (n, hits) in fixture.items():
        line = _line(lines, scopes[name])
        assert f"lookups={n} hits={hits} n={n}" in line, line
        if n < 20:
            assert "not informative" in line, line
            assert "%" not in line and "hit rate" not in line, line
        else:
            assert "not informative" not in line, line
            assert f"hit rate {hits / n * 100:.1f}%" in line, line


@pytest.mark.parametrize("fixture", FIXTURES)
def test_reporter_threshold_19_vs_20(env, tmp_path, monkeypatch, fixture):
    scopes = {"A": _project(tmp_path, "proj-a", monkeypatch),
              "B": _project(tmp_path, "proj-b", monkeypatch)}
    assert scopes["A"] != scopes["B"]
    _seed(env, {scopes[k]: v for k, v in fixture.items()})

    stats = sc.per_project_hit_stats(env)
    by = {s["project_scope"]: s for s in stats}
    for name, (n, hits) in fixture.items():
        assert by[scopes[name]] == {"project_scope": scopes[name], "lookups": n, "hits": hits, "n": n}
    lines = sc.per_project_lines(stats)
    assert lines[0].endswith("n=39 lookup(s) in 2 project(s)"), lines[0]
    _check_threshold(lines, scopes, fixture)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_kpi_prints_per_project_rate_with_threshold(env, tmp_path, monkeypatch, fixture):
    from llm_router.commands import kpi

    scopes = {"A": _project(tmp_path, "proj-a", monkeypatch),
              "B": _project(tmp_path, "proj-b", monkeypatch)}
    _seed(env, {scopes[k]: v for k, v in fixture.items()})
    text = kpi.render_scorecard(kpi.compute_scorecard(days=7))
    lines = text.splitlines()
    assert any(ln.startswith("semantic cache (= result cache) hit rate per project: n=39")
               for ln in lines), text
    _check_threshold(lines, scopes, fixture)
    # the process is pointed at proj-b, so that one is marked
    assert "(this project)" in _line(lines, scopes["B"])
    assert "(this project)" not in _line(lines, scopes["A"])


@pytest.mark.parametrize("fixture", FIXTURES)
def test_status_cache_view_prints_per_project_rate_with_threshold(env, tmp_path, monkeypatch, fixture):
    from llm_router.tools.consolidated import llm_router_status

    scopes = {"A": _project(tmp_path, "proj-a", monkeypatch),
              "B": _project(tmp_path, "proj-b", monkeypatch)}
    _seed(env, {scopes[k]: v for k, v in fixture.items()})
    text = asyncio.run(llm_router_status(view="cache", period="week"))
    lines = text.splitlines()
    assert lines[0].startswith("semantic cache (= result cache) hit rate per project: n=39"), text
    assert "[last 7 day(s)]" in lines[0], text
    _check_threshold(lines, scopes, fixture)


# ── n comes from the real schema: check() writes the project ──────────────────

def test_check_records_project_scope_on_every_lookup(env, tmp_path, monkeypatch):
    """Two projects, same prompt, same context: lookups land under each project."""
    resp = LLMResponse(content="cached answer", model="openai/gpt-4o", input_tokens=1,
                       output_tokens=1, cost_usd=0.002, latency_ms=1.0, provider="openai")
    key = sc.make_key("explain the retry policy", context_text="ctx")

    async def run():
        a = _project(tmp_path, "proj-a", monkeypatch)
        assert await sc.check("", TaskType.QUERY, key=key) is None          # A miss
        await sc.store("", TaskType.QUERY, resp, key=key)
        assert await sc.check("", TaskType.QUERY, key=key) is not None      # A hit
        b = _project(tmp_path, "proj-b", monkeypatch)
        assert await sc.check("", TaskType.QUERY, key=key) is None          # B miss: no sharing
        return a, b

    a, b = asyncio.run(run())
    by = {s["project_scope"]: s for s in sc.per_project_hit_stats(env)}
    assert set(by) == {a, b}, by
    assert (by[a]["lookups"], by[a]["hits"], by[a]["n"]) == (2, 1, 2)
    assert (by[b]["lookups"], by[b]["hits"], by[b]["n"]) == (1, 0, 1)


def test_legacy_lookups_table_is_migrated_and_old_rows_stay_unscoped(env, tmp_path, monkeypatch):
    """A live usage.db has the P0.5 lookups table without project_scope."""
    conn = sqlite3.connect(str(env))
    conn.execute("""CREATE TABLE semantic_cache_lookups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, task_type TEXT NOT NULL,
        hit INTEGER NOT NULL, saved_usd REAL NOT NULL DEFAULT 0)""")
    conn.executemany("INSERT INTO semantic_cache_lookups (ts, task_type, hit) VALUES (?, 'query', ?)",
                     [(time.time(), 1), (time.time(), 0), (time.time(), 0)])
    conn.commit()
    conn.close()

    # Read-only reader on the unmigrated table: rows are counted, as unscoped.
    assert sc.per_project_hit_stats(env) == [
        {"project_scope": "", "lookups": 3, "hits": 1, "n": 3}]
    lines = sc.per_project_lines(sc.per_project_hit_stats(env))
    assert "unscoped (logged before per-project counting): lookups=3 hits=1 n=3" in lines[1]
    assert "not informative" in lines[1]

    scope = _project(tmp_path, "proj-a", monkeypatch)
    asyncio.run(sc.check("", TaskType.QUERY, key=sc.make_key("q", context_text="c")))
    by = {s["project_scope"]: s["n"] for s in sc.per_project_hit_stats(env)}
    assert by == {"": 3, scope: 1}, by


def test_reader_never_creates_a_database(tmp_path):
    missing = tmp_path / "nope" / "usage.db"
    assert sc.per_project_hit_stats(missing) == []
    assert not missing.exists()
    lines = sc.per_project_lines([])
    assert lines == ["semantic cache (= result cache) hit rate per project: "
                     "n=0 lookup(s) in 0 project(s)"]


def test_kpi_window_excludes_older_lookups(env, tmp_path, monkeypatch):
    from llm_router.commands import kpi

    scope = _project(tmp_path, "proj-a", monkeypatch)
    _seed(env, {scope: (25, 5)}, ts=time.time() - 10 * 86_400)   # outside a 7-day window
    _seed(env, {scope: (4, 1)})
    card = kpi.compute_scorecard(days=7)
    assert card["semantic_cache"]["projects"] == [
        {"project_scope": scope, "lookups": 4, "hits": 1, "n": 4}]
    line = _line(kpi.render_scorecard(card).splitlines(), scope)
    assert "not informative" in line and "%" not in line


def test_status_cache_view_is_not_the_savings_fallback(env):
    """Unknown views fall back to savings; "cache" must not."""
    from llm_router.tools.consolidated import llm_router_status

    with patch("llm_router.tools.consolidated.llm_savings") as savings:
        text = asyncio.run(llm_router_status(view="cache", period="all"))
    savings.assert_not_called()
    assert re.match(r"semantic cache \(= result cache\) hit rate per project: n=0 .*"
                    r"\[all retained lookups\]$", text), text
