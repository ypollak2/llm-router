"""F41 (owner decision 2026-10-17): the router's local model is qwen3.8-uncensored.

Before: qwen3.5 stayed resident on Ollama because the router itself loaded it --
the hook's hardcoded "qwen3.5:latest" fallbacks, the zero-Claude edit default, and
a watchdog probe that generated on whatever was resident. These tests pin the
resolver, the hook wiring, and that the watchdog gate no longer generates on the
prompt path. No real Ollama: a recording stub server stands in for it.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from llm_router import model_discovery as md
from llm_router import ollama_watchdog as wd

PREF = "qwen3.8-uncensored:latest"
OLD = "qwen3.5:latest"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for v in ("LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_BUDGET_MODELS", "OLLAMA_MODELS"):
        monkeypatch.delenv(v, raising=False)


# ── resolver ─────────────────────────────────────────────────────────────────

def test_both_installed_picks_qwen38():
    assert md.preferred_local_model([OLD, PREF]) == PREF
    assert md.preferred_local_model([PREF, OLD]) == PREF


def test_env_setting_beats_the_preference(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "chosen:7b")
    assert md.preferred_local_model([OLD, PREF]) == "chosen:7b"


def test_without_qwen38_the_current_discovery_order_applies():
    assert md.preferred_local_model([OLD, "gemma4:latest"]) == OLD
    assert md.preferred_local_model(["gemma4:latest", OLD], prefer=("qwen3.5",)) == OLD


def test_nothing_known_names_qwen38_never_qwen35():
    assert md.preferred_local_model([]) == PREF


def test_prefer_first_reorders_without_adding():
    assert md.prefer_first([OLD, PREF, "x:1"]) == [PREF, OLD, "x:1"]
    assert md.prefer_first([OLD, "x:1"]) == [OLD, "x:1"]


def _load_hook(path: Path):
    spec = importlib.util.spec_from_file_location("auto_route_f41", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_defaults_follow_the_resolver(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", f"{OLD},{PREF}")
    hook = _load_hook(ROOT / "src/llm_router/hooks/auto-route.py")
    assert hook.OLLAMA_MODEL == PREF
    assert hook.OLLAMA_MODELS[0] == PREF and hook.OLLAMA_CODE_MODELS[0] == PREF


def test_hook_with_nothing_discovered_does_not_fall_back_to_qwen35(monkeypatch):
    monkeypatch.setattr(md, "available_ollama_models_nowait", lambda **_k: [])
    hook = _load_hook(ROOT / "src/llm_router/hooks/auto-route.py")
    assert hook.OLLAMA_MODEL == PREF
    assert hook.OLLAMA_MODELS == [PREF] and hook.OLLAMA_CODE_MODELS == [PREF]


def test_zero_claude_edit_model_prefers_qwen38(monkeypatch):
    from llm_router import zero_claude_edit as zce
    monkeypatch.setattr(md, "available_ollama_models", lambda **_k: [OLD, PREF])
    assert zce.edit_model() == PREF


def test_the_two_hook_copies_are_byte_identical():
    assert (ROOT / "hooks/auto-route.py").read_bytes() == (
        ROOT / "src/llm_router/hooks/auto-route.py").read_bytes()


# ── watchdog: nothing generates on the prompt path ───────────────────────────

class _Rec(BaseHTTPRequestHandler):
    paths: list = []
    gen_delay = 0.0

    def log_message(self, *_a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).paths.append(self.path)
        self._send({"models": [{"name": PREF, "model": PREF}], "version": "0"})

    def do_POST(self):
        cls = type(self)
        cls.paths.append(self.path)
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        time.sleep(cls.gen_delay)
        self._send({"done": True, "response": "x"})


@pytest.fixture
def rec_server(monkeypatch):
    _Rec.paths = []
    _Rec.gen_delay = 2.0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Rec)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://%s:%s" % srv.server_address)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG", "on")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", PREF)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_MIN_INTERVAL_S", "60")
    spawned: list = []
    monkeypatch.setattr(wd, "_spawn_background_probe", lambda: spawned.append(1) or True)
    try:
        yield spawned
    finally:
        srv.shutdown()
        srv.server_close()


def test_gate_issues_no_generate_or_chat_and_is_fast_with_a_2s_generate(rec_server):
    t0 = time.monotonic()
    assert wd.gate(now=1000.0) is None
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, f"gate took {elapsed * 1000:.0f} ms on the prompt path"
    assert not [p for p in _Rec.paths if p in ("/api/generate", "/api/chat")], _Rec.paths
    assert rec_server == [1], "a stale state must start the background probe"


def test_background_probe_targets_only_the_preferred_resident_model(rec_server):
    _Rec.gen_delay = 0.0
    r = wd.probe_generation()
    assert r.ok is True and r.model == PREF
    assert [p for p in _Rec.paths if p == "/api/generate"] == ["/api/generate"]


def test_background_probe_does_not_generate_on_a_non_preferred_resident(rec_server, monkeypatch):
    _Rec.gen_delay = 0.0
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", OLD)     # preferred = qwen3.5, resident = qwen3.8
    r = wd.probe_generation()
    assert r.ok is None and r.model is None
    assert "/api/generate" not in _Rec.paths


# ── the goal, end to end: with both installed, every default path picks qwen3.8 ──

def test_with_both_installed_every_default_path_resolves_to_qwen38(monkeypatch):
    import subprocess

    import yaml

    both = [OLD, PREF]
    monkeypatch.setenv("OLLAMA_MODELS", ",".join(both))
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_WARMUP_MODEL", raising=False)
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_WARMUP", raising=False)
    monkeypatch.setattr(md, "available_ollama_models", lambda *a, **k: list(both))
    monkeypatch.setattr(md, "available_ollama_models_nowait", lambda **_k: list(both))

    # 1. session-start warm-up (the packaged copy; the other is byte-identical)
    posted: list[str] = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: posted.append(cmd[-1]))
    ss = _load_hook(ROOT / "src/llm_router/hooks/session-start.py")
    monkeypatch.setattr(ss, "_EDIT_WARMED_MODEL", False, raising=False)
    ss._warm_ollama_bg()
    assert posted and json.loads(posted[0])["model"] == PREF

    # 2. chain_builder local order
    from llm_router.hooks import chain_builder
    monkeypatch.setattr(chain_builder, "_demote_unreliable", lambda ms: ms)
    front, _tail = chain_builder._ollama_models("code")
    assert front[0].model == PREF

    # 3. auto-route default
    ar = _load_hook(ROOT / "src/llm_router/hooks/auto-route.py")
    assert ar.OLLAMA_MODEL == PREF

    # 4. standard policy: the first local entry
    text = (ROOT / "src/llm_router/policies/standard.yaml").read_text()
    local = [ln.split("ollama/", 1)[1].strip() for ln in text.splitlines()
             if ln.strip().startswith("- ollama/")]
    assert local and local[0] == PREF and OLD not in local
    yaml.safe_load(text)
