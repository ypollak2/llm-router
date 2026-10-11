"""Ollama watchdog — a server that accepts connections but never generates.

2026-10-02: the owner's Ollama was HUNG (0% CPU, 16 days up, dozens of stale
connections). /api/tags and /api/ps kept answering, so every existing pre-flight
passed and local edits then burned the whole hook deadline. These tests drive a
real local HTTP server whose /api/generate can be made to stall, and assert the
BEHAVIOUR: detection, no false alarm on a healthy or cold server, rate limiting,
the persisted breaker, fail-fast in the two local paths, and that restart is
opt-in and never touches the real Ollama (every process/kill/open call is mocked).
"""
from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llm_router import failopen, ollama_watchdog as wd
from llm_router import zero_claude_edit as zce
from llm_router.hooks import direct_executor
from llm_router.hooks.direct_executor import ModelSpec

MODEL = "wd-test-model:latest"


class _Stub(BaseHTTPRequestHandler):
    ps_models: list = [MODEL]
    hang_generate = False
    hang_ps = False
    generate_reply: dict = {"done": True, "response": "x"}
    posts: list = []
    release = threading.Event()

    def log_message(self, *_a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        cls = type(self)
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": MODEL}]})
        elif self.path.startswith("/api/ps"):
            if cls.hang_ps:
                cls.release.wait(10)
                return
            self._send({"models": [{"name": n, "model": n} for n in cls.ps_models]})
        else:
            self._send({"version": "0"})

    def do_POST(self):
        cls = type(self)
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        cls.posts.append((self.path, payload))
        if self.path == "/api/generate" and cls.hang_generate:
            cls.release.wait(10)      # accepts the request, never answers
            return
        if self.path == "/api/chat":
            self._send({"model": payload.get("model"), "done": True, "eval_count": 5,
                        "prompt_eval_count": 5,
                        "message": {"role": "assistant", "content": json.dumps([{
                            "file": "foo.py", "old_string": "def old_name():\n    pass\n",
                            "new_string": "def new_name():\n    pass\n",
                            "description": "rename"}])}})
            return
        self._send(cls.generate_reply)


def _generates():
    return [p for path, p in _Stub.posts if path == "/api/generate"]


@pytest.fixture
def server(monkeypatch):
    _Stub.ps_models = [MODEL]
    _Stub.hang_generate = False
    _Stub.hang_ps = False
    _Stub.generate_reply = {"done": True, "response": "x"}
    _Stub.posts = []
    _Stub.release = threading.Event()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    url = f"http://{host}:{port}"
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", url)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", MODEL)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG", "on")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_TIMEOUT_S", "0.5")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_FAIL_N", "2")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_MIN_INTERVAL_S", "60")
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_WATCHDOG_RESTART", raising=False)
    try:
        yield url
    finally:
        _Stub.release.set()
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def no_real_process_calls(monkeypatch):
    """Nothing in this file may signal, list, quit or launch a real process."""
    calls: list = []

    def boom(*a, **k):
        raise AssertionError(f"real subprocess/kill attempted: {a} {k}")

    monkeypatch.setattr(wd.subprocess, "run", boom)
    monkeypatch.setattr(wd.os, "kill", boom)
    return calls


# ── the probe ────────────────────────────────────────────────────────────────

@pytest.mark.timing
def test_healthy_server_passes(server):
    r = wd.probe_generation()
    assert r.ok is True and r.model == MODEL
    body = _generates()[0]
    # a 1-token request that does not change how the model is held resident
    assert body["options"]["num_predict"] == 1 and body["stream"] is False
    assert body["keep_alive"] == 600


@pytest.mark.timing
def test_server_that_accepts_but_never_generates_is_detected_as_hung(server):
    _Stub.hang_generate = True
    t0 = time.monotonic()
    r = wd.probe_generation()
    assert r.ok is False and "stalled" in r.detail
    assert time.monotonic() - t0 < 3          # bounded by the probe deadline


@pytest.mark.timing
def test_api_tags_alone_would_have_called_this_server_healthy(server):
    """The point of the module: the pre-flight the codebase already had passes."""
    _Stub.hang_generate = True
    assert direct_executor.ollama_is_alive(timeout=0.5) is True
    assert direct_executor.available_ollama_models(timeout=0.5) == {MODEL}
    assert wd.probe_generation().ok is False


def test_server_that_stops_answering_api_ps_is_detected_as_hung(server):
    _Stub.hang_ps = True
    r = wd.probe_generation()
    assert r.ok is False and "/api/ps" in r.detail


def test_cold_server_is_not_probed_and_not_hung(server):
    _Stub.ps_models = []
    _Stub.hang_generate = True                # would hang, but nothing is resident
    r = wd.probe_generation()
    assert r.ok is True and _generates() == []


