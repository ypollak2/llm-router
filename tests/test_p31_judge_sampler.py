"""P3.1-1: block-stratified per-door judge sampler, door tag, text-free results."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from llm_router import failopen, judge_sampler
from llm_router.file_lock import exclusive_lock
from llm_router.judge import enqueue_for_grading

DOORS = ("proxy", "sdk", "gateway", "mcp", "hook")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "0.1")
    judge_sampler._pending_skips.clear()
    failopen.reset_cache()
    return tmp_path


def _enqueue(door: str, i: int) -> bool:
    return enqueue_for_grading(
        routing_decision_id=i + 1, prompt="p", response="r",
        task_type="query", answering_model="m", door=door,
    )


def test_every_window_of_ten_has_exactly_one_pick_per_door(home):
    for door in DOORS:
        picks = [_enqueue(door, i) for i in range(1000)]
        assert 95 <= sum(picks) <= 105, door
        for start in range(len(picks) - 9):
            assert sum(picks[start:start + 10]) == 1, (door, start)
    entries = [json.loads(x) for x in (home / "judge_queue.jsonl").read_text().splitlines()]
    assert {e["door"] for e in entries} == set(DOORS)
    state = judge_sampler.counters()
    assert state is not None
    assert all(state[d]["seen"] == 1000 for d in DOORS)


def test_unknown_door_is_reported_separately(home):
    for i in range(20):
        judge_sampler.pick(None)
        judge_sampler.pick("not-a-door")
    state = judge_sampler.counters()
    assert state["unknown"]["seen"] == 40 and state["unknown"]["picked"] == 4
    assert "not-a-door" not in state


def test_counters_survive_a_new_process(home):
    code = (
        "from llm_router import judge_sampler as j\n"
        "[j.pick('sdk') for _ in range(35)]\n"
    )
    env = {**__import__("os").environ}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
    c = judge_sampler.counters()["sdk"]
    assert c["seen"] == 35 and c["picked"] in (3, 4) and c["lock_skipped"] == 0
    judge_sampler.pick("sdk")
    assert judge_sampler.counters()["sdk"]["seen"] == 36


def test_two_processes_lose_no_counts(home):
    code = (
        "from llm_router import judge_sampler as j\n"
        "[j.pick('gateway') for _ in range(200)]\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(2)]
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    c = judge_sampler.counters()["gateway"]
    assert c["seen"] == 400 and c["picked"] == 40 and c["lock_skipped"] == 0


def test_hook_lock_failure_fails_open_and_is_counted(home):
    lock = home / "judge_sampler.json.lock"
    with exclusive_lock(lock, timeout=0):
        assert judge_sampler.pick("hook") is False
        assert judge_sampler.pick("hook") is False
    assert judge_sampler.pick("hook") in (True, False)  # lock free again
    c = judge_sampler.counters()["hook"]
    assert c["lock_skipped"] == 2 and c["seen"] == 1
    assert "CHZ-FO-JUDGE-SAMPLER-LOCK" in (home / "fail_open.jsonl").read_text()


@pytest.mark.parametrize("raw,expected", [
    ("0.25", 0.25), ("1", 1.0), ("0", 0.0),
    ("-0.1", 0.1), ("1.5", 0.1), ("nan", 0.1), ("abc", 0.1), ("", 0.1),
])
def test_rate_validation(home, monkeypatch, raw, expected):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", raw)
    assert judge_sampler.sample_rate() == expected


def test_rate_one_picks_every_call_and_zero_none(home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1")
    assert all(judge_sampler.pick("mcp") for _ in range(5))
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "0")
    assert not judge_sampler.pick("mcp")
    assert judge_sampler.counters()["mcp"]["seen"] == 5


def test_judge_results_has_no_text_fields(home):
    judge_sampler.record_result(
        door="sdk", answering_model="a", judge_model="b", status="graded", score=0.8,
    )
    judge_sampler.record_result(
        door=None, answering_model="a", judge_model=None, status="ungraded", score=None,
    )
    rows = [json.loads(x) for x in (home / "judge_results.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    for row in rows:
        assert set(row) == {"ts", "door", "answering_model", "judge_model", "judge_kind", "status", "score"}
    assert rows[1]["door"] == "unknown" and rows[1]["score"] is None


@pytest.mark.asyncio
async def test_drain_writes_result_row_without_text(home, monkeypatch):
    from llm_router import judge

    monkeypatch.setattr(judge, "_select_judge_model", lambda m: None)
    outcome = await judge._grade_one(
        {"prompt": "SECRET-P", "response": "SECRET-R", "answering_model": "a", "door": "agent"}
    )
    assert outcome == "ungraded"
    raw = (home / "judge_results.jsonl").read_text()
    assert "SECRET" not in raw and json.loads(raw)["door"] == "agent"


@pytest.mark.parametrize("content", ["{garbage", '{"sdk": 5}', '["not", "a", "dict"]', '{"sdk": {"seen": "x"}}'])
def test_corrupt_state_is_repaired_and_judging_continues(tmp_path, monkeypatch, content):
    # rv-598: a corrupt or wrong-shaped counters file used to make every pick()
    # fail open forever (judging silently at 0%).
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "0.1")
    path = judge_sampler._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    picks = sum(judge_sampler.pick("sdk") for _ in range(20))
    assert picks == 2
    assert judge_sampler.counters()["sdk"]["seen"] == 20
