"""Tests for the Claude Code CLI offload adapter (llm_router.claude_agent)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from llm_router import claude_agent
from llm_router.claude_agent import (
    ClaudeResult,
    _model_alias,
    is_claude_available,
    offload_available,
    run_claude,
)


# ── _model_alias: map the router's per-tier anthropic pick to a CLI alias ─────

@pytest.mark.parametrize("model,alias", [
    ("anthropic/claude-opus-4-8", "opus"),
    ("anthropic/claude-sonnet-5", "sonnet"),
    ("anthropic/claude-haiku-4-5", "haiku"),
    ("claude-opus-4-8", "opus"),
    ("sonnet", "sonnet"),
    ("something-unknown", "sonnet"),   # default tier
])
def test_model_alias(model, alias):
    assert _model_alias(model) == alias


# ── run_claude: binary missing → exit 1, never raises ────────────────────────

@pytest.mark.asyncio
async def test_run_claude_binary_not_found():
    with patch("llm_router.claude_agent.find_claude_binary", return_value=None):
        r = await run_claude("hi")
    assert isinstance(r, ClaudeResult)
    assert r.exit_code == 1
    assert not r.success
    assert "not found" in r.content.lower()


# ── run_claude: success path — argv shape + captured text ────────────────────

@pytest.mark.asyncio
async def test_run_claude_success_and_argv():
    async def _stdout():
        yield b"pong\n"

    async def _stderr():
        return
        yield  # pragma: no cover

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = _stdout()
    proc.stderr = _stderr()

    async def _wait():
        return 0
    proc.wait = _wait

    captured = {}

    async def _fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["stdin"] = kwargs.get("stdin")
        return proc

    with (
        patch("llm_router.claude_agent.find_claude_binary", return_value="/usr/bin/claude"),
        patch("llm_router.claude_agent.asyncio.create_subprocess_exec", side_effect=_fake_exec),
        patch("llm_router.safe_subprocess.get_safe_env", return_value={}),
    ):
        r = await run_claude("say pong", model="anthropic/claude-opus-4-8")

    assert r.success and r.content == "pong"
    argv = list(captured["args"])
    assert argv[0] == "/usr/bin/claude"
    assert "-p" in argv and "say pong" in argv
    assert "--output-format" in argv and "text" in argv
    # per-tier alias made it to --model
    assert argv[argv.index("--model") + 1] == "opus"
    # subprocess safety: stdin must be DEVNULL (repo-wide invariant)
    import asyncio as _a
    assert captured["stdin"] == _a.subprocess.DEVNULL


# ── is_claude_available: positive cache short-circuits (no fs probe) ──────────

def test_is_claude_available_positive_cache(monkeypatch):
    monkeypatch.setattr(claude_agent, "_CLAUDE_BINARY_PATH", "/fake/claude")
    probes = {"n": 0}

    def _probe():
        probes["n"] += 1
        return None

    monkeypatch.setattr(claude_agent, "find_claude_binary", _probe)
    assert is_claude_available() is True
    assert probes["n"] == 0


# ── offload_available: the pressure/CLI/subscription gate ─────────────────────

def _cfg(sub=True, cap=0.80):
    return SimpleNamespace(
        llm_router_claude_subscription=sub, llm_router_claude_offload_max_pressure=cap
    )


def test_offload_blocked_when_not_subscription(monkeypatch):
    monkeypatch.setattr(claude_agent, "is_claude_available", lambda: True)
    assert offload_available(_cfg(sub=False)) is False


def test_offload_blocked_when_cli_missing(monkeypatch):
    monkeypatch.setattr(claude_agent, "is_claude_available", lambda: False)
    assert offload_available(_cfg(sub=True)) is False


def test_offload_blocked_when_pressure_over_cap(monkeypatch):
    monkeypatch.setattr(claude_agent, "is_claude_available", lambda: True)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.90)
    assert offload_available(_cfg(sub=True, cap=0.80)) is False


def test_offload_allowed_when_headroom(monkeypatch):
    monkeypatch.setattr(claude_agent, "is_claude_available", lambda: True)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.40)
    assert offload_available(_cfg(sub=True, cap=0.80)) is True