def test_unreachable_server_is_unknown_not_hung(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", f"http://127.0.0.1:{port}")
    r = wd.probe_generation(timeout=0.5)
    assert r.ok is None


def test_runner_crash_reply_counts_as_a_failure(server):
    _Stub.generate_reply = {"error": "model runner has unexpectedly stopped"}
    r = wd.probe_generation()
    assert r.ok is False and "runner crashed" in r.detail


def test_empty_token_with_done_true_is_healthy(server):
    _Stub.generate_reply = {"done": True, "response": ""}
    assert wd.probe_generation().ok is True


# ── the persisted breaker ────────────────────────────────────────────────────

def test_hung_only_after_n_consecutive_failures_and_one_success_clears_it(server):
    bad = wd.ProbeResult(False, "stalled")
    assert wd.record_probe(bad) is None and not wd.read_state().get("hung")
    reason = wd.record_probe(bad)
    assert reason and reason.startswith("backend_unhealthy")
    assert wd.read_state()["hung"] is True
    assert wd.record_probe(bad) is None       # already hung: one transition, one report
    wd.record_probe(wd.ProbeResult(True, "ok"))
    st = wd.read_state()
    assert st["hung"] is False and st["streak"] == 0


def test_unknown_changes_neither_the_streak_nor_the_hung_flag(server):
    bad = wd.ProbeResult(False, "stalled")
    wd.record_probe(bad)
    wd.record_probe(wd.ProbeResult(None, "unreachable"))
    assert wd.read_state()["streak"] == 1
    wd.record_probe(bad)
    wd.record_probe(wd.ProbeResult(None, "unreachable"))
    assert wd.read_state()["hung"] is True


def test_confirmed_hang_is_recorded_as_a_failopen_and_hinted(server):
    failopen.clear()
    bad = wd.ProbeResult(False, "stalled")
    wd.record_probe(bad)
    wd.record_probe(bad)
    assert failopen.snapshot().by_code.get("OLLAMA-WATCHDOG-HUNG") == 1
    hint = wd.hung_hint()
    assert hint and "HUNG" in hint and "\n" not in hint
    wd.record_probe(wd.ProbeResult(True, "ok"))
    assert wd.hung_hint() is None


def test_corrupt_state_file_fails_open(server):
    wd._state_file().parent.mkdir(parents=True, exist_ok=True)
    wd._state_file().write_text("{not json")
    assert wd.read_state() == {}
    assert wd.gate() is None


# ── the gate: rate limiting and fail-fast ────────────────────────────────────

def test_gate_is_rate_limited(server):
    assert wd.gate(now=1000.0) is None
    assert len(_generates()) == 1
    assert wd.gate(now=1010.0) is None        # inside min_interval: no network call
    assert wd.gate(now=1059.0) is None
    assert len(_generates()) == 1
    assert wd.gate(now=1061.0) is None        # past it: probes again
    assert len(_generates()) == 2


def test_gate_fails_fast_on_a_stalled_server_and_serves_the_verdict_from_state(server):
    _Stub.hang_generate = True
    reason = wd.gate(now=2000.0)
    assert reason and reason.startswith("backend_unhealthy")
    # second failure inside the interval is answered from state, with no probe
    posts_before = len(_generates())
    wd.record_probe(wd.ProbeResult(False, "stalled"), now=2001.0)   # reaches fail_n=2
    assert wd.gate(now=2002.0) and len(_generates()) == posts_before


def test_gate_clears_the_flag_when_the_server_recovers(server):
    bad = wd.ProbeResult(False, "stalled")
    wd.record_probe(bad, now=3000.0)
    wd.record_probe(bad, now=3000.0)
    assert wd.gate(now=3010.0)                # hung, fresh: fail fast, no probe
    assert wd.gate(now=3100.0) is None        # stale + healthy: probe clears it
    assert wd.read_state()["hung"] is False


def test_gate_off_switch_does_nothing(server, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG", "off")
    _Stub.hang_generate = True
    assert wd.gate() is None and _Stub.posts == []


def test_gate_unknown_server_proceeds(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG", "on")
    assert wd.gate() is None


# ── the two local paths fail fast ────────────────────────────────────────────

def test_direct_executor_skips_a_hung_ollama_without_calling_it(server):
    _Stub.hang_generate = True
    out = direct_executor.execute_chain("what is 2+2", [ModelSpec("ollama", MODEL)], "query")
    assert out is None
    assert [p for path, p in _Stub.posts if path == "/api/chat"] == []


@pytest.mark.timing
def test_zero_claude_edit_falls_through_fast_on_a_hung_ollama(server, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "foo.py").write_text("def old_name():\n    pass\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=root, check=True)
    _Stub.hang_generate = True
    t0 = time.monotonic()
    out = zce.maybe_replace(prompt="rename old_name to new_name in foo.py", cwd=str(root),
                            deadline_s=time.monotonic() + 36)
    assert out is not None and out.action == "fallthrough" and not out.applied
    assert "unhealthy" in out.log_reason
    assert time.monotonic() - t0 < 15         # not the 37 s deadline; 15 s leaves room for a loaded runner
    assert [p for path, p in _Stub.posts if path == "/api/chat"] == []
    assert (root / "foo.py").read_text() == "def old_name():\n    pass\n"

    # and a healthy server is still served (the gate must not break the good path)
    _Stub.hang_generate = False
    wd.record_probe(wd.ProbeResult(True, "ok"))
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_MIN_INTERVAL_S", "0.0001")
    time.sleep(0.01)
    out = zce.maybe_replace(prompt="rename old_name to new_name in foo.py", cwd=str(root),
                            deadline_s=time.monotonic() + 36)
    assert out is not None and out.applied, out


# ── confirmation round and the opt-in restart ────────────────────────────────

def _confirm(**kw):
    return wd.confirm(sleep=lambda _s: None, **kw)


def test_confirm_on_a_healthy_server_probes_once_and_reports_not_hung(server):
    out = _confirm()
    assert out["probes"] == 1 and out["hung"] is False and out["restarted"] is False


def test_confirm_skips_when_a_healthy_probe_is_recent(server):
    _confirm()
    out = _confirm()
    assert out["skipped"] and out["probes"] == 0 and len(_generates()) == 1


def test_confirm_holds_a_lease_so_two_rounds_do_not_overlap(server):
    wd._write_state({"confirm_lease_ts": time.time()})
    assert "running" in _confirm()["skipped"]
    assert _Stub.posts == []


def test_confirm_detects_a_hang_but_restart_flag_off_means_no_restart(
        server, monkeypatch, no_real_process_calls):
    _Stub.hang_generate = True
    called = []
    monkeypatch.setattr(wd, "restart", lambda *a, **k: called.append(a) or True)
    monkeypatch.setattr(wd, "_process_table", lambda: called.append("ps") or [])
    out = _confirm()
    assert out["hung"] is True and out["probes"] == 2 and out["restarted"] is False
    assert called == []                       # not even a process-table look
    assert wd.hung_hint() and "RESTART=1" in wd.hung_hint()


def test_restart_on_calls_the_restarter(server, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_RESTART", "1")
    _Stub.hang_generate = True
    called = []
    monkeypatch.setattr(wd, "_process_table",
                        lambda: [(4242, "/usr/local/bin/ollama serve")])
    monkeypatch.setattr(wd, "restart", lambda mode, pids, **k: called.append((mode, pids)) or True)
    failopen.clear()
    out = _confirm()
    assert called == [("serve", [4242])] and out["restarted"] is True
    assert out["hung"] is False               # a restarted server starts clean
    assert failopen.snapshot().by_code.get("OLLAMA-WATCHDOG-RESTARTED") == 1


def test_restart_waits_a_grace_period_and_reprobes_before_acting(server, monkeypatch):
    """A long generation from another client must not get the server killed."""
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_RESTART", "1")
    _Stub.hang_generate = True
    called = []
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if s == wd.RESTART_GRACE_S:
            _Stub.hang_generate = False       # it was only busy; it answers now
    monkeypatch.setattr(wd, "_process_table", lambda: [(4242, "ollama serve")])
    monkeypatch.setattr(wd, "restart", lambda *a, **k: called.append(a) or True)
    out = wd.confirm(sleep=sleep)
    assert wd.RESTART_GRACE_S in sleeps
    assert called == [] and out["restarted"] is False and out["hung"] is False


def test_restart_respects_the_cooldown(server, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_RESTART", "1")
    _Stub.hang_generate = True
    wd._write_state({"last_restart_ts": time.time() - 60})
    called = []
    monkeypatch.setattr(wd, "_process_table", lambda: [(4242, "ollama serve")])
    monkeypatch.setattr(wd, "restart", lambda *a, **k: called.append(a) or True)
    assert _confirm()["restarted"] is False and called == []


def test_restart_with_unknown_launch_mode_does_nothing(server, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_RESTART", "1")
    _Stub.hang_generate = True
    called = []
    monkeypatch.setattr(wd, "_process_table", lambda: [(7, "/bin/zsh -l")])
    monkeypatch.setattr(wd, "restart", lambda *a, **k: called.append(a) or True)
    failopen.clear()
    assert _confirm()["restarted"] is False and called == []
    assert failopen.snapshot().by_code.get("OLLAMA-WATCHDOG-RESTART-UNKNOWN-MODE") == 1


def test_a_remote_server_is_never_restarted(monkeypatch, server):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_RESTART", "1")
    monkeypatch.setattr(wd, "_base_url", lambda: "http://ollama.example.com:11434")
    monkeypatch.setattr(wd, "_process_table", lambda: pytest.fail("looked at local processes"))
    assert wd._maybe_restart(lambda _s: None, time.time()) is False


# ── launch-mode detection and the restarter itself (all mocked) ──────────────

def test_detect_launch_mode_hand_run_serve():
    rows = [(10, "/bin/zsh"), (4242, "/opt/homebrew/bin/ollama serve"),
            (4300, "/opt/homebrew/bin/ollama runner --model x")]
    assert wd.detect_launch_mode(rows) == (wd.MODE_SERVE, [4242])


def test_detect_launch_mode_ollama_app():
    rows = [(500, "/Applications/Ollama.app/Contents/MacOS/Ollama"),
            (501, "/Applications/Ollama.app/Contents/Resources/ollama serve")]
    assert wd.detect_launch_mode(rows) == (wd.MODE_APP, [501])


def test_detect_launch_mode_hand_run_wins_over_the_app():
    rows = [(500, "/Applications/Ollama.app/Contents/MacOS/Ollama"),
            (4242, "ollama serve")]
    assert wd.detect_launch_mode(rows) == (wd.MODE_SERVE, [4242])


def test_detect_launch_mode_unknown_when_nothing_is_visible():
    assert wd.detect_launch_mode([(10, "/bin/zsh"), (11, "vim ollama-notes.md")]) == (
        wd.MODE_UNKNOWN, [])


def test_restart_sends_sigterm_never_sigkill_and_relaunches_via_the_script(server, monkeypatch):
    sent, ran = [], []
    monkeypatch.setattr(wd.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(wd, "_wait_gone", lambda *a, **k: True)
    monkeypatch.setattr(wd.subprocess, "run", lambda argv, **k: ran.append((argv, k)))
    assert wd.restart(wd.MODE_SERVE, [4242], sleep=lambda _s: None) is True
    assert sent == [(4242, wd.signal.SIGTERM)]
    assert ran and ran[0][0][0] == "bash" and ran[0][0][1].endswith("start-ollama.sh")
    assert all("env" in k for _a, k in ran)   # R4: never an inheriting subprocess


def test_restart_app_quits_via_osascript_then_reopens_it(server, monkeypatch):
    ran = []
    monkeypatch.setattr(wd.os, "kill", lambda *a: None)
    monkeypatch.setattr(wd, "_wait_gone", lambda *a, **k: True)
    monkeypatch.setattr(wd.subprocess, "run", lambda argv, **k: ran.append(argv))
    assert wd.restart(wd.MODE_APP, [501], sleep=lambda _s: None) is True
    assert ran[0][0] == "osascript" and ran[-1] == ["open", "-a", "Ollama"]


def test_restart_gives_up_without_escalating_when_sigterm_is_ignored(server, monkeypatch):
    sent, ran = [], []
    monkeypatch.setattr(wd.os, "kill", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(wd, "_wait_gone", lambda *a, **k: False)
    monkeypatch.setattr(wd.subprocess, "run", lambda argv, **k: ran.append(argv))
    assert wd.restart(wd.MODE_SERVE, [4242], sleep=lambda _s: None) is False
    assert sent == [wd.signal.SIGTERM] and ran == []   # no SIGKILL, no relaunch


def test_restart_unknown_mode_is_a_no_op(no_real_process_calls):
    assert wd.restart(wd.MODE_UNKNOWN, []) is False


# ── session start wiring ─────────────────────────────────────────────────────

def _load_session_start():
    import importlib.util
    from pathlib import Path
    path = Path(wd.__file__).resolve().parent / "hooks" / "session-start.py"
    spec = importlib.util.spec_from_file_location("session_start_wd", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_start_spawns_the_watchdog_detached_with_an_explicit_env(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG", "on")
    mod = _load_session_start()
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **k: spawned.append((argv, k)))
    mod._ollama_watchdog_bg()
    argv, kw = spawned[0]
    assert argv[1:] == ["-m", "llm_router.ollama_watchdog"]
    assert kw["start_new_session"] is True and "env" in kw


def test_session_start_does_not_spawn_when_disabled(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG", "off")
    mod = _load_session_start()
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    mod._ollama_watchdog_bg()
    assert spawned == []


def test_session_start_hint_appears_only_while_hung(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG", "on")
    mod = _load_session_start()
    assert mod._ollama_watchdog_hint() == ""
    bad = wd.ProbeResult(False, "stalled")
    for _ in range(wd.fail_n()):
        wd.record_probe(bad)
    assert "HUNG" in mod._ollama_watchdog_hint()
    wd.record_probe(wd.ProbeResult(True, "ok"))
    assert mod._ollama_watchdog_hint() == ""


def test_main_exits_zero_even_when_the_round_blows_up(monkeypatch):
    monkeypatch.setattr(wd, "confirm", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert wd.main() == 0
