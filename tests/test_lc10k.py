"""LC-10k: a probe (--no-judge) never touches the eligibility store; recommendation, estimate and rate fixes. Fakes only."""
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from llm_router.commands import local_check as C
from llm_router.local_check import attempts as A, runner, score as S, session as SE
from tests.test_lc5_scoring import BANK, Clock, FakeAdapter, check, fails
from tests.test_lc6_cli import MODEL, deps, run


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))


def elig(tmp_path: Path) -> dict:
    p = tmp_path / "elig.json"
    return json.loads(p.read_text()).get("fake-model:1b", {}).get("text_job_local", {}) if p.exists() else {}


def judged_pass(tmp_path: Path, clock: Clock) -> None:
    out = SE.run_check(check(tmp_path, FakeAdapter(fails(10)), clock=clock))
    assert out.result.verdict == S.PASS and elig(tmp_path)["commit_message"]["grade"] == "passed"


def test_judged_pass_then_probe_fail_keeps_pass_and_logs_probe(tmp_path):
    clock = Clock()
    judged_pass(tmp_path, clock)
    before = elig(tmp_path)
    clock.t += 3600
    out = SE.run_check(check(tmp_path, FakeAdapter({**fails(30, "progfail"), **fails(30, "progfail", draw=2)}), clock=clock, no_judge=True))
    assert out.result.verdict in (S.FAIL, S.NOT_PASSED) and out.record_report == {}
    assert elig(tmp_path) == before and all(v["grade"] == "passed" for v in elig(tmp_path).values())
    log = A.AttemptLog(tmp_path / "runs").entries()
    probe = log[-1]
    assert probe["mode"] == "none" and probe["consumed"] is False and probe["recorded"] is False
    assert probe["verdict"] in (S.FAIL, S.NOT_PASSED) and log[0]["verdict"] == S.PASS


def test_no_judged_record_then_probe_fail_records_no_judged_fail(tmp_path):
    out = SE.run_check(check(tmp_path, FakeAdapter({**fails(30, "progfail"), **fails(30, "progfail", draw=2)}), no_judge=True))
    assert out.result.verdict in (S.FAIL, S.NOT_PASSED)
    assert not (tmp_path / "elig.json").exists() and elig(tmp_path) == {}
    e = A.AttemptLog(tmp_path / "runs").entries()[0]
    assert e["verdict"] == out.result.verdict and e["consumed"] is False and e["recorded"] is False


def test_judged_pass_then_judged_fail_still_replaces(tmp_path):
    clock = Clock()
    judged_pass(tmp_path, clock)
    clock.t += 2 * 86400 * 30
    out = SE.run_check(check(tmp_path, FakeAdapter(fails(30, "judgefail", draw=2)), clock=clock, force=True))
    assert out.result.verdict == S.FAIL
    assert all(v["grade"] == "failed" for v in elig(tmp_path).values())


def test_record_plan_is_empty_for_probe_mode():
    from tests.test_lc5_scoring import good_gates, mkrow, verdict
    rows = [mkrow(i, "commit_message" if i < 35 else "pr_description", i < 40, judge=None, prog=i < 40) for i in range(70)]
    r = verdict(rows, mode=S.NO_JUDGE, gates=good_gates(g3b=None, judge_served=[]))
    assert r.verdict == S.FAIL and S.record_plan(r) == {} and S.record_plan(r, cumulative=True) == {}


def test_status_shows_probe_as_probe(tmp_path):
    d, sink, _ = deps(tmp_path)
    assert run(["--yes", "--no-judge", "--model", MODEL], d) in (C.EXIT_OK, C.EXIT_NOT_PASSED)
    sink.out.clear()
    run(["status"], d)
    text = sink.text
    assert "programmatic-only run(s) (--no-judge) not counted" in text and "probe, not a judged verdict" in text


def test_recommend_line_without_bank_number(tmp_path):
    from llm_router.local_check import recommend as R
    from tests.test_lc10j import data
    rows = R.recommend({}, "ollama", data=data("v1"))
    assert rows and all(r.untested_on == "" for r in rows if r.status == "untested" and r.untested_on is not None)
    d, _, _ = deps(tmp_path)
    def fail():
        raise OSError("fake")
    d.load_bank = fail
    d.recommend = lambda hw, rt, model, res, **k: R.recommend(hw, rt, model, res, data=data("v1"), **k)
    res = SimpleNamespace(verdict=S.FAIL, n_assigned=70)
    text = "\n".join(C._recommend_lines(d, res, "fake-a:7b", "ollama"))
    assert "vNone" not in text and "untested (no number shown)" in text


@pytest.mark.parametrize("model,billions_class", [("fake-moe:8x7b", ">40B"), ("fake-a:7b", "4-9B"), ("fake-a-7B", "4-9B"), ("fake-q:70b-q4bpw", ">40B"),
                                                  ("fake-a:1.5b", "<=4B")])
def test_size_parse(model, billions_class):
    sec, label = C.estimate_run_seconds(model, 1, {"gpu": "apple_unified"})
    assert sec is not None and label == billions_class


@pytest.mark.parametrize("model", ["fake-a:4bit", "fake-a:q4bpw", "fake-a:latest", "fake-a:q4_0"])
def test_size_fragments_do_not_match(model):
    assert C.estimate_run_seconds(model, 1, {"gpu": "x"}) == (None, "")


def test_unknown_gpu_is_not_cpu_only():
    sec, label = C.estimate_run_seconds("fake-a:7b", 2, {})
    assert sec == 2 * 20 and label == "4-9B (GPU unknown)"
    assert C.estimate_run_seconds("fake-a:7b", 2, {"gpu": "NONE"})[0] == 2 * 20 * 5
    assert C.estimate_run_seconds("fake-a:7b", 2, {"gpu": None})[1].endswith("(GPU unknown)")


OK = {"status": "ok", "elapsed_s": 4, "first_token_s": 2, "prompt_chars": 100, "completion_tokens": 20}


def test_measure_rate_cold_flag_not_position():
    cold = {**OK, "cold": True, "elapsed_s": 1000, "first_token_s": 900}
    assert runner.measure_rate([OK, OK]).n == 2                                  # resumed run: nothing is dropped positionally
    assert runner.measure_rate([cold, {"status": "error"}]).mean_item_s == 1000  # only a cold record is usable: use it
    assert runner.measure_rate([cold, {"status": "error"}, OK]).n == 1
    assert runner.measure_rate([OK, cold, OK]).n == 2


def test_runner_marks_only_first_record_of_a_process(tmp_path):
    items = [runner.SimpleItem(i.id, i.task) for i in BANK.draw(1)[:4]]
    ad = FakeAdapter()
    r = runner.Runner(ad, "fake-model:1b", items[:3], tmp_path / "run", probe_n=1)
    r.run()
    flags = [x.get("cold") for x in r.store.load()]
    assert flags == [True, None, None]
    r2 = runner.Runner(ad, "fake-model:1b", items[:2], tmp_path / "run2", probe_n=1)
    r2.run()
    assert [x.get("cold") for x in r2.store.load()] == [True, None]
