"""D-601 runner auto-recovery: fakes only (fake pidfile, lsof/pgrep/ps output, kill and reload). No real process or port."""
import asyncio
import signal

import pytest

from llm_router.proxy import backend_recycle as br
from llm_router.proxy import runner_recover as rr


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class Fake:
    def __init__(self, listener="p50274\n", children="60001\n", ps="50274 /x/llama-server --port 5\n"):
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


@pytest.mark.parametrize("ps", ["50274 /usr/bin/python llama-server.py\n", "50274 /bin/vim llama-server\n",
                                "50274 /usr/bin/ollama serve\n", "999 /x/llama-server\n"])
async def test_child_matched_by_executable_and_parent(pidfile, ps):
    f = Fake(ps=ps)
    assert (await make(pidfile, f)())["reason"] == "no_runner_child"
    assert f.killed == []


async def test_ollama_runner_argv_is_accepted(pidfile):
    f = Fake(ps="50274 /usr/local/bin/ollama runner --model x\n")
    assert (await make(pidfile, f)())["runner_recover"] == "ok"


async def test_child_changed_before_kill_is_not_signalled(pidfile):
    f = Fake()
    calls = {"n": 0}
    orig = f.run

    async def run(argv):
        if argv[0] == "ps":
            calls["n"] += 1
            if calls["n"] == 2:   # the re-read just before the kill
                return "1 /x/llama-server\n"
        return await orig(argv)
    f.run = run
    r = await make(pidfile, f)()
    assert (r["runner_recover"], r["reason"]) == ("skipped", "child_changed")
    assert f.killed == [] and f.posts == []


@pytest.mark.parametrize("model,ctx", [("", 131072), ("m", None), ("m", 0)])
async def test_no_model_or_ctx_skips_before_kill(pidfile, model, ctx):
    f = Fake()
    rec = rr.RunnerRecover(pidfile, "http://127.0.0.1:11435", model, ctx,
                           run=f.run, kill=f.kill, reload=f.reload, clock=Clock())
    r = await rec()
    assert (r["runner_recover"], r["reason"]) == ("skipped", "no_model")
    assert f.killed == [] and f.posts == []
    assert rec.starts == []


async def test_build_app_crash_signature_trip_runs_recover_action(tmp_path, monkeypatch, pidfile):
    """build_app -> a Compute-error reply trips the breaker -> the recover action runs once (fakes)."""
    from llm_router.proxy import server as ps
    from tests.test_proxy import _post, _req
    from tests.test_proxy_backend_health import _app

    f = Fake()
    real = rr.RunnerRecover
    monkeypatch.setattr(rr, "RunnerRecover", lambda *a, **k: real(*a, run=f.run, kill=f.kill, reload=f.reload))
    monkeypatch.delenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", raising=False)
    monkeypatch.delenv("LLM_ROUTER_RUNNER_AUTORECOVER", raising=False)
    monkeypatch.setenv("LLM_ROUTER_RUNNER_PIDFILE", pidfile)

    async def _choose(text, pinned, **_kw):
        return {"task_type": "code", "complexity": "moderate",
                "chain_head": ["ollama/fake:1"], "model": "ollama/fake:1"}
    monkeypatch.setattr(ps, "choose_model", _choose)

    class Wedged:
        calls = 0

        async def complete(self, body, timeout_s):
            Wedged.calls += 1
            return None, "ollama error: decode() failed: Compute error.", {}

        async def probe(self, timeout_s):
            return False, "compute error"

    app = _app(tmp_path, Wedged(), model="ollama/fake:1", num_ctx=131072, backend_fail_n=3)
    r = await _post(app, _req())
    assert r.status_code == 200   # Claude answered
    await asyncio.wait_for(app.state.recycler.task, 5)
    from llm_router.proxy import ledger
    # event rows are dropped by read_rows by default (fix/ledger-event-rows); this test asserts on one
    rows = ledger.read_rows(tmp_path / "proxy_calls.jsonl", include_events=True)
    trip = [r for r in rows if r.get("backend_health", {}).get("trigger") == "crash_signature"]
    event = [r for r in rows if r.get("event") == "backend_recycle"]
    assert len(trip) == 1 and trip[0]["backend_health"]["recycle"] == "started"
    assert len(event) == 1 and event[0]["backend_health"]["runner_recover"] == "ok"
    assert f.killed == [(60001, signal.SIGTERM)] and len(f.posts) == 1
    assert f.posts[0][1]["model"] == "fake:1"
    await app.state.recycler.aclose()


