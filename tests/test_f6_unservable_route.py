"""F6 (D-141 C): the hooks never direct, or hold tools, for a task no provider serves.

Live 2026-10-16: providers {openrouter, ollama, xai}, no research provider, yet
auto-route emitted `llm(task="research")` and enforce-route held Bash until a dummy
llm(task="query") released it. These tests run both real hooks as subprocesses
with a scratch HOME whose resource map says what this machine can serve.
"""

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_router import task_availability as ta

ROOT = Path(__file__).resolve().parents[1]
AUTO = ROOT / "src/llm_router/hooks/auto-route.py"
ENFORCE = ROOT / "src/llm_router/hooks/enforce-route.py"
PROMPT = "Research the latest published benchmarks comparing vector databases and cite your sources."
SID = "f6"

CLAUDE_LOGIN = {"resource": "claude", "kind": "subscription",
                "chain_id": "anthropic/claude-subscription", "status": "connected"}
OPENROUTER = {"resource": "api/OPENROUTER_API_KEY", "kind": "api",
              "chain_id": "openrouter/api", "status": "connected"}
OLLAMA = {"resource": "qwen2.5:7b", "kind": "local",
          "chain_id": "ollama/qwen2.5:7b", "status": "connected"}
ANTHROPIC_KEY = {"resource": "api/ANTHROPIC_API_KEY", "kind": "api",
                 "chain_id": "anthropic/api", "status": "connected"}


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_map(home: Path, rows, *, age_h: float = 0.0) -> None:
    state = home / ".llm-router"
    state.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()
    for i, row in enumerate(rows):
        row.setdefault("capabilities", ["generate"])
        row.setdefault("effective_priority", i + 1)
    (state / "resource_map.json").write_text(json.dumps(
        {"schema": 1, "generated_at": stamp, "policy": "balanced", "resources": rows}))


def _env(home: Path, **overrides) -> dict:
    state = home / ".llm-router"
    state.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    url = f"http://127.0.0.1:{_closed_port()}"
    env.update({
        "HOME": str(home), "LLM_ROUTER_HOME": str(state),
        "LLM_ROUTER_DB_PATH": str(state / "test.db"),
        "LLM_ROUTER_OLLAMA_URL": url, "OLLAMA_HOST": url,
        "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
        "LLM_ROUTER_MINI_SUMMARY_EVERY": "0",
        "LLM_ROUTER_NO_DETACHED_SPAWN": "1",
        "LLM_ROUTER_MAP_REDISCOVERY": "0",   # keep the map the test wrote
        "LLM_ROUTER_ENFORCE": "smart",
        "LLM_ROUTER_QA_ROUTING": "on",       # Q&A directives on, as on the live machine
    })
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def route(home: Path, prompt: str = PROMPT, **overrides):
    result = subprocess.run(
        [sys.executable, str(AUTO)], cwd=home, env=_env(home, **overrides),
        input=json.dumps({"hook_event_name": "UserPromptSubmit",
                          "prompt": prompt, "session_id": SID}),
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    return json.dumps(json.loads(result.stdout), ensure_ascii=False) if result.stdout.strip() else ""


def bash(home: Path, **overrides) -> str:
    """enforce-route's verdict on a write Bash call: '' when allowed."""
    result = subprocess.run(
        [sys.executable, str(ENFORCE)], cwd=home, env=_env(home, **overrides),
        input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "tool_input": {"command": "touch out.txt"}, "session_id": SID}),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def pending(home: Path):
    files = list((home / ".llm-router").rglob(f"pending_route_{SID}*.json"))
    return json.loads(files[0].read_text()) if files else None


def debug_log(home: Path) -> str:
    path = home / ".llm-router" / "auto-route-debug.log"
    return path.read_text() if path.is_file() else ""


def held(verdict: str) -> bool:
    return '"block"' in verdict or '"deny"' in verdict


# ── no provider can serve research or any fallback: no directive, no hold ──────

