"""Offline self-heal cases: fake clocks, transports and a tmp-dir marker script."""
import asyncio
import shlex
import subprocess
import sys

import pytest

from llm_router.proxy import backend_health as bh
from llm_router.proxy import backend_recycle as br
from llm_router.proxy import backends as pb
from tests.test_proxy import _call, _ollama, _post, _req, _rows
from tests.test_proxy_backend_health import Clock, _app, _ollama_client, policy  # noqa: F401


@pytest.fixture
def command(tmp_path):
    script = tmp_path / "fake recycle.py"
    marker = tmp_path / "recycled"
    script.write_text("from pathlib import Path\nimport sys\n"
                      "with Path(sys.argv[1]).open('a') as f: f.write('recycled\\n')\n")
    return shlex.join([sys.executable, str(script), str(marker)]), marker


@pytest.mark.parametrize("elapsed", [0.0, 0.999, 1.0, 2.0])
async def test_fast_empty_threshold(elapsed):
    clock = Clock()
    health = bh.BackendHealth(clock=clock, fast_empty=True, warn=lambda _: None)
    t0 = clock()
    clock.t += elapsed
    trip = health.record("m", "zero-line stream", "validation", clock() - t0)
    if elapsed < 1:
        assert trip == {"state": "tripped", "trigger": "fast_empty"}
    else:
        assert trip is None
        assert health.record("m", "zero-line stream", "validation", elapsed) is None
        assert health.record("m", "zero-line stream", "validation", elapsed)["trigger"] == "consecutive_invalid"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("lines", [[], [{"done": True, "message": {"content": ""}}]])
async def test_stream_signal_only_for_zero_lines(monkeypatch, enabled, lines):
    monkeypatch.setenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", "fake" if enabled else "")
    client, _ = _ollama_client(200, lines)
    backend = pb.OllamaBackend("ollama/x", client, base_url="http://offline.invalid", num_ctx=32768)
    _, err, usage = await backend.complete(_req(), 5)
    assert err == "empty response"
    assert ("zero_line_s" in usage) == (enabled and not lines)


async def test_command_once_per_ten_minutes(command):
    cmd, marker = command
    clock = Clock()
    recycler = br.BackendRecycle(cmd, clock=clock)
    assert (await recycler.recycle())["recycle"] == "ok"
    clock.t += 599
    assert (await recycler.recycle())["recycle"] == "skipped_ratelimit"
    assert marker.read_text().splitlines() == ["recycled"]
    clock.t += 1
    assert (await recycler.recycle())["recycle"] == "ok"
    assert marker.read_text().splitlines() == ["recycled", "recycled"]


async def test_unset_or_empty_never_runs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("command must not run")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    for value in (None, "", "  "):
        if value is None:
            monkeypatch.delenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", raising=False)
        else:
            monkeypatch.setenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", value)
        assert await br.BackendRecycle(br.recycle_command()).recycle() is None
        h = bh.BackendHealth(fast_empty=bool(br.recycle_command()), warn=lambda _: None)
        assert h.record("m", "empty response", "validation", 0.1) is None


async def test_inflight_skipped_and_recorded(monkeypatch, command):
    cmd, marker = command
    monkeypatch.setattr(br, "INFLIGHT_WAIT_S", 0)
    recycler = br.BackendRecycle(cmd)
    async with recycler.request():
        row = await recycler.recycle()
    assert row == {"recycle": "skipped_inflight", "exit_code": None, "duration_s": 0.0}
    assert not marker.exists()
    assert recycler.ready.is_set()


async def test_waits_for_inflight_and_blocks_new_requests(command):
    cmd, marker = command
    recycler = br.BackendRecycle(cmd)
    async with recycler.request():
        recycling = asyncio.create_task(recycler.recycle())
        await asyncio.sleep(0)
        assert not recycler.ready.is_set()
        assert not marker.exists()
        entered = asyncio.Event()

        async def newcomer():
            async with recycler.request():
                assert marker.exists()
                entered.set()
        waiting = asyncio.create_task(newcomer())
        await asyncio.sleep(0)
        assert not entered.is_set()
    assert (await recycling)["recycle"] == "ok"
    await waiting
    assert recycler.inflight == 0


