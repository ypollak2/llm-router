"""P3.1-6: judge_view summary on kpi and session-end (hook v41).

Evidence: before this, judged scores existed only inside routing_decisions.judge_score and
judge_results.jsonl; no surface printed a judge figure with its n, and a model with no judged
row read as 0.0. Every oracle below is hand-written from the seeded rows.
"""
from __future__ import annotations

import ast
import builtins
import importlib.util
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import pytest

from llm_router import failopen, judge_view as jv
from llm_router.commands import kpi

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "session-end.py"
NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_NO_DETACHED_SPAWN", "")
    monkeypatch.delenv("LLM_ROUTER_JUDGE_AUTODRAIN", raising=False)
    failopen.reset_cache()
    return tmp_path


@pytest.fixture
def hook(home):
    spec = importlib.util.spec_from_file_location("se_p31f", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(door, score, *, status="graded", ago=1.0, answering="alpha:7b", judge="beta:13b", **extra):
    r = {"ts": NOW - ago * 3600, "door": door, "answering_model": answering,
         "judge_model": judge, "judge_kind": "local", "status": status, "score": score}
    r.update(extra)
    return r


def _write(home, rows):
    (home / "judge_results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def _sampler(home, data):
    (home / "judge_sampler.json").write_text(json.dumps(data))


def _summary(**kw):
    return jv.judge_summary(NOW - 14 * DAY, NOW, label="14d", **kw)


# -- empty store: n/a on all three surfaces -----------------------------------------

def test_empty_store_is_na_on_all_three_surfaces(home, hook):
    s = _summary()
    d = s.to_dict()
    assert (d["n"], d["mean"], d["self_judge"]) == (None, None, None)       # unknown, not 0
    assert all(v["n"] is None and v["mean"] is None for v in d["doors"].values())
    assert d["line"] == "judge: n/a (no judged calls, 14d)"
    # 1: kpi section
    lines = jv.render_lines(s)
    assert lines[0] == "  judge quality (14d): n/a (no judged calls; n=n/a)"
    assert "0.00" not in "\n".join(lines)
    # 2 + 3: the child writes the cache with None fields; the Stop reads the line back
    data = jv.refresh_cache(14, now=time.time())
    assert data["n"] is None and data["mean"] is None and data["self_judge"] is None
    on_disk = json.loads((home / "judge_summary.json").read_text())
    assert on_disk["n"] is None and on_disk["mean"] is None
    assert hook._cached_judge_line() == "judge: n/a (no judged calls, 14d)"


def test_results_file_with_no_graded_rows_in_window_is_n0_and_na(home):
    _write(home, [_row("mcp", 0.9, ago=30 * 24)])               # outside the 14d window
    s = _summary()
    assert s.n == 0 and s.mean is None and s.self_judge == 0
    assert s.to_dict()["line"] == "judge: n/a (no judged calls, 14d)"
    assert "n/a (no judged calls; n=0)" in jv.render_lines(s)[0]


# -- n, mean, per-door min-n ---------------------------------------------------------

def test_n_mean_and_not_informative_per_door(home):
    rows = [_row("mcp", 0.5)] * 50 + [_row("mcp", 1.0)] * 0
    rows += [_row("hook", 1.0), _row("hook", 1.0), _row("hook", 0.0)]
    rows += [_row("sdk", None, status="ungraded_no_judge"), _row("sdk", None, status="failed")]
    _write(home, rows)
    _sampler(home, {"mcp": {"seen": 500, "picked": 50, "lock_skipped": 0}, "hook": {"seen": 30, "picked": 3}})
    s = _summary()
    d = s.to_dict()
    assert d["n"] == 53 and d["doors"]["mcp"]["n"] == 50 and d["doors"]["hook"]["n"] == 3
    assert d["doors"]["mcp"]["mean"] == 0.5 and d["doors"]["hook"]["mean"] == pytest.approx(2 / 3)
    assert d["mean"] == pytest.approx((50 * 0.5 + 2.0) / 53)
    assert d["doors"]["mcp"]["not_informative"] is False                     # exactly the min n
    assert d["doors"]["hook"]["not_informative"] is True                     # 3 < 50
    assert d["doors"]["sdk"]["n"] == 0 and d["doors"]["sdk"]["mean"] is None
    assert d["doors"]["sdk"]["ungraded"] == {"ungraded_no_judge": 1, "failed": 1}
    assert d["ungraded"] == {"ungraded_no_judge": 1, "failed": 1}
    assert (d["doors"]["mcp"]["seen"], d["doors"]["mcp"]["picked"]) == (500, 50)
    assert (d["seen"], d["picked"]) == (530, 53)
    text = "\n".join(jv.render_lines(s))
    assert "(n=53)" in text and "mean 0.51" in text
    assert re.search(r"judge door hook\s+n=3\s+mean 0\.67.*not informative \(n<50\)", text)
    assert not re.search(r"judge door mcp\s+n=50.*not informative", text)
    assert "seen 500/picked 50" in text
    assert jv.format_line(s) == "judge: mean 0.51 (n=53, 14d)"


def test_overall_below_min_n_is_flagged_in_both_lines(home):
    _write(home, [_row("mcp", 0.8)] * 4)
    s = _summary()
    assert jv.format_line(s) == "judge: mean 0.80 (n=4, 14d, not informative n<50)"
    assert "mean 0.80 (n=4); not informative (n<50)" in jv.render_lines(s)[0]


def test_window_bounds_and_session_scope(home):
    _write(home, [_row("mcp", 1.0, ago=1), _row("mcp", 0.0, ago=24 * 15), _row("mcp", 0.5, ago=1, session_id="s1")])
    assert _summary().n == 2
    assert _summary(session_id="s1").n == 1
    assert _summary(session_id="other").n == 0 and _summary(session_id="other").mean is None
    assert jv.judge_summary(NOW - 3600 * 0.5, NOW, label="30m").n == 0


def test_malformed_rows_are_counted_not_dropped_silently(home):
    (home / "judge_results.jsonl").write_text(json.dumps(_row("mcp", 1.0)) + "\n{broken\n[1]\n")
    s = _summary()
    assert s.n == 1 and s.skipped_rows == 2
    assert "2 malformed judge_results rows skipped" in "\n".join(jv.render_lines(s))


# -- self-judge counter --------------------------------------------------------------

def test_self_judge_counter_is_present_and_zero_when_independent(home):
    _write(home, [_row("mcp", 0.9)])
    s = _summary()
    assert s.self_judge == 0
    assert "self-judge: 0 (must be 0)" in jv.render_lines(s)[1]
    assert s.to_dict()["doors"]["mcp"]["self_judge"] == 0


def test_self_judge_counter_catches_an_alias_and_a_missing_judge(home):
    _write(home, [_row("mcp", 0.9, answering="ollama/foo", judge="foo:latest"),
                  _row("hook", 0.9, judge=None)])
    s = _summary()
    assert s.self_judge == 2 and s.to_dict()["doors"]["mcp"]["self_judge"] == 1
    assert "self-judge: 2 (must be 0)" in jv.render_lines(s)[1]


# -- cache file ----------------------------------------------------------------------

def test_cache_is_atomic_versioned_and_carries_window(home):
    _write(home, [{**_row("mcp", 0.7), "ts": time.time() - 60}])
    data = jv.refresh_cache(14)
    on_disk = json.loads((home / "judge_summary.json").read_text())
    assert on_disk == json.loads(json.dumps(data, sort_keys=True))
    assert on_disk["v"] == 1 and on_disk["generated_at"].endswith("Z")
    assert on_disk["window"]["label"] == "14d" and on_disk["window"]["since_ts"] < on_disk["window"]["until_ts"]
    assert on_disk["n"] == 1 and on_disk["line"] == "judge: mean 0.70 (n=1, 14d, not informative n<50)"
    assert [p.name for p in home.iterdir() if p.name.endswith(".tmp")] == []
    assert jv.read_cache()["n"] == 1


def test_read_cache_rejects_another_schema_version(home):
    (home / "judge_summary.json").write_text(json.dumps({"v": 99, "line": "judge: mean 1.00"}))
    assert jv.read_cache() is None


# -- hook: Stop reads only the cache ---------------------------------------------------

def test_stop_reader_opens_only_the_cache_and_parses_no_judge_file(home, hook, monkeypatch):
    (home / "judge_results.jsonl").write_text("{garbage\n" * 1000)
    (home / "judge_sampler.json").write_text("{garbage")
    jv_data = {"v": 1, "generated_ts": time.time(), "line": "judge: mean 0.60 (n=70, 14d)"}
    (home / "judge_summary.json").write_text(json.dumps(jv_data))
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, *a, **k):
        opened.append(os.fspath(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    assert hook._cached_judge_line() == "judge: mean 0.60 (n=70, 14d)"
    assert [Path(p).name for p in opened] == ["judge_summary.json"]


def test_stop_reader_returns_none_for_absent_stale_or_foreign_cache(home, hook):
    assert hook._cached_judge_line() is None
    path = home / "judge_summary.json"
    path.write_text("{not json")
    assert hook._cached_judge_line() is None
    path.write_text(json.dumps({"v": 2, "generated_ts": time.time(), "line": "judge: x"}))
    assert hook._cached_judge_line() is None
    path.write_text(json.dumps({"v": 1, "generated_ts": time.time() - 2 * DAY, "line": "judge: stale"}))
    assert hook._cached_judge_line() is None


def test_judge_work_is_not_reachable_from_the_stop_sync_path():
    tree = ast.parse(HOOK.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    reader = ast.unparse(funcs["_cached_judge_line"])
    for forbidden in ("judge_view", "judge_results", "judge_sampler", "judge_queue", "sqlite3", "drain"):
        assert forbidden not in reader, forbidden
    main = ast.unparse(funcs["main"])
    assert "_refresh_judge_summary" not in main and "drain_queue" not in main and "judge_view" not in main
    # the only reference to the child step besides its def is the step tuple
    refs = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "_refresh_judge_summary"]
    assert len(refs) == 1
    assert "_refresh_judge_summary" in jv_steps(HOOK)


def jv_steps(path: Path) -> tuple:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_STOP_BACKGROUND_STEPS" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("_STOP_BACKGROUND_STEPS not found")


@pytest.mark.timing
def test_stop_reader_stays_inside_the_hook_budget(home, hook):
    (home / "judge_results.jsonl").write_text((json.dumps(_row("mcp", 1.0)) + "\n") * 50_000)
    (home / "judge_summary.json").write_text(
        json.dumps({"v": 1, "generated_ts": time.time(), "line": "judge: n/a (no judged calls, 14d)"}))
    t0 = time.monotonic()
    for _ in range(50):
        hook._cached_judge_line()
    per_call_ms = (time.monotonic() - t0) / 50 * 1000
    assert per_call_ms < 10, f"{per_call_ms:.2f} ms per Stop read (budget 300 ms for the whole hook)"


def test_hook_pair_is_identical_and_bumped_to_42():
    a, b = (ROOT / "hooks" / "session-end.py").read_bytes(), HOOK.read_bytes()
    assert a == b and b.splitlines()[1] == b"# llm_router-hook-version: 42"


# -- no database in hooks -------------------------------------------------------------

def test_no_database_query_in_judge_view_or_the_judge_hook_code(home, hook, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("judge_view opened a database")

    monkeypatch.setattr(sqlite3, "connect", boom)
    _write(home, [_row("mcp", 0.5)])
    jv.refresh_cache(14)
    jv.drain_due()
    hook._cached_judge_line()
    src = (ROOT / "src" / "llm_router" / "judge_view.py").read_text()
    assert not re.search(r"sqlite3|\bSELECT\b|_get_db|aiosqlite|usage\.db|routing_decisions", src)
    hook_src = HOOK.read_text(encoding="utf-8")
    start = hook_src.index("# -- P3.1-6")
    block = hook_src[start:hook_src.index("# -- D-171 S1", start)]
    assert not re.search(r"sqlite3|\bSELECT\b|_get_db|usage\.db|routing_decisions", block)


def test_judge_view_names_no_model(home):
    src = (ROOT / "src" / "llm_router" / "judge_view.py").read_text().lower()
    for lit in ("claude-", "haiku", "sonnet", "opus", "gpt-", "gemini", "llama", "qwen", "mistral"):
        assert lit not in src, lit


# -- drain trigger ----------------------------------------------------------------------

def test_drain_only_when_queue_non_empty_and_rate_limited(home):
    assert jv.drain_due(NOW) is False                          # no queue file
    (home / "judge_queue.jsonl").write_text("")
    assert jv.drain_due(NOW) is False                          # empty queue
    (home / "judge_queue.jsonl").write_text('{"x": 1}\n')
    assert jv.drain_due(NOW) is True                           # claims
    assert jv.drain_due(NOW + 60) is False                     # inside the interval
    assert jv.drain_due(NOW + jv.DRAIN_MIN_INTERVAL_S + 1) is True


def test_drain_respects_the_autodrain_opt_out(home, monkeypatch):
    (home / "judge_queue.jsonl").write_text('{"x": 1}\n')
    monkeypatch.setenv("LLM_ROUTER_JUDGE_AUTODRAIN", "0")
    assert jv.drain_due(NOW) is False


def test_child_step_writes_cache_and_spawns_one_low_priority_drain(home, hook, monkeypatch):
    import llm_router.statusline_tick as st

    spawned: list[list[str]] = []
    monkeypatch.setattr(st, "_spawn_detached", lambda argv: spawned.append(argv))
    hook._refresh_judge_summary()
    assert (home / "judge_summary.json").is_file() and spawned == []       # empty queue: no drain
    (home / "judge_queue.jsonl").write_text('{"x": 1}\n')
    hook._refresh_judge_summary()
    hook._refresh_judge_summary()                                          # rate-limited
    assert len(spawned) == 1 and spawned[0][1] == "-c"
    assert "os.nice(19)" in spawned[0][2] and "drain_queue" in spawned[0][2]


def test_child_step_does_not_spawn_when_spawning_is_disabled(home, hook, monkeypatch):
    import llm_router.statusline_tick as st

    monkeypatch.setenv("LLM_ROUTER_NO_DETACHED_SPAWN", "1")
    monkeypatch.setattr(st, "_spawn_detached", lambda argv: pytest.fail("spawned"))
    (home / "judge_queue.jsonl").write_text('{"x": 1}\n')
    hook._refresh_judge_summary()
    assert not (home / "judge_drain.claim").exists()


# -- kpi ------------------------------------------------------------------------------------

def _card(judge):
    return {"include_research": False, "generated_at": "t", "window_days": 7, "joins": {},
            "kpis": {k: {"value": "x"} for k in kpi._ORDER}, "judge": judge, "kpis_diag": {}}


def test_kpi_scorecard_prints_the_judge_section(home, monkeypatch):
    for name, ret in (("_join_line", "joins"), ("_decision_ledger_line", None),
                      ("_proxy_liveness_lines", []), ("_semantic_cache_lines", [])):
        monkeypatch.setattr(kpi, name, (lambda r: lambda *a, **k: r)(ret))
    _write(home, [{**_row("mcp", 0.5), "ts": time.time() - 60}] * 2)
    out = kpi.render_scorecard(_card(kpi._judge_quality(7, time.time(), None)))
    assert "judge quality (7d): mean 0.50 (n=2); not informative (n<50)" in out
    assert "self-judge: 0 (must be 0)" in out
    empty = kpi.render_scorecard(_card(jv.judge_summary(NOW - 7 * DAY, NOW, label="7d",
                                                       results_path=home / "absent.jsonl").to_dict()))
    assert "judge quality (7d): n/a (no judged calls; n=n/a)" in empty


def test_kpi_card_carries_the_judge_key_outside_kpis(home):
    src = (ROOT / "src" / "llm_router" / "commands" / "kpi.py").read_text()
    assert '"judge": _judge_quality(days, now_ts, win)' in src
    assert "judge" not in kpi._ORDER