def test_no_provider_no_directive_and_bash_not_held(tmp_path):
    # A claude.ai login alone does not put anthropic in the router's chain.
    write_map(tmp_path, [CLAUDE_LOGIN, OPENROUTER])
    out = route(tmp_path)
    assert 'llm(task=\\"research\\")' not in out and "ROUTE" not in out
    assert "no-provider" in out
    assert pending(tmp_path) is None
    assert not held(bash(tmp_path))
    log = [line for line in debug_log(tmp_path).splitlines() if "F6 UNSERVABLE" in line]
    assert len(log) == 1 and ta.NO_PROVIDER in log[0] and "task=research -> none" in log[0]
    assert PROMPT not in debug_log(tmp_path)


# ── the live machine: research unservable, query servable -> query fallback ───

def test_live_shape_falls_back_to_query(tmp_path):
    write_map(tmp_path, [CLAUDE_LOGIN, OLLAMA, OPENROUTER])
    out = route(tmp_path, XAI_API_KEY="x")
    assert 'llm(task=\\"research\\")' not in out
    assert 'llm(task=\\"query\\")' in out
    state = pending(tmp_path)
    assert state["task_type"] == "query"
    assert "unservable:research" in state["method"]
    verdict = bash(tmp_path)
    assert "research" not in verdict   # whatever is held is a door that can succeed
    log = [line for line in debug_log(tmp_path).splitlines() if "F6 UNSERVABLE" in line]
    assert len(log) == 1 and ta.FALLBACK in log[0] and "task=research -> query" in log[0]
    rows = [json.loads(line) for line in
            (tmp_path / ".llm-router/model_tracking.jsonl").read_text().splitlines()]
    assert any("unservable:research" in r.get("classification_method", "") for r in rows)


# ── a research provider is present: unchanged behaviour ──────────────────────

@pytest.mark.parametrize("rows,extra", [
    ([ANTHROPIC_KEY, OLLAMA], {}),
    ([CLAUDE_LOGIN, OLLAMA], {"LLM_ROUTER_CLAUDE_SUBSCRIPTION": "true"}),
    ([OLLAMA], {"PERPLEXITY_API_KEY": "x"}),
])
def test_research_provider_present_keeps_research_directive(tmp_path, rows, extra):
    write_map(tmp_path, rows)
    out = route(tmp_path, **extra)
    assert 'llm(task=\\"research\\")' in out
    assert pending(tmp_path)["task_type"] == "research"
    assert "unservable" not in pending(tmp_path)["method"]
    assert "F6 UNSERVABLE" not in debug_log(tmp_path)


# ── unknown availability fails open to today's directive ─────────────────────

@pytest.mark.parametrize("kind", ["missing", "corrupt", "stale", "other-schema"])
def test_unknown_map_keeps_todays_directive(tmp_path, kind):
    state = tmp_path / ".llm-router"
    state.mkdir(parents=True)
    if kind == "corrupt":
        (state / "resource_map.json").write_text("{not json")
    elif kind == "stale":
        write_map(tmp_path, [OPENROUTER], age_h=7)
    elif kind == "other-schema":
        write_map(tmp_path, [OPENROUTER])
        data = json.loads((state / "resource_map.json").read_text())
        data["schema"] = 99
        (state / "resource_map.json").write_text(json.dumps(data))
    out = route(tmp_path)
    assert 'llm(task=\\"research\\")' in out
    assert pending(tmp_path)["task_type"] == "research"
    assert "F6 UNSERVABLE" not in debug_log(tmp_path)


# ── a user prefix still wins (P2.7) ──────────────────────────────────────────

def test_prefix_still_wins(tmp_path):
    write_map(tmp_path, [CLAUDE_LOGIN, OPENROUTER])
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nprintf '%s\\n' 'Stub answer.'\n")
    codex.chmod(0o700)
    out = route(tmp_path, f"codex: {PROMPT}", CODEX_PATH=str(codex))
    assert "USER PREFIX `codex:`" in out
    assert "no-provider" not in out
    assert "F6 UNSERVABLE" not in debug_log(tmp_path)