@pytest.mark.parametrize("mode", ["nonzero", "timeout", "bad_argv", "missing"])
async def test_failed_exit_timeout_and_argv(monkeypatch, mode):
    clock = Clock()
    seen = []

    async def spawn(*argv, **kwargs):
        seen.append(argv)
        assert argv == ("fake runner", "--recycle")
        assert kwargs == {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if mode == "missing":
            raise FileNotFoundError("must never be stored")

        class Process:
            returncode = None

            async def wait(self):
                if self.returncode is not None:
                    return self.returncode
                clock.t += 4
                if mode == "timeout":
                    raise asyncio.TimeoutError
                self.returncode = 7
                return 7

            def kill(self):
                self.returncode = -9

        return Process()

    assert br.COMMAND_TIMEOUT_S == 60.0
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    recycler = br.BackendRecycle("'fake runner' --recycle" if mode != "bad_argv" else "'", clock=clock)
    result = await recycler.recycle()
    assert result == {"recycle": "failed", "exit_code": 7 if mode == "nonzero" else None,
                      "duration_s": 0.0 if mode in {"bad_argv", "missing"} else 4.0}
    assert len(seen) == (0 if mode == "bad_argv" else 1)
    assert (await recycler.recycle())["recycle"] == "skipped_ratelimit"


class StubBackend:
    def __init__(self, clock, marker):
        self.clock, self.marker = clock, marker
        self.calls = self.probes = 0
        self.probe_ok = True

    async def complete(self, body, timeout_s):
        self.calls += 1
        if not self.marker.exists():
            return None, "empty response", {"zero_line_s": 0.1}
        from llm_router.proxy.translate import from_ollama
        msg, err = from_ollama(_ollama("Ready.", [_call("Bash", {"command": "ls"})]), body)
        return msg, err, {}

    async def probe(self, timeout_s):
        self.probes += 1
        return self.probe_ok and self.marker.exists(), "stub"


@pytest.mark.usefixtures("policy")
async def test_offline_drill(tmp_path, monkeypatch, command):
    cmd, marker = command
    monkeypatch.setenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", cmd)
    clock = Clock()
    backend = StubBackend(clock, marker)
    app = _app(tmp_path, backend, clock, loop_max_consecutive=100, loop_repeat_window=0)
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    assert row["backend_health"]["trigger"] == "fast_empty"
    assert row["backend_health"]["recycle"] == "started"
    await app.state.recycler.task
    assert _rows(tmp_path)[-1]["backend_health"]["recycle"] == "ok"
    assert marker.read_text().splitlines() == ["recycled"]
    await _post(app, _req())
    assert backend.calls == 1 and backend.probes == 0
    clock.t += 59
    await _post(app, _req())
    assert backend.probes == 0
    clock.t += 1
    await _post(app, _req())
    assert _rows(tmp_path)[-1]["backend_health"]["state"] == "recovered"
    for _ in range(10):
        clock.t += 60
        await _post(app, _req())
        assert _rows(tmp_path)[-1]["decision"] == "served"
    assert backend.probes == 1
    assert marker.read_text().splitlines() == ["recycled"]


@pytest.mark.usefixtures("policy")
async def test_failed_probe_and_second_crash_do_not_recycle(tmp_path, monkeypatch, command):
    cmd, marker = command
    monkeypatch.setenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", cmd)
    clock = Clock()
    backend = StubBackend(clock, marker)
    app = _app(tmp_path, backend, clock)
    await _post(app, _req())
    await app.state.recycler.task
    backend.probe_ok = False
    clock.t += 60
    await _post(app, _req())
    assert _rows(tmp_path)[-1]["backend_health"]["probe_ok"] is False
    await _post(app, _req())
    assert backend.probes == 1
    backend.probe_ok = True
    clock.t += 60
    await _post(app, _req())
    marker.unlink()
    await _post(app, _req())
    assert _rows(tmp_path)[-1]["backend_health"]["recycle"] == "skipped_ratelimit"
    assert not marker.exists()


@pytest.mark.usefixtures("policy")
async def test_background_returns_deduplicates_and_shutdown_reaps(tmp_path, monkeypatch, command):
    cmd, marker = command
    monkeypatch.setenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", cmd)
    entered = asyncio.Event()
    reaped = asyncio.Event()
    children = []

    async def spawn(*argv, **kwargs):
        class Process:
            returncode = None

            async def wait(self):
                if self.returncode is None:
                    entered.set()
                    await asyncio.Event().wait()
                reaped.set()
                return self.returncode

            def kill(self):
                self.returncode = -9
        child = Process()
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    # Keep startup entirely offline, including the discovery loop.
    async def no_rescan():
        await asyncio.Event().wait()
    monkeypatch.setattr("llm_router.discovery_rescan.rescan_loop", no_rescan)
    app = _app(tmp_path, StubBackend(Clock(), marker), Clock())
    async with app.router.lifespan_context(app):
        response = await asyncio.wait_for(_post(app, _req()), 2)
        assert response.status_code == 200
        recycler = app.state.recycler
        await asyncio.wait_for(entered.wait(), 2)
        assert not recycler.task.done()
        original = recycler.task
        assert recycler.start(lambda _: None)["recycle"] == "skipped_ratelimit"
        assert recycler.task is original
        await asyncio.wait_for(_post(app, _req()), 2)
        assert len(children) == 1
    assert recycler.task.done() and recycler.task.cancelled()
    assert reaped.is_set() and children[0].returncode == -9
    assert recycler.ready.is_set()


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
@pytest.mark.usefixtures("policy")
async def test_failed_recycle_retries_only_after_ten_minutes(tmp_path, monkeypatch, command, failure):
    cmd, marker = command
    monkeypatch.setenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", cmd)
    clock = Clock()
    backend = StubBackend(clock, marker)
    calls = []

    async def run(self):
        calls.append(clock())
        if len(calls) == 1:
            return {"recycle": "failed", "exit_code": 7 if failure == "nonzero" else None,
                    "duration_s": 60.0 if failure == "timeout" else 0.0}
        marker.write_text("recycled\n")
        return {"recycle": "ok", "exit_code": 0, "duration_s": 0.0}

    monkeypatch.setattr(br.BackendRecycle, "_run", run)
    app = _app(tmp_path, backend, clock, loop_max_consecutive=100, loop_repeat_window=0)
    await _post(app, _req())
    await app.state.recycler.task
    assert [r for r in _rows(tmp_path) if r.get("event") == "backend_recycle"][-1]["backend_health"]["recycle"] == "failed"
    for _ in range(9):
        clock.t += 60
        await _post(app, _req())
        assert _rows(tmp_path)[-1]["backend_health"]["probe_ok"] is False
        assert _rows(tmp_path)[-1]["backend_health"]["recycle"] == "skipped_ratelimit"
        assert len(calls) == 1
    clock.t += 60
    await _post(app, _req())
    assert _rows(tmp_path)[-1]["backend_health"]["recycle"] == "started"
    await app.state.recycler.task
    assert len(calls) == 2
    clock.t += 60
    await _post(app, _req())
    assert _rows(tmp_path)[-1]["backend_health"]["state"] == "recovered"
    clock.t += 600
    await _post(app, _req())
    assert len(calls) == 2


@pytest.mark.parametrize("method", ["complete", "probe", "warm_up"])
async def test_all_local_calls_are_tracked_and_release_on_cancel(method):
    entered = asyncio.Event()

    class Backend:
        async def complete(self):
            entered.set()
            await asyncio.Event().wait()

        probe = warm_up = complete

    recycler = br.BackendRecycle("fake")
    task = asyncio.create_task(getattr(recycler.wrap(Backend()), method)())
    await entered.wait()
    assert recycler.inflight == 1 and not recycler.idle.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recycler.inflight == 0 and recycler.idle.is_set()


async def test_cooldown_admits_only_one_concurrent_probe():
    clock = Clock()
    health = bh.BackendHealth(clock=clock, fast_empty=True, warn=lambda _: None)
    health.record("m", "zero-line stream", "validation", 0.1)
    clock.t += 60
    entered, finish = asyncio.Event(), asyncio.Event()

    class Backend:
        calls = 0

        async def probe(self, timeout_s):
            self.calls += 1
            entered.set()
            await finish.wait()
            return True, "ok"

    backend = Backend()
    probing = asyncio.create_task(health.admit("m", backend))
    await entered.wait()
    assert (await health.admit("m", backend))[0] is False
    assert backend.calls == 1
    finish.set()
    assert (await probing)[1]["state"] == "recovered"


async def test_background_exception_is_recorded_and_admission_restored(monkeypatch):
    recycler = br.BackendRecycle("fake")
    events = []

    async def broken():
        raise RuntimeError("must never be stored")

    monkeypatch.setattr(recycler, "_run", broken)
    assert recycler.start(events.append)["recycle"] == "started"
    await recycler.task
    assert events == [{"recycle": "failed", "exit_code": None, "duration_s": 0.0}]
    assert recycler.ready.is_set()
    await recycler.aclose()
