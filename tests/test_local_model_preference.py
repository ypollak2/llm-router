"""Local model comes from the USER's configuration, never from code (owner rule 2026-10-17).

llm-router is open source and dynamic: installed models, subscriptions and keys are the
user's own configuration, discovered at runtime. Resolution order, everywhere:

    1. LLM_ROUTER_OLLAMA_MODEL (the user's setting), when installed
    2. the discovered inventory, in discovery order, no family preference
    3. None -> callers skip local

These tests use deliberately unrelated, fake model names so a family preference or a
literal fallback anywhere cannot pass by luck. No real Ollama: a recording stub server
stands in for it.
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

MODEL = "fake-alpha:1b"
OTHER = "fake-beta:2b"
ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src/llm_router/hooks/auto-route.py"
SESSION = ROOT / "src/llm_router/hooks/session-start.py"

FAKE_INVENTORIES = {
    "only_gemma": ["gemma4:latest"],
    "only_llama": ["llama3.2:3b"],
    "qwen35_and_qwen38": ["qwen3.5:latest", "qwen3.8-uncensored:latest"],
    "qwen38_then_qwen35": ["qwen3.8-uncensored:latest", "qwen3.5:latest"],
    "mistral": ["mistral:7b", "phi3:mini"],
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_NO_DETACHED_SPAWN", "1")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")      # hermetic: never the machine's Ollama
    for v in ("LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_BUDGET_MODELS", "OLLAMA_MODELS",
              "LLM_ROUTER_OLLAMA_WARMUP_MODEL", "LLM_ROUTER_OLLAMA_WARMUP"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(md, "last_skip_reason", None)


def _load_hook(path: Path, name="hook_under_test"):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_inventory(monkeypatch, models):
    """The user's installed models, as every discovery entry point reports them."""
    monkeypatch.setenv("OLLAMA_MODELS", ",".join(models))
    monkeypatch.setattr(md, "available_ollama_models", lambda *a, **k: list(models))
    monkeypatch.setattr(md, "available_ollama_models_nowait", lambda **_k: list(models))


# ── (i) resolver ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("inv", FAKE_INVENTORIES.values(), ids=FAKE_INVENTORIES.keys())
def test_resolver_returns_the_first_discovered_model_whatever_the_family(inv):
    assert md.local_model(inv) == inv[0]


@pytest.mark.parametrize("inv", FAKE_INVENTORIES.values(), ids=FAKE_INVENTORIES.keys())
def test_user_setting_wins_over_discovery_order(inv, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", inv[-1])
    assert md.local_model(inv) == inv[-1]


def test_setting_matches_name_with_or_without_latest(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "fake-gamma")
    assert md.local_model([OTHER, "fake-gamma:latest"]) == "fake-gamma:latest"


def test_setting_not_installed_is_skipped_and_the_reason_recorded(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "fake-missing:9b")
    assert md.local_model([MODEL, OTHER]) == MODEL
    assert "fake-missing:9b" in (md.last_skip_reason or "") and "not installed" in md.last_skip_reason


def test_inventory_read_from_the_user_state_cache(tmp_path):
    (tmp_path / "discovery.json").write_text(json.dumps(
        {"cached_at": time.time(), "models": {f"ollama/{OTHER}": {}, f"ollama/{MODEL}": {}}}))
    assert md.local_model(allow_probe=False) == OTHER


def test_empty_inventory_is_none(monkeypatch):
    assert md.local_model([]) is None
    assert md.local_model(allow_probe=False) is None          # no cache, no env, no probe


def test_setting_is_used_as_given_when_the_inventory_is_unknown(monkeypatch):
    """Empty inventory (Ollama down, no cache yet) cannot show the setting is missing."""
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", MODEL)
    assert md.local_model([]) == MODEL and md.last_skip_reason is None


def test_prefer_first_is_generic_it_only_moves_the_users_setting():
    assert md.prefer_first([OTHER, MODEL, "x:1"], MODEL) == [MODEL, OTHER, "x:1"]
    assert md.prefer_first([OTHER, MODEL]) == [OTHER, MODEL]                  # no setting: unchanged
    assert md.prefer_first([OTHER, "x:1"], MODEL) == [OTHER, "x:1"]           # never adds


# ── (ii) nothing discovered: every caller skips local ────────────────────────

def test_hook_with_nothing_discovered_has_no_local_model(monkeypatch):
    monkeypatch.setattr(md, "available_ollama_models_nowait", lambda **_k: [])
    hook = _load_hook(HOOK)
    assert hook.OLLAMA_MODEL is None
    assert hook.OLLAMA_MODELS == [] and hook.OLLAMA_CODE_MODELS == []


def test_hook_select_model_skips_local_when_none(monkeypatch):
    from llm_router import profiles
    from llm_router.types import RoutingProfile, TaskType
    monkeypatch.setattr(md, "available_ollama_models_nowait", lambda **_k: [])
    hook = _load_hook(HOOK)
    key = (RoutingProfile.BUDGET, TaskType.QUERY)
    monkeypatch.setitem(profiles.ROUTING_TABLE, key, [f"ollama/{MODEL}", "anthropic/claude-sonnet-4-6"])
    assert hook._get_selected_model("query", "simple") == ("anthropic/claude-sonnet-4-6", "anthropic")
    monkeypatch.setitem(profiles.ROUTING_TABLE, key, [f"ollama/{MODEL}"])
    assert hook._get_selected_model("query", "simple") == ("unknown", "unknown")


def test_session_start_warmup_does_nothing_without_a_local_model(monkeypatch):
    import subprocess
    monkeypatch.setattr(md, "available_ollama_models", lambda *a, **k: [])
    monkeypatch.setattr(md, "available_ollama_models_nowait", lambda **_k: [])
    monkeypatch.setattr(md, "_inventory", lambda **_k: [])
    posted: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: posted.append(cmd))
    ss = _load_hook(SESSION, "ss_empty")
    monkeypatch.setattr(ss, "_EDIT_WARMED_MODEL", False, raising=False)
    ss._warm_ollama_bg()
    assert posted == []