# ── the resolver itself ──────────────────────────────────────────────────────

def test_resolver_rules(monkeypatch):
    def avail(*providers):
        monkeypatch.setattr(ta, "available_providers", lambda: frozenset(providers))

    avail("ollama", "openrouter", "xai")
    assert ta.resolve("research") == ("query", ta.FALLBACK)    # local never counts for research
    assert ta.resolve("query") == ("query", ta.SERVABLE)
    avail("openrouter")
    assert ta.resolve("research") == (None, ta.NO_PROVIDER)
    assert ta.resolve("code") == (None, ta.NO_PROVIDER)         # code has no fallback
    assert ta.resolve("image") == ("image", ta.UNCHECKED)
    avail("perplexity")
    assert ta.resolve("research") == ("research", ta.SERVABLE)
    monkeypatch.setattr(ta, "available_providers", lambda: None)
    assert ta.resolve("research") == ("research", ta.UNKNOWN)

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(ta, "available_providers", boom)
    assert ta.resolve("research") == ("research", ta.UNKNOWN)


def _isolate_keys(monkeypatch, home: Path, cwd: Path) -> Path:
    state = home / ".llm-router"
    write_map(home, [CLAUDE_LOGIN])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.chdir(cwd)
    for var in list(os.environ):
        if var.endswith("_API_KEY") or var in ("LLM_ROUTER_CLAUDE_SUBSCRIPTION",
                                               "OPENAI_COMPAT_BASE_URL"):
            monkeypatch.delenv(var)
    return state


def test_key_names_come_from_os_environ(tmp_path, monkeypatch):
    _isolate_keys(monkeypatch, tmp_path / "h", tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    assert ta.available_providers() == frozenset({"xai"})   # empty value, login-only claude out
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:8080/v1")
    assert ta.available_providers() == frozenset({"xai", "openai_compat"})


def test_dotenv_keys_arrive_through_env_loader_only(tmp_path, monkeypatch):
    state = _isolate_keys(monkeypatch, tmp_path / "h", tmp_path)
    (state / ".env").write_text("MINIMAX_API_KEY='k'\n")
    assert ta.available_providers() == frozenset()           # the module reads no .env itself
    from llm_router import env_loader

    for name, value in env_loader.load_dotenv_files(cwd=tmp_path, target={}).items():
        monkeypatch.setenv(name, value)                      # what the hook does at import
    assert ta.available_providers() == frozenset({"minimax"})
    assert ta.resolve("research") == ("research", ta.SERVABLE)   # minimax is in a research chain


@pytest.mark.timing
def test_fifo_dotenv_in_cwd_is_never_opened(tmp_path, monkeypatch):
    """D-153: a FIFO named .env in the cwd hung resolve() for >8 s via a raw read."""
    import builtins
    import threading
    import time

    _isolate_keys(monkeypatch, tmp_path / "h", tmp_path)
    os.mkfifo(tmp_path / ".env")
    (tmp_path / "h" / ".env").write_text("XAI_API_KEY=x\n")
    opened: list[str] = []
    real_read, real_open = Path.read_text, builtins.open

    def spy_read(self, *a, **kw):
        opened.append(self.name)
        return real_read(self, *a, **kw)

    def spy_open(file, *a, **kw):
        opened.append(Path(str(file)).name)
        return real_open(file, *a, **kw)

    monkeypatch.setattr(Path, "read_text", spy_read)
    monkeypatch.setattr(builtins, "open", spy_open)
    out: list = []
    t0 = time.monotonic()
    worker = threading.Thread(target=lambda: out.append(ta.resolve("research")), daemon=True)
    worker.start()
    worker.join(timeout=5)
    elapsed = time.monotonic() - t0
    assert out, "resolve() blocked on the FIFO .env"
    assert elapsed < 1.0
    assert ".env" not in opened
    assert out[0] == (None, ta.NO_PROVIDER)   # neither the FIFO nor ~/.env was read