# D-650: the wider trigger. Fakes only: tmp_path files stand in for the runner log; nothing is killed.
OOM = b"ggml_metal: kIOGPUCommandBufferCallbackErrorOutOfMemory\n"


def test_strong_triggers_always_recover_even_with_flag_off(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_RUNNER_RECOVER_ON_EMPTY", raising=False)
    for t in ("fast_empty", "crash_signature", "empty_burst"):
        assert rr.trip_wants_recover(t, None)


def test_slow_empty_trip_needs_the_flag(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_RUNNER_RECOVER_ON_EMPTY", raising=False)
    assert not rr.trip_wants_recover("consecutive_invalid", "empty_body")
    monkeypatch.setenv("LLM_ROUTER_RUNNER_RECOVER_ON_EMPTY", "1")
    assert rr.trip_wants_recover("consecutive_invalid", "empty_body")
    assert rr.trip_wants_recover("consecutive_invalid", "runner_compute_error")


@pytest.mark.parametrize("trigger,kind", [("consecutive_invalid", "xml_parse"), ("consecutive_invalid", "other"),
                                          ("consecutive_invalid", "step_budget_exceeded"), (None, "empty_body")])
def test_wide_trigger_ignores_other_kinds(trigger, kind):
    assert not rr.trip_wants_recover(trigger, kind, widen=True)


def test_log_watch_needs_new_oom_lines(tmp_path):
    log = tmp_path / "server.log"
    log.write_bytes(OOM * 5)                      # old wedge, before the proxy started
    w = rr.OomLogWatch(str(log))
    assert not rr.trip_wants_recover("consecutive_invalid", "empty_body", watch=w, widen=True)
    with open(log, "ab") as f:
        f.write(b"normal line\n" + OOM)
    assert rr.trip_wants_recover("consecutive_invalid", "empty_body", watch=w, widen=True)
    w.mark()                                      # a repair started: that OOM is spent
    assert not rr.trip_wants_recover("consecutive_invalid", "empty_body", watch=w, widen=True)


def test_log_watch_reads_logpath_indirection_and_replaced_log(tmp_path):
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    a.write_bytes(b"x" * 100)
    lp = tmp_path / "server.logpath"
    lp.write_text(str(a))
    w = rr.OomLogWatch(str(lp))
    lp.write_text(str(b))                         # runner restarted into a smaller new log
    b.write_bytes(OOM)
    assert w.grew()


def test_log_watch_fail_closed_on_missing_log(tmp_path):
    w = rr.OomLogWatch(str(tmp_path / "nope.log"))
    assert not w.grew()
    assert not rr.trip_wants_recover("consecutive_invalid", "empty_body", watch=w, widen=True)


async def test_cooldown_and_mismatched_child_still_hold_for_wide_trigger(pidfile):
    """The repair itself is unchanged: second start inside 10 min is rate-limited, a foreign child is refused."""
    f = Fake(ps="1 /x/llama-server --port 5\n")   # ppid 1 != pidfile pid
    rec = br.BackendRecycle("", clock=Clock(), action=make(pidfile, f))
    emitted = []
    assert rec.start(emitted.append)["recycle"] == "started"
    await rec.task
    assert emitted[0]["reason"] == "no_runner_child" and f.killed == []
    assert rec.start(emitted.append)["recycle"] == "skipped_ratelimit"
