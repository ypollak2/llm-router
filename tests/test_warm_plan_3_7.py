"""Plan 3.7 (warm.py): keep the zero-Claude edit model warm, fail fast when cold.

Every test here drives behaviour against a stub Ollama over real HTTP (or a real
detached curl against it) and asserts on what the server RECEIVED or what the
caller RETURNED. None reads source text.

What each group pins:
  * keep_alive / num_ctx consistency  -> the edit call, the warm-up and the retry
    path all send the same options, so Ollama does not reload between them.
  * ollama_ps / should_skip_cold      -> the fail-fast decision, including the
    "unknown stays unknown" cases that must NOT skip.
  * maybe_replace                     -> cold + tight deadline falls through with
    NO /api/chat call and starts the warm-up; resident model is served.
  * session start                     -> warm-up spawn, no double-warm, contention hint.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_router import warm  # noqa: E402
from llm_router import zero_claude_edit as zce  # noqa: E402
from llm_router.hooks import direct_executor  # noqa: E402

MODEL = "warm-test-model:latest"

_EDIT_RESPONSE = json.dumps([{
    "file": "foo.py",
    "old_string": "def old_name():\n    pass\n",
    "new_string": "def new_name():\n    pass\n",
    "description": "rename old_name to new_name",
}])


class _Stub(BaseHTTPRequestHandler):
    """Records every POST; /api/ps answers from class state."""
    ps_body: object = {"models": []}
    chat_status = 200
    posts: list[tuple[str, dict]] = []

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
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": MODEL}]})
        elif self.path.startswith("/api/ps"):
            self._send(type(self).ps_body)
        else:
            self._send({"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        type(self).posts.append((self.path, payload))
        if self.path == "/api/chat":
            # First request with a `format` is refused (old Ollama), to exercise the retry.
            if type(self).chat_status != 200:
                self._send({"error": "bad"}, type(self).chat_status)
                return
            self._send({"model": payload.get("model"), "done": True,
                        "prompt_eval_count": 5, "eval_count": 5,
                        "message": {"role": "assistant", "content": _EDIT_RESPONSE}})
        else:
            self._send({"done": True})


def _resident(*names):
    return {"models": [{"name": n, "model": n} for n in names]}


@pytest.fixture
def stub(monkeypatch, tmp_path):
    _Stub.ps_body = _resident()
    _Stub.chat_status = 200
    _Stub.posts = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    url = f"http://{host}:{port}"
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", url)
    monkeypatch.setenv("OLLAMA_BASE_URL", url)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", MODEL)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    for var in ("LLM_ROUTER_LOCAL_KEEP_ALIVE", "LLM_ROUTER_ZCE_COLD_BUDGET_S",
                "LLM_ROUTER_ZCE_WARMUP", "LLM_ROUTER_AGENT_NUM_CTX", "LLM_ROUTER_LOCAL_NUM_CTX"):
        monkeypatch.delenv(var, raising=False)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def _chat_posts():
    return [p for path, p in _Stub.posts if path == "/api/chat"]


def _generate_posts():
    return [p for path, p in _Stub.posts if path == "/api/generate"]


# ── keep_alive / num_ctx consistency ─────────────────────────────────────────


def test_edit_keep_alive_default_env_and_duration_string(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_LOCAL_KEEP_ALIVE", raising=False)
    assert warm.edit_keep_alive() == -1
    monkeypatch.setenv("LLM_ROUTER_LOCAL_KEEP_ALIVE", "600")
    assert warm.edit_keep_alive() == 600
    monkeypatch.setenv("LLM_ROUTER_LOCAL_KEEP_ALIVE", "30m")
    assert warm.edit_keep_alive() == "30m"
    monkeypatch.setenv("LLM_ROUTER_LOCAL_KEEP_ALIVE", "  ")
    assert warm.edit_keep_alive() == -1


def test_call_ollama_sends_keep_alive_only_when_asked(stub):
    direct_executor.call_ollama("hi", MODEL, 5)
    direct_executor.call_ollama("hi", MODEL, 5, keep_alive=-1)
    plain, kept = _chat_posts()
    assert "keep_alive" not in plain          # other callers' behaviour is unchanged
    assert kept["keep_alive"] == -1


def test_format_rejection_retry_keeps_keep_alive(stub):
    """The retry without `format` must not silently drop keep_alive."""
    _Stub.chat_status = 400
    direct_executor.call_ollama("hi", MODEL, 5, format={"type": "object"}, keep_alive=-1)
    posts = _chat_posts()
    assert len(posts) == 2, posts
    assert "format" in posts[0] and "format" not in posts[1]
    assert posts[0]["keep_alive"] == -1 and posts[1]["keep_alive"] == -1


def test_warmup_and_edit_call_agree_on_num_ctx_and_keep_alive(stub, monkeypatch):
    """The whole point: a warm-up that differs from the real call in num_ctx makes
    Ollama reload the model on the first edit."""
    for ctx in (None, "8192"):
        if ctx:
            monkeypatch.setenv("LLM_ROUTER_AGENT_NUM_CTX", ctx)
        _Stub.posts = []
        zce.generate_edits("rename old_name to new_name", {"foo.py": "def old_name():\n    pass\n"},
                           MODEL, time.monotonic() + 30)
        chat = _chat_posts()[0]
        wp = warm.warmup_payload(MODEL)
        assert chat["keep_alive"] == wp["keep_alive"] == -1
        assert chat["options"].get("num_ctx") == wp["options"].get("num_ctx")
        if ctx:
            assert wp["options"]["num_ctx"] == int(ctx)


def test_warmup_payload_is_a_single_token_load(stub):
    wp = warm.warmup_payload(MODEL)
    assert wp["model"] == MODEL and wp["stream"] is False
    assert wp["options"]["num_predict"] == 1


# ── /api/ps and the fail-fast decision ───────────────────────────────────────


def test_ollama_ps_lists_resident_models(stub):
    _Stub.ps_body = _resident("a:latest", "b:7b")
    assert warm.ollama_ps() == ["a:latest", "b:7b"]
    _Stub.ps_body = _resident()
    assert warm.ollama_ps() == []              # empty is "cold", distinct from unknown


def test_ollama_ps_is_none_when_unknown(stub, monkeypatch):
    _Stub.ps_body = {"ok": True}               # no `models` list: not an Ollama answer
    assert warm.ollama_ps() is None
    _Stub.ps_body = {"models": "nope"}
    assert warm.ollama_ps() is None
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://127.0.0.1:1")   # nothing listens
    assert warm.ollama_ps() is None


@pytest.mark.parametrize("wanted,resident,expected", [
    ("qwen3.5", "qwen3.5:latest", True),
    ("qwen3.5:latest", "qwen3.5", True),
    ("qwen3.5:latest", "qwen3.5:latest", True),
    ("qwen3.5:7b", "qwen3.5:latest", False),
    ("qwen3.5:latest", "qwen3-coder:30b", False),
])
def test_model_matches(wanted, resident, expected):
    assert warm.model_matches(wanted, resident) is expected


def test_should_skip_cold_cold_model_tight_deadline(stub):
    reason = warm.should_skip_cold(MODEL, time.monotonic() + 36)
    assert reason and MODEL in reason


def test_should_skip_cold_does_not_skip_when_resident(stub):
    _Stub.ps_body = _resident(MODEL)
    assert warm.should_skip_cold(MODEL, time.monotonic() + 5) is None


def test_should_skip_cold_matches_untagged_name(stub):
    _Stub.ps_body = _resident("warm-test-model:latest")
    assert warm.should_skip_cold("warm-test-model", time.monotonic() + 5) is None


def test_should_skip_cold_generous_deadline_gets_a_real_attempt(stub):
    assert warm.should_skip_cold(MODEL, time.monotonic() + 120) is None


def test_should_skip_cold_unknown_state_proceeds(stub):
    _Stub.ps_body = {"ok": True}
    assert warm.should_skip_cold(MODEL, time.monotonic() + 1) is None


def test_cold_budget_is_operator_tunable(stub, monkeypatch):
    deadline = time.monotonic() + 36
    assert warm.should_skip_cold(MODEL, deadline)                      # default 45 s: skip
    monkeypatch.setenv("LLM_ROUTER_ZCE_COLD_BUDGET_S", "10")
    assert warm.should_skip_cold(MODEL, deadline) is None              # 10 s: attempt
    monkeypatch.setenv("LLM_ROUTER_ZCE_COLD_BUDGET_S", "garbage")
    assert warm.should_skip_cold(MODEL, deadline)                      # bad value -> default


# ── contention warning ───────────────────────────────────────────────────────


def test_dedicated_server_warning_only_for_multiple_resident_models(stub):
    assert warm.dedicated_server_warning(["a"]) is None
    assert warm.dedicated_server_warning([]) is None
    text = warm.dedicated_server_warning(["a:latest", "b:7b"])
    assert text and "a:latest" in text and "b:7b" in text
    _Stub.ps_body = _resident("a", "b", "c")
    assert "3 models" in warm.dedicated_server_warning()
    _Stub.ps_body = {"ok": True}
    assert warm.dedicated_server_warning() is None                     # unknown: stay quiet


# ── warm-up spawn (real detached curl against the stub) ─────────────────────


def _wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")
def test_warm_edit_model_bg_loads_with_the_edit_calls_options(stub, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    assert warm.warm_edit_model_bg() == MODEL
    assert _wait_for(lambda: _generate_posts()), "warm-up never reached the server"
    got = _generate_posts()[0]
    assert got == warm.warmup_payload(MODEL)
    assert got["keep_alive"] == -1


def test_warm_edit_model_bg_is_gated(stub, monkeypatch):
    spawned = []
    monkeypatch.setattr(warm.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.delenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", raising=False)
    assert warm.warm_edit_model_bg() is None                           # scope off
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    monkeypatch.setenv("LLM_ROUTER_ZCE_WARMUP", "off")
    assert warm.warm_edit_model_bg() is None                           # opted out
    assert spawned == []


def test_warm_edit_model_bg_swallows_spawn_failure(stub, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")

    def boom(*_a, **_k):
        raise OSError("no curl")
    monkeypatch.setattr(warm.subprocess, "Popen", boom)
    assert warm.warm_edit_model_bg() is None


# ── maybe_replace end to end (in-process, stub Ollama) ──────────────────────


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "foo.py").write_text("def old_name():\n    pass\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def test_cold_model_falls_through_fast_without_calling_the_model(stub, repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    spawned = []

    real_popen = warm.subprocess.Popen

    def fake_popen(argv, **k):
        if argv and argv[0] == "curl":
            spawned.append(argv)
            return type("FakeProc", (), {"pid": 1})()
        return real_popen(argv, **k)
    monkeypatch.setattr(warm.subprocess, "Popen", fake_popen)
    t0 = time.monotonic()
    out = zce.maybe_replace(prompt="rename old_name to new_name in foo.py", cwd=str(repo),
                            deadline_s=time.monotonic() + 36)
    assert out is not None and out.action == "fallthrough" and not out.applied
    assert "cold" in out.log_reason
    assert time.monotonic() - t0 < 5          # fail fast, not the 37 s deadline
    assert _chat_posts() == []                # the model was never asked
    assert (repo / "foo.py").read_text() == "def old_name():\n    pass\n"
    # ...and the next edit is made warm: a detached warm-up was started.
    assert len(spawned) == 1 and spawned[0][0] == "curl"
    assert json.loads(spawned[0][spawned[0].index("-d") + 1])["keep_alive"] == -1


def test_resident_model_is_served_with_keep_alive(stub, repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    _Stub.ps_body = _resident(MODEL)
    out = zce.maybe_replace(prompt="rename old_name to new_name in foo.py", cwd=str(repo),
                            deadline_s=time.monotonic() + 36)
    assert out is not None and out.applied, out
    assert "new_name" in (repo / "foo.py").read_text()
    assert _chat_posts() and all(p["keep_alive"] == -1 for p in _chat_posts())


def test_unknown_ps_state_behaves_as_before(stub, repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    _Stub.ps_body = {"ok": True}              # e.g. a proxy that has no /api/ps
    out = zce.maybe_replace(prompt="rename old_name to new_name in foo.py", cwd=str(repo),
                            deadline_s=time.monotonic() + 36)
    assert out is not None and out.applied, out


def test_cold_model_with_a_generous_deadline_still_attempts(stub, repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    out = zce.maybe_replace(prompt="rename old_name to new_name in foo.py", cwd=str(repo),
                            deadline_s=time.monotonic() + 120)
    assert out is not None and out.applied, out


# ── session start ────────────────────────────────────────────────────────────


def _load_session_start(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src/llm_router/hooks/session-start.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_start_warms_edit_model_and_skips_the_generic_double_warm(stub, monkeypatch):
    mod = _load_session_start("_ss_warm_1")
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WARMUP_MODEL", MODEL)
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_WARMUP", raising=False)
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **k: spawned.append(argv))

    mod._warm_edit_model_bg()
    mod._warm_ollama_bg()                      # same model: must NOT reload it at server defaults
    assert len(spawned) == 1
    body = json.loads(spawned[0][spawned[0].index("-d") + 1])
    assert body["model"] == MODEL and body["keep_alive"] == -1 and "num_ctx" in body["options"]


def test_session_start_generic_warmup_still_runs_for_a_different_model(stub, monkeypatch):
    mod = _load_session_start("_ss_warm_2")
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WARMUP_MODEL", "other-model:latest")
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_WARMUP", raising=False)
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    mod._warm_edit_model_bg()
    mod._warm_ollama_bg()
    models = [json.loads(a[a.index("-d") + 1])["model"] for a in spawned]
    assert models == [MODEL, "other-model:latest"]


def test_session_start_scope_off_warms_nothing_extra(stub, monkeypatch):
    mod = _load_session_start("_ss_warm_3")
    monkeypatch.delenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", raising=False)
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    assert mod._warm_edit_model_bg() is None
    assert spawned == []
    assert mod._ollama_contention_hint() == ""


def test_session_start_contention_hint(stub, monkeypatch):
    mod = _load_session_start("_ss_warm_4")
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    _Stub.ps_body = _resident("a:latest")
    assert mod._ollama_contention_hint() == ""
    _Stub.ps_body = _resident("a:latest", "b:7b")
    hint = mod._ollama_contention_hint()
    assert hint.startswith("\n") and "a:latest" in hint and "b:7b" in hint
    _Stub.ps_body = {"ok": True}
    assert mod._ollama_contention_hint() == ""
