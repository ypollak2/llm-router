"""Fitness tasks, frozen pass marks, the eligibility adapter, run_calibration wiring."""

from __future__ import annotations

import json

import pytest

from llm_router.resolver import calibrate as cal
from llm_router.resolver import fitness as fit
from llm_router.resolver import inventory as inv_mod
from llm_router.resolver.calibrate import Reply
from llm_router.resolver.eligibility_store import record_fitness

from .fakes import make_probes
from .test_calibrate import FakeModel


class Fit:
    supports_tools = supports_images = True

    def __init__(self, good=True, error=None):
        self.good, self.error = good, error

    def chat(self, model, messages, *, tools=None, num_ctx=None, timeout=90):
        if self.error:
            return Reply(error=self.error)
        p = messages[-1]["content"]
        if not self.good:
            return Reply(text="no idea")
        if "git command" in p:
            return Reply(text="git log origin/main..HEAD")
        if "Summarise" in p:
            return Reply(text="The Haifa ferry leaves at 08:15 and tickets cost 32 shekels.")
        if "is_even" in p:
            return Reply(text='{"old_string": "== 1", "new_string": "== 0"}')
        if '"city"' in p:
            return Reply(text='{"city": "Haifa", "count": 3}')
        if "בירת" in p:
            return Reply(text="ירושלים")
        return Reply(text="It removes duplicate items and keeps the original order using a set.")


def test_six_tasks_and_perfect_scores():
    res = fit.run_fitness(Fit(), "m")
    assert set(res) == {"tech_step", "summary", "edit_with_test", "json", "hebrew_qa",
                        "code_explanation"}
    assert all(r.score == 1.0 for r in res.values())


def test_no_pass_mark_is_unknown_never_pass():
    assert fit.PASS_MARKS == {}
    assert {r.result for r in fit.run_fitness(Fit(), "m").values()} == {"unknown"}
    with pytest.raises(TypeError):
        fit.PASS_MARKS["code"] = 0.5  # type: ignore[index]


def test_marks_pass_and_fail():
    assert fit.run_fitness(Fit(), "m", pass_marks={"json": 0.5})["json"].result == "pass"
    bad = fit.run_fitness(Fit(good=False), "m", pass_marks={"json": 0.5})
    assert bad["json"].result == "fail" and bad["json"].score == 0.0


def test_transport_error_is_unknown_even_with_mark():
    r = fit.run_fitness(Fit(error=cal.ERR_PROVIDER), "m", pass_marks={"json": 0.5})["json"]
    assert (r.result, r.score, r.n) == ("unknown", None, 0)


def test_record_fitness_merges_and_survives_corrupt(tmp_path):
    p = tmp_path / "local_eligibility.json"
    p.write_text(json.dumps({"other": {"x": 1}, "m": {"keep": True}}))
    record_fitness("m", fit.run_fitness(Fit(), "m"), path=p)
    d = json.loads(p.read_text())
    assert d["other"] == {"x": 1} and d["m"]["keep"] is True
    assert d["m"]["fitness"]["json"] == {"score": 1.0, "n": 1, "result": "unknown",
                                         "source": "fitness"}
    p.write_text("{not json")
    record_fitness("m", fit.run_fitness(Fit(), "m"), path=p)
    assert "fitness" in json.loads(p.read_text())["m"]


def _inv():
    return inv_mod.collect_inventory(
        make_probes(ollama={"m:7b": {"caps": ["completion", "tools"], "ctx": 32768}}), profiles={})


def test_run_calibration_fitness_flag(tmp_path):
    p = tmp_path / "e.json"
    both = FakeModel()
    both_chat = both.chat
    f = Fit()

    class Mixed:
        supports_tools = supports_images = True

        def chat(self, model, messages, **kw):
            r = both_chat(model, messages, **kw)
            return f.chat(model, messages, **kw) if r.error == "unexpected prompt" else r

    off = cal.run_calibration(_inv(), completer_for=lambda m: Mixed(), eligibility_path=p)
    assert off.fitness == {} and not p.exists()
    on = cal.run_calibration(_inv(), completer_for=lambda m: Mixed(), fitness=True,
                             eligibility_path=p)
    assert set(on.fitness["ollama/m:7b"]) >= {"json", "hebrew_qa"}
    assert "fitness" in json.loads(p.read_text())["ollama/m:7b"]
