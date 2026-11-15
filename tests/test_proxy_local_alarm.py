"""PR-A A2: local-serving alarm (proxy/local_alarm.py, LLM_ROUTER_LOCAL_ALARM) and its status-line segment."""

from __future__ import annotations

import json
import os
import stat


from llm_router.observability import surface_status
from llm_router.proxy import local_alarm
from llm_router.proxy.local_alarm import LocalAlarm
from tests import test_proxy_local_mode as T

Backend, _first_call, _post, _ollama, _call = T.Backend, T._first_call, T._post, T._ollama, T._call
env = T.env
_overrides_readable = T._overrides_readable


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _state(home):
    return json.loads((home / "local_alarm.json").read_text())


def _codes(home):
    return [a["code"] for a in _state(home)["active"]]


def _log(home):
    return [json.loads(x) for x in (home / "proxy_alarms.jsonl").read_text().splitlines()]


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_LOCAL_ALARM", raising=False)
    assert not local_alarm.enabled()
    monkeypatch.setenv("LLM_ROUTER_LOCAL_ALARM", "1")
    assert local_alarm.enabled()


def test_kill_switch_raises_after_30_min_and_clears(tmp_path):
    clk, kill = Clock(), tmp_path / "kill"
    kill.write_text("")
    os.utime(kill, (clk.t - 1700, clk.t - 1700))
    a = LocalAlarm(tmp_path, clock=clk)
    assert a.evaluate(kill_path=kill, backend_open_s=None, quality_open=False) == []
    os.utime(kill, (clk.t - 1900, clk.t - 1900))
    assert a.evaluate(kill_path=kill, backend_open_s=None, quality_open=False) == ["kill_switch"]
    kill.unlink()
    assert a.evaluate(kill_path=kill, backend_open_s=None, quality_open=False) == []
    assert [(x["code"], x["event"]) for x in _log(tmp_path)] == [("kill_switch", "raise"), ("kill_switch", "clear")]
    assert _codes(tmp_path) == []


def test_backend_breaker_after_15_min(tmp_path):
    clk = Clock()
    a = LocalAlarm(tmp_path, clock=clk)
    assert a.evaluate(kill_path=None, backend_open_s=0.0, quality_open=False) == []
    clk.t += 14 * 60
    assert a.evaluate(kill_path=None, backend_open_s=clk.t - 1_000_000.0, quality_open=False) == []
    clk.t += 2 * 60
    assert a.evaluate(kill_path=None, backend_open_s=clk.t - 1_000_000.0, quality_open=False) == ["backend_breaker"]
    assert _state(tmp_path)["active"][0]["since"] == 1_000_000.0
    assert a.evaluate(kill_path=None, backend_open_s=None, quality_open=False) == []
    assert _log(tmp_path)[-1] == {"ts": clk.t, "code": "backend_breaker", "event": "clear"}


def test_backend_breaker_open_before_first_evaluation(tmp_path):
    # an idle proxy evaluates late: the age comes from the breaker, not from the first evaluation
    clk = Clock()
    a = LocalAlarm(tmp_path, clock=clk)
    assert a.evaluate(kill_path=None, backend_open_s=20 * 60.0, quality_open=False) == ["backend_breaker"]
    assert _state(tmp_path)["active"][0]["since"] == clk.t - 20 * 60


def test_quality_breaker_immediate(tmp_path):
    a = LocalAlarm(tmp_path, clock=Clock())
    assert a.evaluate(kill_path=None, backend_open_s=None, quality_open=True) == ["quality_breaker"]
    assert a.evaluate(kill_path=None, backend_open_s=None, quality_open=False) == []