def test_zero_claude_edit_and_its_warmup_skip_local_without_a_model(monkeypatch):
    import subprocess
    from llm_router import warm
    from llm_router import zero_claude_edit as zce
    monkeypatch.setattr(md, "_inventory", lambda **_k: [])
    assert zce.edit_model() is None
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    popen: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popen.append(a))
    assert warm.warm_edit_model_bg() is None and popen == []


def test_chain_builder_has_no_local_entries_without_a_model(monkeypatch):
    from llm_router.hooks import chain_builder
    monkeypatch.setattr(md, "available_ollama_models", lambda *a, **k: [])
    front, tail = chain_builder._ollama_models("code")
    assert front == [] and tail == []


def test_watchdog_probes_nothing_without_a_local_model(monkeypatch):
    monkeypatch.setattr(md, "_inventory", lambda **_k: [])
    assert wd._pick_model([MODEL, OTHER]) is None


# ── (iii) policy placeholder ─────────────────────────────────────────────────

def test_policy_placeholder_resolves_to_the_discovered_model_in_place(monkeypatch):
    from llm_router.policy import _resolve_chain
    monkeypatch.setenv("OLLAMA_MODELS", f"{OTHER},{MODEL}")
    assert _resolve_chain(["ollama/@local", "anthropic/claude-sonnet-4-6"])[:1] == [f"ollama/{OTHER}"]


def test_policy_placeholder_follows_the_user_setting(monkeypatch):
    from llm_router.policy import _resolve_chain
    monkeypatch.setenv("OLLAMA_MODELS", f"{OTHER},{MODEL}")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", MODEL)
    assert _resolve_chain([md.LOCAL_PLACEHOLDER])[0] == f"ollama/{MODEL}"


def test_policy_placeholder_is_dropped_when_nothing_is_discovered():
    from llm_router.policy import _resolve_chain
    out = _resolve_chain(["ollama/@local", "anthropic/claude-sonnet-4-6"])
    assert out == ["anthropic/claude-sonnet-4-6"]


@pytest.mark.parametrize("name", ["standard", "cost_aggressive", "routerarena_tuned"])
def test_shipped_policies_use_the_placeholder_not_a_named_local_model(name, monkeypatch):
    text = (ROOT / f"src/llm_router/policies/{name}.yaml").read_text()
    assert "ollama/@local" in text
    assert not [ln for ln in text.splitlines() if ln.strip().startswith("- ollama/qwen")]
    from llm_router.policy import PolicyManager
    monkeypatch.setenv("OLLAMA_MODELS", MODEL)
    pol = PolicyManager().load_policy(name)
    flat = [m for tasks in pol.chains.values() for chain in tasks.values() for m in chain]
    assert f"ollama/{MODEL}" in flat and not any("@local" in m for m in flat)
    monkeypatch.delenv("OLLAMA_MODELS")
    pol = PolicyManager().load_policy(name)
    flat = [m for tasks in pol.chains.values() for chain in tasks.values() for m in chain]
    assert not any("@local" in m or m == f"ollama/{MODEL}" for m in flat)


