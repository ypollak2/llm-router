"""Shared fixtures for the end-to-end suite.

These tests drive the real stack: a real local model, a real HTTP server, real
SQLite ledgers, the real CLI as a subprocess. Nothing about the routing path is
mocked — that is the entire point. The audit that prompted this suite said it
plainly: "most tests are unit/mocked and do not exercise these seams", and every
defect the re-audit found lived in a seam between two components that each had
passing unit tests.

Two absolute rules:

1. **Never touch the user's real ``~/.llm-router``.** These tests make real model
   calls, which write cost rows, savings rows and receipts. ``isolated_home``
   redirects all of it into a tmp_path and every test in this package uses it.
2. **Skip, never fail, when the environment is missing.** A developer without
   Ollama running should see skips, not a wall of red.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request

import pytest

#: Smallest/fastest local model on this machine (~6s p50). E2E wants a real
#: model call, not a big one.
E2E_MODEL = os.environ.get("LLM_ROUTER_E2E_MODEL", "lfm2.5:8b")
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


def _ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return []


@pytest.fixture(scope="session")
def ollama_model() -> str:
    """The model these tests route to, or skip if Ollama cannot serve it."""
    models = _ollama_models()
    if not models:
        pytest.skip(f"Ollama not reachable at {OLLAMA_URL}")
    if E2E_MODEL not in models:
        pytest.skip(f"{E2E_MODEL} not installed (have: {', '.join(models[:4])})")
    return E2E_MODEL


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect every on-disk ledger into tmp_path.

    Real calls write usage.db, savings_log.jsonl, receipts.db and
    session_spend.json. Without this the suite would silently corrupt the
    developer's own cost history — and worse, read it back and assert on it.
    """
    home = tmp_path / "home"
    state = home / ".llm-router"
    state.mkdir(parents=True)
    # Three levers, because there is no single one. paths.py's own docstring
    # records the reason: ~120 sites in src/ compose "~/.llm-router" directly and
    # usage.db alone is resolved about 23 different ways, so LLM_ROUTER_HOME
    # (the supported override) covers the paths that ask paths.py, HOME covers
    # the subprocesses, and the Path.home patch covers the ones that call it
    # directly at runtime.
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    # cost.py refuses to write usage.db when PYTEST_CURRENT_TEST is set and the
    # path resolves to the configured state dir — a guard against unisolated
    # tests polluting a real developer's cost history. These tests ARE isolated
    # and the data is real, which is what its documented escape hatch is for.
    monkeypatch.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home))

    # The config singleton caches provider availability; force a rebuild so the
    # env below is what the router actually sees.
    import llm_router.config as config_module

    config_module._config = None
    yield home
    config_module._config = None


@pytest.fixture
def ollama_only_env(isolated_home, ollama_model, monkeypatch):
    """A router configured to reach exactly one real provider: local Ollama.

    No API keys, no subscription — so if a test dispatches, it demonstrably went
    to the local model and not to something that happened to be configured on
    the developer's machine.
    """
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                "PERPLEXITY_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY",
                "OPENROUTER_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_URL)
    # OLLAMA_BUDGET_MODELS is the env fallback config.all_ollama_models() reads.
    # Under pytest it deliberately skips the discovery cache, so this is the
    # only way to tell the router which local models actually exist.
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", ollama_model)
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "false")
    monkeypatch.setenv("LLM_ROUTER_GEMINI_SUBSCRIPTION", "false")
    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "off")

    import llm_router.config as config_module

    config_module._config = None
    return f"ollama/{ollama_model}"


@pytest.fixture
def live_gateway(ollama_only_env):
    """The real gateway on a real loopback socket, with a real model behind it."""
    import uvicorn

    from llm_router.gateway import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        pytest.fail("gateway did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def read_ledgers(isolated_home):
    """Read back what a real call actually wrote to disk."""
    import sqlite3

    def _read() -> dict:
        base = isolated_home / ".llm-router"
        out: dict = {"savings_jsonl": [], "usage_rows": [], "receipts": 0}

        jsonl = base / "savings_log.jsonl"
        if jsonl.exists():
            out["savings_jsonl"] = [
                json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()
            ]

        db = base / "usage.db"
        if db.exists():
            conn = sqlite3.connect(db)
            try:
                out["usage_rows"] = [
                    dict(zip([c[0] for c in cur.description], row))
                    for cur in [conn.execute(
                        "SELECT model, cost_usd, input_tokens, output_tokens FROM usage"
                    )]
                    for row in cur.fetchall()
                ]
            except sqlite3.Error:
                pass
            conn.close()

        rdb = base / "receipts.db"
        if rdb.exists():
            conn = sqlite3.connect(rdb)
            try:
                out["receipts"] = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            except sqlite3.Error:
                pass
            conn.close()
        return out

    return _read
