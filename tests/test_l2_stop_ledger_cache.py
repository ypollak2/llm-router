"""D-171 L2: the Stop renders ledger-wide figures from the detached child's cache,
and imports no pydantic.

Live session-end p95 was 4,237 ms. A synthetic ledger reproduces the mechanism: the
Stop re-ran full-table aggregates (five ``query_window`` windows, two ``summary``
calls, ``query_daily``, the lifetime unverified note), some of them 2 or 3 times, so
its cost grew with usage.db (500 ms p50 at 50k rows per table, 2,302 ms at 250k).

Here a usage.db with 50,000 rows in each of five tables is generated at test time.
Nothing here starts the real child or touches a keychain: every test replaces
``_spawn_background_stop_work``, ``_fetch_live_usage`` and the verify-worker spawn.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import random
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# the 50k-row ledgers are generated in module fixtures; on a loaded runner that setup alone
# can pass the suite-wide 30 s per-test timeout
pytestmark = pytest.mark.timeout(300)

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "session-end.py"
ROWS = 50_000
_CACHE = "ledger_aggregates.json"
# rolling windows the dashboard/hook query with datetime('now', '-N ...'): 24h (executed
# header), 7 days (week), 14 days. The margin outlasts the 300 s module timeout.
_ROLLING_EDGES_S = (86400, 7 * 86400, 14 * 86400)
_EDGE_MARGIN_S = 1800

# ── synthetic ledger ──────────────────────────────────────────────────────────


def _seed(state: Path, n: int) -> None:
    """usage.db with ``n`` rows in each of five money/decision tables over 60 days.
    Schema comes from ``cost._get_db`` so it is the real one."""
    from llm_router import cost

    os.environ["LLM_ROUTER_HOME"] = str(state)

    async def _create():
        db = await cost._get_db()
        await db.close()

    asyncio.run(_create())
    rng = random.Random(11)
    now = datetime.now(timezone.utc)
    models = [("ollama", "qwen3.5:latest"), ("codex", "gpt-5-codex"), ("openrouter", "deepseek-v3"),
              ("subscription", "claude-sonnet-4-6"), ("gemini", "gemini-2.5-flash")]
    tasks = ["query", "code", "analyze", "generate", "research"]
    c = sqlite3.connect(state / "usage.db")

    def ts(i: int) -> str:
        # i == 0 .. 9 land inside "today"; the rest spread over 60 days
        if i < 10:
            return (now - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        # F40: never land within _EDGE_MARGIN_S of a rolling window edge. Those edges are
        # SQL ``datetime('now', '-N')`` and move with the wall clock, so a row sitting on
        # one is counted by the cached Stop and not by the live one a second later.
        while True:
            age = rng.uniform(0, 60 * 86400)
            if all(abs(age - edge) > _EDGE_MARGIN_S for edge in _ROLLING_EDGES_S):
                return (now - timedelta(seconds=age)).strftime("%Y-%m-%d %H:%M:%S")

    def fill(table: str, gen) -> None:
        have = {r[1] for r in c.execute(f"pragma table_info({table})")}
        cols = [k for k in gen(0) if k in have]
        rows = [tuple(gen(i).get(k) for k in cols) for i in range(n)]
        c.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)

    def usage(i):
        p, m = rng.choice(models)
        return dict(timestamp=ts(i), model=m, provider=p, task_type=rng.choice(tasks), profile="balanced",
                    input_tokens=rng.randint(100, 8000), output_tokens=rng.randint(50, 2000),
                    cost_usd=rng.random() / 100, latency_ms=rng.uniform(200, 9000), success=1,
                    saved_usd=rng.random() / 50, is_simulated=0, session_id=f"s{i % 400}",
                    complexity="moderate")

    def decision(i):
        p, m = rng.choice(models)
        return dict(timestamp=ts(i), prompt_hash=f"{i:x}", task_type=rng.choice(tasks), profile="balanced",
                    classifier_type="heuristic", complexity="moderate", final_model=m, final_provider=p,
                    success=1, input_tokens=100, output_tokens=50, cost_usd=0.0, latency_ms=50.0, is_real=1,
                    session_id=f"s{i % 400}", provenance="runtime", prompt_sequence=i % 50)

    def stat(i):
        # every third row is unverified (mode NULL) so the lifetime unverified note exists
        return dict(timestamp=ts(i), session_id=f"s{i % 400}", task_type=rng.choice(tasks),
                    estimated_claude_cost_saved=rng.random() / 20, external_cost=0.0,
                    model_used="qwen3.5:latest", host="claude_code", input_tokens=500, output_tokens=200,
                    mode=None if i % 3 == 0 else "block", is_simulated=0)

    def quota(i):
        return dict(timestamp=ts(i), session_id=f"s{i % 400}", prompt_sequence=i % 50, claude_session_pct=0.3,
                    claude_weekly_pct=0.4, claude_sonnet_pct=0.1, cache_age_seconds=10.0,
                    final_model="claude-sonnet-4-6", final_provider="subscription", was_cache_fresh=1,
                    was_downgraded=0)

    def claude(i):
        return dict(timestamp=ts(i), model="claude-sonnet-4-6", tokens_used=3000, complexity="moderate",
                    cost_saved_usd=0.0, input_tokens=2000, output_tokens=1000, is_simulated=0,
                    session_id=f"s{i % 400}")

    for table, gen in (("usage", usage), ("routing_decisions", decision), ("savings_stats", stat),
                       ("quota_snapshots", quota), ("claude_usage", claude)):
        fill(table, gen)
    c.commit()
    c.close()


@pytest.fixture(scope="module")
def big_state(tmp_path_factory) -> Path:
    saved = os.environ.get("LLM_ROUTER_HOME")
    state = tmp_path_factory.mktemp("l2_big")
    try:
        _seed(state, ROWS)
    finally:
        if saved is None:
            os.environ.pop("LLM_ROUTER_HOME", None)
        else:
            os.environ["LLM_ROUTER_HOME"] = saved
    return state


@pytest.fixture(scope="module")
def small_state(tmp_path_factory) -> Path:
    saved = os.environ.get("LLM_ROUTER_HOME")
    state = tmp_path_factory.mktemp("l2_small")
    try:
        _seed(state, 200)
    finally:
        if saved is None:
            os.environ.pop("LLM_ROUTER_HOME", None)
        else:
            os.environ["LLM_ROUTER_HOME"] = saved
    return state


# ── harness ───────────────────────────────────────────────────────────────────


def _load():
    spec = importlib.util.spec_from_file_location("session_end_l2", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def _isolate(monkeypatch, state: Path, mode: str = "condensed"):
    """Point the hook at ``state`` and make it impossible to start the real child,
    the verify worker or a live-usage fetch (which may read the macOS keychain)."""
    monkeypatch.setenv("HOME", str(state))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.setenv("LLM_ROUTER_STOP_HOOK", mode)
    monkeypatch.setenv("LLM_ROUTER_HOOK_LATENCY", "off")
    monkeypatch.delenv("LLM_ROUTER_ROLLING_SUMMARY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    for stale in (state / _CACHE, state / "usage.json", state / "stop_notes.json",
                  state / "northstar_line.json", state / "session_id.txt",
                  state / "session_start_cc_pct.json", state / "quota_samples.jsonl"):
        stale.unlink(missing_ok=True)
    (state / "usage.json").write_text(json.dumps(
        {"session_pct": 16.0, "weekly_pct": 39.0, "sonnet_pct": 5.0, "session_resets_at": "",
         "updated_at": time.time()}))
    hook = _load()
    spawned: list[str] = []
    monkeypatch.setattr(hook, "_spawn_background_stop_work", lambda: spawned.append("child"))
    monkeypatch.setattr(hook, "_fetch_live_usage", lambda: spawned.append("live") or None)
    monkeypatch.setattr("llm_router.verify_queue.spawn_worker_if_needed", lambda: None)
    # a Stop appends a quota sample and rewrites the session-start snapshot; two Stops
    # compared byte for byte must not see each other's state
    monkeypatch.setattr("llm_router.quota_samples.append_session_sample", lambda *a, **k: None)
    hook._spawned = spawned
    return hook


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        return base if tz else base.replace(tzinfo=None)


def _stop(hook, monkeypatch) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"})))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    hook.main()
    return buf.getvalue()


class _Recorder:
    """Counts calls to the ledger-wide aggregates, passing them through."""

    def __init__(self, monkeypatch, hook):
        from llm_router import dashboard_data as dd

        self.calls: list[tuple] = []
        self.windows: list[str] = []
        for name in ("query_window", "query_daily", "summary", "query_primary_metric", "query_model_savings"):
            self._wrap(monkeypatch, dd, name)
        for name in ("_query_cumulative_savings", "_query_daily_14d"):
            self._wrap(monkeypatch, hook, name)

    def _wrap(self, monkeypatch, mod, name):
        real = getattr(mod, name)

        def spy(*a, **k):
            self.calls.append((name, a[0] if a else None))
            return real(*a, **k)

        monkeypatch.setattr(mod, name, spy)

    def count(self, name, window=None):
        return sum(1 for n, w in self.calls if n == name and (window is None or w == window))


def _sql_counter(monkeypatch) -> list[str]:
    stmts: list[str] = []
    real = sqlite3.connect

    def connect(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(stmts.append)
        return c

    monkeypatch.setattr(sqlite3, "connect", connect)
    return stmts


# ── S1: the child writes the cache ────────────────────────────────────────────


def test_child_writes_the_ledger_cache(big_state, monkeypatch):
    hook = _isolate(monkeypatch, big_state)
    assert "_refresh_ledger_cache" in hook._STOP_BACKGROUND_STEPS
    # the real dispatcher, with the steps that are not under test stubbed out
    for step in [n for n in hook._STOP_BACKGROUND_STEPS if n != "_refresh_ledger_cache"]:
        monkeypatch.setattr(hook, step, lambda: None)
    hook._run_background_stop_work()
    d = json.loads((big_state / _CACHE).read_text())
    assert d["v"] == 1 and isinstance(d["ts"], float) and d["day"] == hook._local_day()
    assert len(d["cumulative"]) == 5 and [r[0] for r in d["cumulative"]][0] == "today"
    assert d["cumulative"][-1][1] > 0, "lifetime calls come from the seeded ledger"
    assert d["today_compact"].startswith("~$") and d["lifetime_compact"].startswith("~$")
    assert d["lifetime_note"], "every third savings_stats row is unverified: a note exists"
    assert isinstance(d["daily_14d"], list) and d["daily_14d"]
    assert hook._spawned == [], "no live-usage fetch or child was started"


def test_child_computes_each_aggregate_once(big_state, monkeypatch):
    """S2. 'lifetime' used to run 3 times and 'today' twice in one process."""
    hook = _isolate(monkeypatch, big_state)
    rec = _Recorder(monkeypatch, hook)
    hook._refresh_ledger_cache()
    for window in ("today", "week", "14d", "month", "lifetime"):
        assert rec.count("query_window", window) == 1, (window, rec.calls)
    assert rec.count("query_primary_metric") == 2 and rec.count("query_model_savings") == 2, rec.calls
    assert rec.count("query_daily") == 1, rec.calls
    assert rec.count("summary") == 2, rec.calls


# ── S1: the Stop never runs a ledger-wide aggregate ───────────────────────────


@pytest.mark.parametrize("mode", ["condensed", "full"])
def test_stop_runs_no_ledger_wide_aggregate(big_state, monkeypatch, mode):
    hook = _isolate(monkeypatch, big_state, mode)
    hook._refresh_ledger_cache()
    rec = _Recorder(monkeypatch, hook)
    out = _stop(hook, monkeypatch)
    assert out, "the Stop printed something"
    assert rec.calls == [], f"ledger-wide aggregate on the Stop path: {rec.calls}"
    assert hook._spawned == ["child"], "only the (stubbed) child spawn; no live fetch inline"


def test_stop_sql_statement_count_does_not_depend_on_ledger_size(big_state, small_state, monkeypatch):
    counts = {}
    for name, state in (("small", small_state), ("big", big_state)):
        hook = _isolate(monkeypatch, state)
        hook._refresh_ledger_cache()
        stmts = _sql_counter(monkeypatch)
        _stop(hook, monkeypatch)
        counts[name] = len(stmts)
        monkeypatch.undo()
    assert counts["small"] == counts["big"], counts
    assert counts["big"] <= 60, f"the Stop's bounded statement budget: {counts}"


# ── S1: output is byte-identical to the live computation ──────────────────────


@pytest.mark.parametrize("mode", ["condensed", "full"])
def test_cached_render_is_byte_identical_to_the_live_computation(big_state, monkeypatch, mode):
    hook = _isolate(monkeypatch, big_state, mode)
    monkeypatch.setattr(hook, "datetime", _Frozen)
    # the session window's edge moves with the clock; two Stops seconds apart would
    # otherwise count different session rows (a 50k-row ledger has ~0.6 rows a minute)
    session_start = time.time() - 6 * 3600
    monkeypatch.setattr(hook, "_read_session_start", lambda: session_start)
    hook._refresh_ledger_cache()
    cached = _stop(hook, monkeypatch)
    (big_state / "session_start_cc_pct.json").unlink(missing_ok=True)  # written by that Stop

    # the reference: the same Stop with the cache unavailable to it, so every helper
    # runs the unchanged live query (what the hook did before the cache)
    monkeypatch.setattr(hook, "_read_ledger_cache", lambda: hook._LEDGER_LIVE)
    rec = _Recorder(monkeypatch, hook)
    live = _stop(hook, monkeypatch)
    assert rec.calls, "the reference run did query the ledger (the comparison is not empty)"

    assert cached == live
    body = json.loads(cached)["systemMessage"]
    if mode == "condensed":
        assert "today ~$" in body and "lifetime ~$" in body
    else:
        assert "lifetime" in body and "14-DAY ACTIVITY" in body


@pytest.mark.parametrize("table", ["usage", "routing_decisions", "savings_stats", "quota_snapshots",
                                   "claude_usage"])
def test_seeded_rows_keep_clear_of_every_rolling_window_edge(big_state, table):
    """F40: the byte-identity test flaked (about 1 in 200 runs) when a seeded row sat on the
    24h/7d/14d edge: the cached Stop (computed first) and the live Stop (seconds later) then
    disagreed by one row ('872 executed' vs '873', '$192.61' vs '$192.63'). Pin the
    seeding invariant, with a floor well above the runtime of one test."""
    c = sqlite3.connect(big_state / "usage.db")
    try:
        n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert n == ROWS, "the table was seeded (the check below is not over an empty set)"
        for edge in _ROLLING_EDGES_S:
            near = c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE ABS(strftime('%s','now') - strftime('%s', timestamp) - ?) < ?",
                (edge, _EDGE_MARGIN_S // 2),
            ).fetchone()[0]
            assert near == 0, f"{near} {table} rows within {_EDGE_MARGIN_S // 2}s of the {edge}s edge"
    finally:
        c.close()


def test_full_mode_carries_the_lifetime_unverified_note(big_state, monkeypatch):
    hook = _isolate(monkeypatch, big_state, "full")
    hook._refresh_ledger_cache()
    note = json.loads((big_state / _CACHE).read_text())["lifetime_note"]
    assert note
    assert f"\n  lifetime {note}\n" in json.loads(_stop(hook, monkeypatch))["systemMessage"]


# ── S1: no cache, stale cache, corrupt cache ──────────────────────────────────


def test_first_ever_run_renders_a_line_without_ledger_figures(big_state, monkeypatch):
    hook = _isolate(monkeypatch, big_state)
    rec = _Recorder(monkeypatch, hook)
    line = json.loads(_stop(hook, monkeypatch))["systemMessage"]
    assert line.startswith("⚡ llm_router")
    assert "quota used" in line, "the per-turn bits are still there"
    assert "today" not in line and "lifetime" not in line, line
    assert rec.calls == []


def _write_cache(state: Path, **over):
    base = {"v": 1, "ts": time.time(), "day": datetime.now().strftime("%Y-%m-%d"),
            "cumulative": [["today", 1, 2, 0, 0.5]], "daily_14d": [["2026-10-17", 1, 2, 0.5, 3]],
            "today_compact": "~$0.50 est", "lifetime_compact": "~$9.00 est", "lifetime_note": ""}
    base.update(over)
    (state / _CACHE).write_text(json.dumps(base))


_BAD_CACHES = {
    "empty_file": lambda s: (s / _CACHE).write_text(""),
    "garbage": lambda s: (s / _CACHE).write_text("{not json"),
    "json_list": lambda s: (s / _CACHE).write_text("[1, 2, 3]"),
    "binary": lambda s: (s / _CACHE).write_bytes(b"\xff\xfe\x00\x01"),
    "directory": lambda s: (s / _CACHE).mkdir(),
    "wrong_version": lambda s: _write_cache(s, v=99),
    "stale": lambda s: _write_cache(s, ts=time.time() - 7200),
    "from_the_future": lambda s: _write_cache(s, ts=time.time() + 7200),
    "other_day": lambda s: _write_cache(s, day="2001-01-01"),
    "ts_is_bool": lambda s: _write_cache(s, ts=True),
    "ts_missing": lambda s: _write_cache(s, ts=None),
}


@pytest.mark.parametrize("name", sorted(_BAD_CACHES))
def test_unusable_cache_never_raises_and_never_queries(big_state, monkeypatch, name):
    hook = _isolate(monkeypatch, big_state)
    _BAD_CACHES[name](big_state)
    try:
        assert hook._read_ledger_cache() == {}
        rec = _Recorder(monkeypatch, hook)
        line = json.loads(_stop(hook, monkeypatch))["systemMessage"]
        assert rec.calls == [], "an unusable cache is not a reason to scan the ledger (or wait) on the Stop"
        assert "lifetime" not in line and "today ~$" not in line
    finally:
        p = big_state / _CACHE
        if p.is_dir():
            p.rmdir()


@pytest.mark.parametrize("inner", [
    {"cumulative": "nope"}, {"cumulative": [["today", 1]]}, {"cumulative": [5]},
    {"daily_14d": {"a": 1}}, {"daily_14d": [["x"]]}, {"today_compact": 5}, {"lifetime_compact": None},
    {"lifetime_note": 7}, {"cumulative": None, "daily_14d": None},
])
def test_valid_envelope_with_garbage_inside_never_raises(big_state, monkeypatch, inner):
    for mode in ("condensed", "full"):
        hook = _isolate(monkeypatch, big_state, mode)
        _write_cache(big_state, **inner)
        assert json.loads(_stop(hook, monkeypatch))["systemMessage"]


def test_a_fresh_cache_is_shown_one_turn_old(big_state, monkeypatch):
    hook = _isolate(monkeypatch, big_state)
    _write_cache(big_state)
    line = json.loads(_stop(hook, monkeypatch))["systemMessage"]
    assert "today ~$0.50 est" in line and "lifetime ~$9.00 est" in line


def test_helpers_are_live_again_after_the_stop_returns(big_state, monkeypatch):
    hook = _isolate(monkeypatch, big_state)
    _write_cache(big_state)
    _stop(hook, monkeypatch)
    assert hook._STOP_LEDGER is hook._LEDGER_LIVE


# ── S3: no pydantic on the Stop path ──────────────────────────────────────────

_DRIVER = textwrap.dedent(
    """
    import importlib.util, io, json, os, sys
    spec = importlib.util.spec_from_file_location("se", sys.argv[1])
    se = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(se)
    se._spawn_background_stop_work = lambda: None
    se._fetch_live_usage = lambda: None
    import llm_router.verify_queue as vq
    vq.spawn_worker_if_needed = lambda: None
    if sys.argv[2] == "child":
        for step in [n for n in se._STOP_BACKGROUND_STEPS if n != "_refresh_ledger_cache"]:
            setattr(se, step, lambda: None)
        se._run_background_stop_work()
    else:
        sys.stdin = io.StringIO('{"hook_event_name": "Stop"}')
        se.main()
    bad = sorted(m for m in sys.modules
                 if m.split(".")[0] in ("pydantic", "pydantic_core", "pydantic_settings")
                 or m in ("llm_router.cost", "llm_router.config"))
    print("LOADED:" + ",".join(bad), file=sys.stderr)
    """
)


def _stop_subprocess(state: Path, mode: str, what: str = "stop") -> tuple[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(state), "LLM_ROUTER_HOME": str(state),
           "LLM_ROUTER_STOP_HOOK": mode, "LLM_ROUTER_HOOK_LATENCY": "off",
           "PYTHONPATH": str(ROOT / "src"), "LANG": "en_US.UTF-8"}
    proc = subprocess.run([sys.executable, "-c", _DRIVER, str(HOOK_PATH), what],
                          capture_output=True, text=True, env=env, cwd=str(state), timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    loaded = [ln for ln in proc.stderr.splitlines() if ln.startswith("LOADED:")]
    assert loaded, proc.stderr[-1500:]
    return loaded[-1][len("LOADED:"):], proc.stdout


@pytest.mark.parametrize("mode", ["condensed", "full", "disabled"])
def test_stop_path_imports_no_pydantic(big_state, mode):
    cache_home = big_state
    # the realistic install: a state .env that holds only API keys
    (cache_home / ".env").write_text("OPENAI_API_KEY=sk-not-a-real-key\nGEMINI_API_KEY=also-fake\n")
    try:
        _write_cache(cache_home)
        loaded, out = _stop_subprocess(cache_home, mode)
        assert loaded == "", f"Stop path imported: {loaded}"
        if mode != "disabled":
            assert out.strip()
    finally:
        (cache_home / ".env").unlink(missing_ok=True)
        (cache_home / _CACHE).unlink(missing_ok=True)


def test_child_cache_step_imports_no_pydantic_either(big_state):
    loaded, _ = _stop_subprocess(big_state, "condensed", "child")
    assert loaded == "", f"cache refresh imported: {loaded}"
    assert (big_state / _CACHE).exists()
    (big_state / _CACHE).unlink()


def test_production_only_lives_in_savings_and_cost_reexports_it():
    from llm_router import cost, savings

    assert cost.production_only is savings.production_only
    assert cost.PROVENANCE_COLUMN == savings.PROVENANCE_COLUMN == "is_simulated"
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys\nfrom llm_router import dashboard_data, savings\n"
         "dashboard_data._production_pred({'is_simulated'})\n"
         "print('llm_router.cost' in sys.modules, 'pydantic' in sys.modules)"],
        capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert out.stdout.split() == ["False", "False"], out.stderr[-800:]


# ── S3: config_lite reads one key from a state .env without the import ────────


def _config_probe(state: Path, env_text: str | None, field: str) -> list[str]:
    if env_text is not None:
        (state / ".env").write_text(env_text)
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys\nfrom llm_router.config_lite import config_value\n"
             f"print(config_value({field!r}), 'llm_router.config' in sys.modules)"],
            capture_output=True, text=True, timeout=120, cwd=str(state),
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(state), "LLM_ROUTER_HOME": str(state),
                 "PYTHONPATH": str(ROOT / "src")})
        assert proc.returncode == 0, proc.stderr[-800:]
        return proc.stdout.split()
    finally:
        (state / ".env").unlink(missing_ok=True)


def test_config_lite_ignores_a_dotenv_that_does_not_name_the_field(tmp_path):
    (tmp_path / "state").mkdir()
    out = _config_probe(tmp_path / "state", "OPENAI_API_KEY=x\n# LLM_ROUTER_CLAUDE_SUBSCRIPTION=true\n",
                        "llm_router_claude_subscription")
    assert out == ["False", "False"]


@pytest.mark.parametrize("line", ["LLM_ROUTER_CLAUDE_SUBSCRIPTION=true", "export llm_router_claude_subscription=true",
                                  "  LLM_ROUTER_CLAUDE_SUBSCRIPTION = true"])
def test_config_lite_takes_the_real_path_when_the_dotenv_names_the_field(tmp_path, line):
    (tmp_path / "state").mkdir()
    out = _config_probe(tmp_path / "state", line + "\n", "llm_router_claude_subscription")
    assert out == ["True", "True"]


def test_successful_cache_refresh_records_no_fail_open(big_state, monkeypatch):
    """F51: the success path (os.replace moved the tmp file) used to hit
    os.remove -> FileNotFoundError -> CHZ-FO-LEDGER-CACHE-TMP-REMOVE on every
    refresh. A fail-open ledger must stay empty when nothing failed."""
    hook = _isolate(monkeypatch, big_state)
    from llm_router import failopen

    fo_path = failopen.store_path()
    fo_path.unlink(missing_ok=True)
    hook._refresh_ledger_cache()
    assert (big_state / _CACHE).exists(), "the refresh wrote its cache (the check had something to check)"
    assert not fo_path.exists() or fo_path.read_text() == "", fo_path.read_text()
