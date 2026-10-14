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


class Mixed:
    """Passes the calibration probes (FakeModel) and the fitness tasks (Fit)."""

    supports_tools = supports_images = True

    def __init__(self):
        self.probe, self.fit = FakeModel(), Fit()

    def chat(self, model, messages, **kw):
        r = self.probe.chat(model, messages, **kw)
        return self.fit.chat(model, messages, **kw) if r.error == "unexpected prompt" else r


def _inv():
    return inv_mod.collect_inventory(
        make_probes(ollama={"m:7b": {"caps": ["completion", "tools"], "ctx": 32768}}), profiles={})


def test_run_calibration_fitness_flag(tmp_path):
    p = tmp_path / "e.json"
    off = cal.run_calibration(_inv(), completer_for=lambda m: Mixed(), eligibility_path=p)
    assert off.fitness == {} and not p.exists()
    on = cal.run_calibration(_inv(), completer_for=lambda m: Mixed(), fitness=True,
                             eligibility_path=p)
    assert set(on.fitness["ollama/m:7b"]) >= {"json", "hebrew_qa"}
    # Keyed by the bare Ollama name: the key resource_map reads.
    assert "fitness" in json.loads(p.read_text())["m:7b"]


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class Slow(Fit):
    """Each reply costs ``step`` seconds of the fake clock; records each timeout."""

    def __init__(self, clock, step):
        super().__init__()
        self.clock, self.step, self.timeouts = clock, step, []

    def chat(self, model, messages, *, timeout=90, **kw):
        self.timeouts.append(timeout)
        self.clock.t += self.step
        return super().chat(model, messages, timeout=timeout)


def test_fitness_stops_at_the_deadline_and_caps_each_call():
    clock = Clock()
    c = Slow(clock, step=40)
    res = fit.run_fitness(c, "m", timeout=90, deadline=100.0, clock=clock)
    # t=0 -> 40 -> 80; at t=80 the third call gets only the 20 s left; at t=120 stop.
    assert len(res) == 3 and c.timeouts == [90, 60, 20]


def test_run_calibration_fitness_respects_the_budget(tmp_path):
    clock = Clock()
    seen = []

    class Budgeted(Fit):
        def chat(self, model, messages, *, timeout=90, **kw):
            seen.append(timeout)
            clock.t += 30
            return super().chat(model, messages, timeout=timeout)

    cal.run_calibration(_inv(), completer_for=lambda m: Budgeted(), fitness=True,
                        budget_s=200, clock=clock, eligibility_path=tmp_path / "e.json")
    assert clock.t <= 200 + 30 and all(t <= 200 for t in seen)


def test_inconclusive_run_keeps_the_earlier_measurement(tmp_path):
    p = tmp_path / "e.json"
    assert record_fitness("m", fit.run_fitness(Fit(), "m"), path=p) is True
    before = json.loads(p.read_text())["m"]["fitness"]
    assert record_fitness("m", fit.run_fitness(Fit(error=cal.ERR_PROVIDER), "m"), path=p) is False
    assert json.loads(p.read_text())["m"]["fitness"] == before


def test_partial_run_merges_per_task(tmp_path):
    p = tmp_path / "e.json"
    record_fitness("m", fit.run_fitness(Fit(), "m"), path=p)
    only_json = tuple(t for t in fit.TASKS if t.task_type == "json")
    record_fitness("m", fit.run_fitness(Fit(good=False), "m", tasks=only_json), path=p)
    stored = json.loads(p.read_text())["m"]["fitness"]
    assert stored["json"]["score"] == 0.0 and stored["hebrew_qa"]["score"] == 1.0


def test_storage_error_keeps_the_profiles(tmp_path, monkeypatch):
    from llm_router.resolver import eligibility_store

    def boom(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(eligibility_store, "record_fitness", boom)
    rep = cal.run_calibration(_inv(), completer_for=lambda m: Mixed(), fitness=True,
                              eligibility_path=tmp_path / "e.json")
    assert "ollama/m:7b" in rep.profiles
    assert rep.fitness_errors == {"ollama/m:7b": "PermissionError"}


def test_write_failure_leaves_no_temp_file(tmp_path, monkeypatch):
    p = tmp_path / "e.json"

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("llm_router.resolver.eligibility_store.os.replace", boom)
    with pytest.raises(OSError):
        record_fitness("m", fit.run_fitness(Fit(), "m"), path=p)
    assert [f.name for f in tmp_path.iterdir() if f.name.endswith(".tmp")] == []


def test_fitness_runs_only_on_local_models(tmp_path):
    from dataclasses import replace

    inv = _inv()
    inv.models[:] = [replace(m, route_kind="api") for m in inv.models]
    calls = []

    class Count(Mixed):
        def chat(self, model, messages, **kw):
            calls.append(messages[-1]["content"])
            return super().chat(model, messages, **kw)

    rep = cal.run_calibration(inv, completer_for=lambda m: Count(), fitness=True, allow_paid=True,
                              eligibility_path=tmp_path / "e.json")
    assert rep.fitness == {} and not any("Haifa" in c for c in calls)


def test_cli_fitness_flag():
    from llm_router.commands import calibrate as cmd

    assert cmd._parse(["--fitness"])["fitness"] is True
    assert cmd._parse([])["fitness"] is False
