"""P12: priority classes, yield signal, abort primitive, stale waiters for the local-model gate."""
from __future__ import annotations

import http.server
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from llm_router.toolkit import local_gate as g
from llm_router.toolkit import local_gate_abort as ab

SRC = str(Path(__file__).resolve().parent.parent / "src")
URL = "http://localhost:59999"  # never contacted by the gate; no server needed

WAITER = textwrap.dedent("""
    import sys, time, os
    from llm_router.toolkit import local_gate as g
    label, prio, out = sys.argv[2], sys.argv[3], sys.argv[4]
    a = g.admit(sys.argv[1], time.monotonic() + 30, priority=prio)
    fd = os.open(out, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    os.write(fd, (label + "\\n").encode())
    os.close(fd)
    a.release()
""")

TICKET_ONLY = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from llm_router.toolkit import local_gate as g
    d = g.gate_dir(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
    g._Ticket(d, sys.argv[2])
    print("TICKET", flush=True)
    time.sleep(60)
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_SLOTS", "1")
    monkeypatch.setenv("PYTHONPATH", SRC)
    g._FS_CACHE.clear()
    monkeypatch.setattr(g, "_fstype_for", lambda p: "apfs")
    return tmp_path


@pytest.fixture
def procs():
    live = []
    yield live
    for p in live:
        if p.poll() is None:
            p.kill()
        p.wait()
        if p.stdout:
            p.stdout.close()


def _tickets(url=URL):
    return sorted((g.gate_dir(url) / "wait").glob("*.wait"))


def _expect_timeout(priority, secs=0.5):
    def run():
        with pytest.raises(g.LocalQueueTimeout):
            g.admit(URL, time.monotonic() + secs, priority=priority)
    return run


def _admit_release(priority):
    def run():
        g.admit(URL, time.monotonic() + 10, priority=priority).release()
    return run


def _wait_for(pred, secs=15.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _spawn_waiter(procs, label, prio, out):
    p = subprocess.Popen([sys.executable, "-c", WAITER, URL, label, prio, str(out)],
                         env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    procs.append(p)
    return p


def test_interactive_before_background_fifo_within_class_across_processes(env, procs):
    out = env / "order.txt"
    holder = g.admit(URL, time.monotonic() + 5, priority="background")
    order = [("bg1", "background"), ("bg2", "background"), ("int1", "interactive"), ("int2", "interactive")]
    for i, (label, prio) in enumerate(order, start=1):
        _spawn_waiter(procs, label, prio, out)
        assert _wait_for(lambda: len(_tickets()) == i), f"ticket {label} never appeared"
    holder.release()
    for p in procs:
        assert p.wait(timeout=30) == 0, p.stderr.read()
    assert out.read_text().split() == ["int1", "int2", "bg1", "bg2"]
    assert _tickets() == []  # every ticket removed


def test_unprioritised_path_has_no_tickets(env):
    a = g.admit(URL, time.monotonic() + 2)
    assert a.priority is None and not (g.gate_dir(URL) / "wait").exists()
    a.release()


def test_invalid_priority_rejected(env):
    with pytest.raises(ValueError):
        g.admit(URL, time.monotonic() + 1, priority="urgent")


def test_should_yield_signals_background_holder_only(env):
    bg = g.admit(URL, time.monotonic() + 2, priority="background")
    assert bg.should_yield() is False
    got = {}

    def waiter():
        try:
            got["adm"] = g.admit(URL, time.monotonic() + 20, priority="interactive")
        except Exception as exc:  # noqa: BLE001
            got["err"] = exc

    t = threading.Thread(target=waiter)
    t.start()
    assert _wait_for(lambda: len(_tickets()) == 1)
    assert _wait_for(bg.should_yield)
    assert g.should_yield(URL) is True
    bg.release()
    t.join(timeout=20)
    inter = got["adm"]
    assert inter.should_yield() is False  # interactive holders never yield
    inter.release()
    assert g.should_yield(URL) is False


def test_background_waiter_does_not_trigger_yield(env):
    bg = g.admit(URL, time.monotonic() + 2, priority="background")
    t = threading.Thread(target=_expect_timeout("background"))
    t.daemon = True
    t.start()
    assert _wait_for(lambda: len(_tickets()) == 1)
    assert bg.should_yield() is False
    t.join(timeout=10)
    bg.release()


def test_stale_waiter_does_not_block_queue_and_is_removed(env, procs):
    holder = g.admit(URL, time.monotonic() + 5, priority="background")
    p = subprocess.Popen([sys.executable, "-c", TICKET_ONLY, URL, "interactive"],
                         env=os.environ.copy(), stdout=subprocess.PIPE, text=True)
    procs.append(p)
    assert p.stdout.readline().strip() == "TICKET"
    stale = _tickets()
    assert len(stale) == 1 and stale[0].name.startswith("0-")
    os.kill(p.pid, signal.SIGKILL)
    p.wait()
    holder.release()
    # The dead interactive ticket outranks us but must not block.
    a = g.admit(URL, time.monotonic() + 10, priority="background")
    a.release()
    assert _tickets() == []
    assert g.should_yield(URL) is False


def test_flag_off_is_old_path(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "")
    a = g.admit(URL, time.monotonic() + 1, priority="interactive")
    b = g.admit(URL, time.monotonic() + 1, priority="background")  # no exclusion at all
    assert a.disabled and b.disabled
    assert not g.gate_dir(URL).exists()
    assert g.should_yield(URL) is False
    assert b.should_yield() is False


def test_async_priority_admit(env):
    import asyncio

    async def run():
        a = await g.aadmit(URL, time.monotonic() + 2, priority="interactive")
        assert a.priority == "interactive"
        a.release()
        assert _tickets() == []

    asyncio.run(run())


# ── abort primitive against a fake local HTTP server ─────────────────────────

class _Slow(http.server.BaseHTTPRequestHandler):
    release = threading.Event()

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.release.wait(30)
        try:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        except OSError:
            pass

    def log_message(self, *a):
        pass


@pytest.fixture
def slow_server():
    _Slow.release = threading.Event()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    assert srv.server_address[1] not in (11434, 11435, 8797)
    yield f"http://127.0.0.1:{srv.server_address[1]}/api/chat"
    _Slow.release.set()
    srv.shutdown()
    srv.server_close()


def _call_in_thread(url, handle):
    res = {}

    def run():
        try:
            res["ok"] = ab.abortable_post(url, b"{}", handle, timeout=30)
        except Exception as exc:  # noqa: BLE001
            res["err"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, res


def test_abort_in_flight_call_via_handle(slow_server):
    h = ab.AbortHandle()
    t, res = _call_in_thread(slow_server, h)
    time.sleep(0.3)  # request is parked in the server handler
    assert t.is_alive()
    h.abort()
    t.join(timeout=10)
    assert not t.is_alive()
    assert isinstance(res.get("err"), ab.LocalCallAborted)


def test_call_completes_when_not_aborted(slow_server):
    _Slow.release.set()
    status, body = ab.abortable_post(slow_server, b"{}", ab.AbortHandle(), timeout=10)
    assert (status, body) == (200, b"ok")


def test_background_call_aborts_when_interactive_waits(env, slow_server):
    bg = g.admit(URL, time.monotonic() + 2, priority="background")
    h = ab.AbortHandle()
    with ab.watch_yield(bg, h, poll_s=0.02):
        t, res = _call_in_thread(slow_server, h)
        time.sleep(0.3)
        assert t.is_alive() and not h.aborted
        w = threading.Thread(target=_admit_release("interactive"), daemon=True)
        w.start()
        t.join(timeout=10)
    assert not t.is_alive()
    assert isinstance(res.get("err"), ab.LocalCallAborted)
    bg.release()
    w.join(timeout=10)


# ── regression: a new ticket must never be reaped before its owner has locked it ──

RACE = textwrap.dedent("""
    import sys, time
    from llm_router.toolkit import local_gate as g
    d = g.gate_dir("http://127.0.0.1:19999")
    if sys.argv[1] == "scan":
        end = time.time() + 3
        while time.time() < end:
            g._live_tickets(d)
            g.should_yield("http://127.0.0.1:19999")
    else:
        err = lost = n = 0
        end = time.time() + 2.5
        while time.time() < end:
            n += 1
            try:
                t = g._Ticket(d, "interactive")
            except OSError:
                err += 1
                continue
            if not t.path.exists():
                lost += 1
            t.close()
        print(n, err, lost)
""")


def test_new_ticket_not_reaped_by_concurrent_scanners(env):
    cmd = [sys.executable, "-c", RACE]
    scanners = [subprocess.Popen(cmd + ["scan"], env=os.environ.copy(), stderr=subprocess.PIPE)
                for _ in range(4)]
    creator = subprocess.run(cmd + ["create"], env=os.environ.copy(), capture_output=True, text=True, timeout=60)
    for p in scanners:
        assert p.wait(timeout=60) == 0, p.stderr.read()
    assert creator.returncode == 0, creator.stderr
    n, err, lost = map(int, creator.stdout.split())
    assert n > 100  # the check found something to check
    assert (err, lost) == (0, 0)


def test_ticket_creation_failure_fails_open(env, monkeypatch):
    def boom(*a, **k):
        raise OSError(28, "no space")
    monkeypatch.setattr(g, "_Ticket", boom)
    a = g.admit(URL, time.monotonic() + 1, priority="interactive")
    assert a.disabled
    a.release()


def test_abort_during_connect_is_caught(slow_server):
    h = ab.AbortHandle()
    real = ab.http.client.HTTPConnection.connect

    def connect_then_abort(self):
        h.abort()  # closers run while conn.sock is still None
        real(self)

    ab.http.client.HTTPConnection.connect = connect_then_abort
    try:
        with pytest.raises(ab.LocalCallAborted):
            ab.abortable_post(slow_server, b"{}", h, timeout=5)
    finally:
        ab.http.client.HTTPConnection.connect = real


def test_stale_tmp_ticket_reaped_fresh_and_held_kept(env):
    """A creator killed between create and rename leaves ``.tmp-*``; the next ticket removes it.
    A fresh one (creator may not have locked it yet) and a locked one stay."""
    w = g.gate_dir(URL) / "wait"
    w.mkdir(parents=True, exist_ok=True)
    old = time.time() - g.STALE_TMP_S - 5
    stale = w / ".tmp-1-stale.wait"
    stale.touch()
    os.utime(stale, (old, old))
    fresh = w / ".tmp-1-fresh.wait"
    fresh.touch()
    held = w / ".tmp-1-held.wait"
    held.touch()
    os.utime(held, (old, old))
    fd = os.open(str(held), os.O_RDWR)
    try:
        assert g._try_lock(fd)
        a = g.admit(URL, time.monotonic() + 1, priority="interactive")
        a.release()
        assert not stale.exists()
        assert fresh.exists()
        assert held.exists()
    finally:
        os.close(fd)
