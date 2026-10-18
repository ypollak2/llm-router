"""F101: judge drains skip during benchmarks and share one rate limit.

Evidence: session-start spawned a drain on every start (no rate limit, no nice, no benchmark check) and the
session-end drain (#624) had no benchmark check, so a local-model benchmark was perturbed by judge grading.
Both triggers now ask ``judge_view.drain_due``; ``judge.drain_queue`` also defers while ``bench.active`` exists.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from llm_router import failopen, judge, judge_view as jv
from llm_router.commands.judge import main as judge_main

ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "src" / "llm_router" / "hooks" / "session-start.py"
END = ROOT / "src" / "llm_router" / "hooks" / "session-end.py"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_NO_DETACHED_SPAWN", raising=False)
    monkeypatch.delenv("LLM_ROUTER_JUDGE_AUTODRAIN", raising=False)
    failopen.reset_cache()
    (tmp_path / "judge_queue.jsonl").write_text('{"x": 1}\n')
    return tmp_path


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def start_hook(home):
    return _load("_f101_start", START)


@pytest.fixture
def end_hook(home):
    return _load("_f101_end", END)


@pytest.fixture
def spawns(monkeypatch, start_hook, end_hook):
    import llm_router.statusline_tick as st

    got: list[tuple[str, list[str]]] = []

    def fake_popen(argv, **kw):
        got.append(("start", list(argv)))
        return type("P", (), {"pid": 1})()

    monkeypatch.setattr(start_hook.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(st, "_spawn_detached", lambda argv: got.append(("end", list(argv))))
    return got


def _trigger_both(start_hook, end_hook):
    start_hook._drain_judge_queue_bg()
    end_hook._refresh_judge_summary()


def test_bench_marker_blocks_both_triggers(home, start_hook, end_hook, spawns):
    (home / "bench.active").write_text("")
    _trigger_both(start_hook, end_hook)
    assert spawns == []
    assert not (home / "judge_drain.claim").exists()      # a deferred drain must not burn the window


def test_without_marker_exactly_one_trigger_spawns_and_it_is_niced(home, start_hook, end_hook, spawns):
    _trigger_both(start_hook, end_hook)                    # control: the guard is not simply always-False
    assert [w for w, _ in spawns] == ["start"]             # shared claim: session-end is inside the window
    argv = spawns[0][1]
    assert argv[1] == "-c" and "os.nice(19)" in argv[2] and "drain_queue" in argv[2]
    assert spawns[0][1][2] == jv.DRAIN_SCRIPT


def test_shared_claim_in_either_direction(home, start_hook, end_hook, spawns):
    end_hook._refresh_judge_summary()
    start_hook._drain_judge_queue_bg()
    assert [w for w, _ in spawns] == ["end"]


def test_empty_queue_spawns_nothing(home, start_hook, end_hook, spawns):
    (home / "judge_queue.jsonl").write_text("")
    _trigger_both(start_hook, end_hook)
    assert spawns == []


def test_autodrain_off_spawns_nothing(home, monkeypatch, start_hook, end_hook, spawns):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_AUTODRAIN", "0")
    _trigger_both(start_hook, end_hook)
    assert spawns == []


def test_drain_queue_makes_no_model_call_during_benchmark(home, monkeypatch):
    (home / "bench.active").write_text("")

    async def boom(item):
        raise AssertionError("model call during benchmark")

    monkeypatch.setattr(judge, "_grade_one", boom)
    res = asyncio.run(judge.drain_queue())
    assert res["deferred"] == "benchmark active" and res["graded"] == 0
    assert (home / "judge_queue.jsonl").read_text() == '{"x": 1}\n'   # nothing claimed


def test_cli_says_deferred_and_force_overrides(home, monkeypatch, capsys):
    (home / "bench.active").write_text("")
    calls: list[bool] = []

    async def fake(batch_size=50, time_budget_s=20.0, *, force=False):
        calls.append(force)
        base = {"graded": 0, "ungraded": 0, "failed": 0, "requeued": 0}
        return base if force else {**base, "deferred": "benchmark active"}

    monkeypatch.setattr(judge, "drain_queue", fake)
    assert judge_main(["drain"]) == 0
    assert "deferred: benchmark active" in capsys.readouterr().out
    assert judge_main(["drain", "--force"]) == 0
    out = capsys.readouterr().out
    assert "graded=0" in out and calls == [False, True]


def test_force_reaches_the_real_drain_past_the_marker(home, monkeypatch):
    (home / "bench.active").write_text("")
    async def grade(item):
        return "graded"

    monkeypatch.setattr(judge, "_grade_one", grade)
    res = asyncio.run(judge.drain_queue(force=True))
    assert "deferred" not in res


def test_kpi_relabels_failed_as_not_graded():
    d = {"window": {"label": "14d"}, "n": 0, "ungraded": {"failed": 2, "ungraded_no_judge": 1}, "doors": {}}
    text = "\n".join(jv.render_lines(d))
    assert "not graded 2" in text and "failed" not in text