# ── hooks follow the resolver ────────────────────────────────────────────────

@pytest.mark.parametrize("inv", FAKE_INVENTORIES.values(), ids=FAKE_INVENTORIES.keys())
def test_hook_defaults_follow_discovery_order(inv, monkeypatch):
    _fake_inventory(monkeypatch, inv)
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    hook = _load_hook(HOOK)
    assert hook.OLLAMA_MODEL == inv[0]
    assert hook.OLLAMA_MODELS == inv and hook.OLLAMA_CODE_MODELS == inv


def test_the_two_hook_copies_are_byte_identical():
    for name in ("auto-route.py", "session-start.py"):
        assert (ROOT / "hooks" / name).read_bytes() == (
            ROOT / "src/llm_router/hooks" / name).read_bytes(), name


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
        self._send({"models": [{"name": MODEL, "model": MODEL}], "version": "0"})

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
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", MODEL)
    monkeypatch.setenv("OLLAMA_MODELS", MODEL)          # the user's inventory (env source)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WATCHDOG_MIN_INTERVAL_S", "60")
    spawned: list = []
    monkeypatch.setattr(wd, "_spawn_background_probe", lambda: spawned.append(1) or True)
    try:
        yield spawned
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.timing
def test_gate_issues_no_generate_or_chat_and_is_fast_with_a_2s_generate(rec_server):
    t0 = time.monotonic()
    assert wd.gate(now=1000.0) is None
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, f"gate took {elapsed * 1000:.0f} ms on the prompt path"
    assert not [p for p in _Rec.paths if p in ("/api/generate", "/api/chat")], _Rec.paths
    assert rec_server == [1], "a stale state must start the background probe"


def test_background_probe_targets_only_the_users_resident_model(rec_server):
    _Rec.gen_delay = 0.0
    r = wd.probe_generation()
    assert r.ok is True and r.model == MODEL
    assert [p for p in _Rec.paths if p == "/api/generate"] == ["/api/generate"]


def test_background_probe_does_not_generate_on_a_non_selected_resident(rec_server, monkeypatch):
    _Rec.gen_delay = 0.0
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", OTHER)   # the user's model, installed but not resident
    monkeypatch.setenv("OLLAMA_MODELS", f"{OTHER},{MODEL}")
    r = wd.probe_generation()
    assert r.ok is None and r.model is None
    assert "/api/generate" not in _Rec.paths


# ── (iv) the user setting wins on every default path ─────────────────────────

def test_user_setting_wins_on_every_default_path(monkeypatch, tmp_path):
    import subprocess

    from llm_router import warm  # noqa: F401
    from llm_router import zero_claude_edit as zce
    from llm_router.policy import _resolve_chain

    inv = [OTHER, MODEL, "fake-gamma:3b"]
    (tmp_path / "discovery.json").write_text(json.dumps(
        {"cached_at": time.time(), "models": {f"ollama/{m}": {} for m in inv}}))
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", MODEL)
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")

    # session-start warm-up
    posted: list[str] = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: posted.append(cmd[-1]))
    ss = _load_hook(SESSION, "ss_setting")
    monkeypatch.setattr(ss, "_EDIT_WARMED_MODEL", False, raising=False)
    ss._warm_ollama_bg()
    assert posted and json.loads(posted[0])["model"] == MODEL
    # chain_builder
    from llm_router.hooks import chain_builder
    monkeypatch.setattr(chain_builder, "_demote_unreliable", lambda ms: ms)
    assert chain_builder._ollama_models("code")[0][0].model == MODEL
    # auto-route
    ar = _load_hook(HOOK, "ar_setting")
    assert ar.OLLAMA_MODEL == MODEL and ar.OLLAMA_MODELS[0] == MODEL
    # zero-claude edit
    assert zce.edit_model() == MODEL
    # policy placeholder
    assert _resolve_chain([md.LOCAL_PLACEHOLDER]) == [f"ollama/{MODEL}"]
    # watchdog
    assert wd._pick_model(inv) == MODEL


# ── (v) the codebase names no model as the default ───────────────────────────

def test_no_owner_model_literal_in_src_or_hooks():
    hits = []
    for base in (ROOT / "src", ROOT / "hooks"):
        for f in base.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".yaml", ".yml", ".json", ".sh", ".md", ".txt", ".toml"):
                if "qwen3.8-uncensored" in f.read_text(errors="ignore"):
                    hits.append(str(f.relative_to(ROOT)))
    assert hits == [], f"the owner's model belongs in their local config, not code: {hits}"