def test_no_local_needs_20_candidates_and_zero_served(tmp_path):
    clk = Clock()
    a = LocalAlarm(tmp_path, clock=clk)
    for _ in range(19):
        a.note_step(True, False)
    a.note_step(False, False)  # not a candidate: does not count
    assert a.evaluate(kill_path=None, backend_open_s=None, quality_open=False) == []
    a.note_step(True, False)
    assert a.evaluate(kill_path=None, backend_open_s=None, quality_open=False) == ["no_local"]
    a.note_step(True, True)  # one served local clears it
    assert a.evaluate(kill_path=None, backend_open_s=None, quality_open=False) == []
    # the window is 60 min: old steps age out
    b = LocalAlarm(tmp_path / "b", clock=clk)
    for _ in range(25):
        b.note_step(True, False)
    clk.t += 3601
    assert b.evaluate(kill_path=None, backend_open_s=None, quality_open=False) == []


def test_silenced_by_maintenance_file(tmp_path):
    clk = Clock()
    a = LocalAlarm(tmp_path, clock=clk)
    (tmp_path / "local_alarm_maintenance.json").write_text(json.dumps({"maintenance_until": clk.t + 60}))
    a.evaluate(kill_path=None, backend_open_s=None, quality_open=True)
    st = _state(tmp_path)
    assert st["silenced"] is True and _codes(tmp_path) == ["quality_breaker"]
    clk.t += 120
    a.evaluate(kill_path=None, backend_open_s=None, quality_open=True)
    assert _state(tmp_path)["silenced"] is False


def test_files_are_0600_and_state_is_atomic(tmp_path):
    a = LocalAlarm(tmp_path, clock=Clock())
    a.evaluate(kill_path=None, backend_open_s=None, quality_open=True)
    assert _mode(tmp_path / "local_alarm.json") == 0o600 and _mode(tmp_path / "proxy_alarms.jsonl") == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_evaluates_at_most_once_per_300s_and_swallows_errors(tmp_path):
    clk = Clock()
    a = LocalAlarm(tmp_path, clock=clk)
    assert a.maybe_evaluate(None, False, True) is True
    clk.t += 299
    assert a.maybe_evaluate(None, False, True) is False
    clk.t += 2

    def boom(**k):
        raise OSError("disk")

    a.evaluate = boom
    assert a.maybe_evaluate(None, False, True) is False and a.errors == 1


def test_status_segment(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_STATE_DIR", str(tmp_path / "elsewhere"))  # the alarm file is read from the home
    assert surface_status.local_alarm_segment(1000.0) == ""  # absent file
    path = tmp_path / "local_alarm.json"
    path.write_text(json.dumps({"ts": 1000.0, "active": [{"code": "no_local", "since": 1000.0 - 42 * 60}], "silenced": False}))
    assert surface_status.local_alarm_segment(1060.0) == "LOCAL DOWN no_local 43m"
    assert surface_status.local_alarm_segment(1000.0 + 901) == ""  # stale
    path.write_text(json.dumps({"ts": 1000.0, "active": [{"code": "no_local", "since": 0}], "silenced": True}))
    assert surface_status.local_alarm_segment(1001.0) == ""
    path.write_text("{not json")
    assert surface_status.local_alarm_segment(1001.0) == ""


async def test_flag_off_proxy_writes_nothing(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.delenv("LLM_ROUTER_LOCAL_ALARM", raising=False)
    app = env.app(Backend(_ollama("", [_call("Bash", {"command": "x"})])))
    assert (await _post(app, _first_call())).status_code == 200
    assert not (home / "local_alarm.json").exists() and not (home / "proxy_alarms.jsonl").exists()


async def test_flag_on_proxy_writes_state_and_kill_switch_alarm(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_ALARM", "1")
    env.kill.write_text("")
    os.utime(env.kill, (1.0, 1.0))  # existed far longer than 30 min
    app = env.app(Backend(_ollama("", [_call("Bash", {"command": "x"})])))
    assert (await _post(app, _first_call())).status_code == 200
    assert _codes(home) == ["kill_switch"] and _mode(home / "local_alarm.json") == 0o600


async def test_alarm_exception_never_fails_the_request(env, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_ALARM", "1")

    def boom(self, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(LocalAlarm, "evaluate", boom)
    app = env.app(Backend(_ollama("", [_call("Bash", {"command": "x"})])))
    r = await _post(app, _first_call())
    assert r.status_code == 200 and env.rows()[0]["decision"] == "served"
