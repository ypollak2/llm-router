"""D-601 runner auto-recovery: fakes only (fake pidfile, lsof/pgrep/ps output, kill and reload). No real process or port."""
import signal

import pytest

from llm_router.proxy import backend_recycle as br
from llm_router.proxy import runner_recover as rr


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class Fake:
    def __init__(self, listener="p50274\n", children="60001\n", ps="/x/llama-server --port 5\n"):
        self.out = {"lsof": listener, "pgrep": children, "ps": ps}
        self.argvs, self.killed, self.posts = [], [], []
        self.reload_ok = True

    async def run(self, argv):
        self.argvs.append(argv)
        return self.out[argv[0]]

    def kill(self, pid, sig):
        self.killed.append((pid, sig))

    async def reload(self, url, body):
        self.posts.append((url, body))
        return self.reload_ok


@pytest.fixture
def pidfile(tmp_path):
    p = tmp_path / "ollama.pid"
    p.write_text("50274\n")
    return str(p)


def make(pidfile, fake, clock=None):
    return rr.RunnerRecover(pidfile, "http://127.0.0.1:11435/", "ollama/qwen3", 131072,
                            run=fake.run, kill=fake.kill, reload=fake.reload, clock=clock or Clock())


async def test_happy_path_kills_only_child_and_reloads(pidfile):
    f = Fake()
    r = await make(pidfile, f)()
    assert (r["runner_recover"], r["reason"], r["recycle"]) == ("ok", "reloaded", "ok")
    assert f.killed == [(60001, signal.SIGTERM)]
    assert f.argvs[0] == ["lsof", "-nP", "-iTCP:11435", "-sTCP:LISTEN", "-Fp"]
    assert f.posts == [("http://127.0.0.1:11435/api/generate",
                        {"model": "qwen3", "prompt": "", "keep_alive": -1, "options": {"num_ctx": 131072}})]


@pytest.mark.parametrize("listener", ["p999\n", "", "p50274\np999\n"])
async def test_unverified_listener_does_nothing(pidfile, listener):
    f = Fake(listener=listener)
    r = await make(pidfile, f)()
    assert (r["runner_recover"], r["reason"]) == ("skipped", "listener_not_pidfile_pid")
    assert f.killed == [] and f.posts == []


@pytest.mark.parametrize("content", [None, "", "abc", "1"])
async def test_missing_or_bad_pidfile_does_nothing(tmp_path, content):
    p = tmp_path / "pid"
    if content is not None:
        p.write_text(content)
    f = Fake()
    r = await make(str(p), f)()
    assert (r["runner_recover"], r["reason"]) == ("skipped", "pidfile_missing_or_invalid")
    assert f.killed == [] and f.argvs == []


async def test_child_must_be_a_runner(pidfile):
    f = Fake(ps="/usr/bin/python evil\n")
    r = await make(pidfile, f)()
    assert (r["runner_recover"], r["reason"]) == ("skipped", "no_runner_child")
    assert f.killed == []


async def test_reload_failure_is_a_failed_row(pidfile):
    f = Fake()
    f.reload_ok = False
    assert (await make(pidfile, f)())["reason"] == "reload_failed"


async def test_hourly_cap_then_alarm_then_window_slides(pidfile):
    f, clock = Fake(), Clock()
    rec = make(pidfile, f, clock)
    for _ in range(3):
        assert (await rec())["runner_recover"] == "ok"
        clock.t += 601
    capped = await rec()
    assert (capped["reason"], capped["alarm"]) == ("hourly_cap", True)
    assert len(f.killed) == 3
    clock.t += 3600
    assert (await rec())["runner_recover"] == "ok"


async def test_recycler_uses_action_with_ten_minute_limit(pidfile):
    f, clock = Fake(), Clock()
    recycler = br.BackendRecycle("", clock=clock, action=make(pidfile, f, clock))
    events = []
    assert recycler.start(events.append) == {"recycle": "started", "exit_code": None, "duration_s": 0.0}
    await recycler.task
    assert events[0]["runner_recover"] == "ok"
    clock.t += 599
    assert recycler.start(events.append)["recycle"] == "skipped_ratelimit"
    assert len(f.killed) == 1


def test_flag_default_on_and_killable(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_RUNNER_AUTORECOVER", raising=False)
    assert rr.enabled()
    for v in ("0", "off", "false"):
        monkeypatch.setenv("LLM_ROUTER_RUNNER_AUTORECOVER", v)
        assert not rr.enabled()
