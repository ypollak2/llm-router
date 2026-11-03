"""BIND W2: the MCP errors that were left, and the research call that cannot succeed.

BIND-R2 read the 16 tool errors of the audit window: 10 bare ``Error executing tool`` (5 from
a server that predated a deploy until the owner reconnected it, 4 from ``task="research"``
with no provider, 1 unexplained), 5 ``deadline 90s exceeded`` while a benchmark held Ollama,
and 1 ``Connection closed`` while the server restarted under a deploy (a client-side message
the server cannot write).

The bare-error class was already made readable by D-141 (``mcp_door``, merged 2026-10-16)
and the empty research chain became a refusal in F25 (2026-10-17): both landed after the
errors. What this file adds is the stale-server message and the fail-fast for a busy local
queue, and it pins the research call end to end through the real server.

Every test drives a tool over the MCP protocol (``mcp.client.Client``, in memory) or through
``IdentityMCPServer.call_tool``. No provider is called.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from llm_router import call_identity, mcp_door

PROMPT = "W2-PROMPT-CANARY please answer"
ROOT = Path(__file__).resolve().parents[1]


def _text(result) -> str:
    return result.content[0].text


def _probe(fn, name: str | None = None):
    server = call_identity.IdentityMCPServer("probe")
    server.tool(name=name)(fn)
    return server


async def _call(server, name: str, args: dict, guard_s: float = 10.0):
    from mcp.client import Client

    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args)

    return await asyncio.wait_for(go(), guard_s)


def _real_server():
    from llm_router import server

    return server.mcp


@pytest.fixture(autouse=True)
def _clean_door_state():
    """The door keeps process-wide state (start snapshot, busy window): isolate every test."""
    saved = dict(mcp_door._START)
    mcp_door.reset_busy()
    yield
    mcp_door._START.update(saved)
    mcp_door.reset_busy()


# ── C. a stale server ────────────────────────────────────────────────────────

def test_the_fingerprint_moves_when_a_file_is_added_or_touched(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "a.cpython.py").write_text("ignored")
    before = mcp_door.code_fingerprint(tmp_path)
    assert before[1] == 1
    assert mcp_door.code_fingerprint(tmp_path) == before   # reading does not change it
    (tmp_path / "pkg" / "b.py").write_text("y = 2\n")
    added = mcp_door.code_fingerprint(tmp_path)
    assert added[1] == 2 and added != before
    time.sleep(0.01)
    (tmp_path / "pkg" / "a.py").write_text("x = 3\n")
    assert mcp_door.code_fingerprint(tmp_path)[2] > added[2]


def test_no_snapshot_means_no_stale_claim():
    mcp_door._START.update(fp=None, at=None)
    assert mcp_door.stale_reason() is None


def test_an_unchanged_package_is_not_stale():
    mcp_door._START.update(fp=mcp_door.code_fingerprint(), at=time.time())
    assert mcp_door.stale_reason() is None


@pytest.mark.parametrize("start,now,expect", [
    (("15.9.0", 300, 5), ("16.0.0", 300, 5), "version 15.9.0 -> 16.0.0"),
    (("16.0.0", 300, 5), ("16.0.0", 303, 5), "+3 python file(s)"),
    (("16.0.0", 300, 5), ("16.0.0", 300, 9), "files changed on disk"),
])
def test_the_reason_says_what_changed(monkeypatch, start, now, expect):
    mcp_door._START.update(fp=start, at=1_700_000_000.0)
    monkeypatch.setattr(mcp_door, "code_fingerprint", lambda root=None: now)
    reason = mcp_door.stale_reason()
    assert reason and expect in reason and "started 2023-11-14" in reason


@pytest.mark.asyncio
async def test_a_crash_in_a_stale_server_says_reconnect_and_keeps_the_original_error(monkeypatch):
    mcp_door._START.update(fp=("15.9.0", 300, 5), at=time.time() - 3600)
    monkeypatch.setattr(mcp_door, "code_fingerprint", lambda root=None: ("16.0.0", 301, 9))

    async def boom() -> str:
        mcp_door.attach_trace(["ollama/qwen3-coder:30b"])
        raise ImportError("cannot import name 'resolve_door' from 'llm_router.tool_surface' "
                          "with key AKIAIOSFODNN7EXAMPLE")

    result = await _call(_probe(boom, name="llm_query"), "llm_query", {})
    text = _text(result)
    first, second, third = text.splitlines()
    assert result.is_error
    assert first.startswith("llm-router error: this MCP server is running code from before the last update")
    assert "/mcp" in first and "version 15.9.0 -> 16.0.0" in first
    assert second.startswith("original error: ImportError: cannot import name 'resolve_door'")
    assert "AKIAIOSFODNN7EXAMPLE" not in text                 # the original is still scrubbed
    assert third == "tried: ollama/qwen3-coder:30b"
    assert "Error executing tool" not in text


@pytest.mark.asyncio
async def test_a_crash_in_a_fresh_server_keeps_the_plain_readable_error():
    mcp_door._START.update(fp=mcp_door.code_fingerprint(), at=time.time())

    async def boom() -> str:
        raise ValueError("No providers available for query/balanced.")

    text = _text(await _call(_probe(boom), "boom", {}))
    assert text == "llm-router error: ValueError: No providers available for query/balanced."


@pytest.mark.asyncio
async def test_the_real_llm_door_reports_a_stale_server(monkeypatch, temp_db):
    mcp_door._START.update(fp=("15.9.0", 300, 5), at=time.time())
    monkeypatch.setattr(mcp_door, "code_fingerprint", lambda root=None: ("16.0.0", 300, 5))
    err = AttributeError("module 'llm_router.router' has no attribute 'route_and_call_v2'")
    with patch("llm_router.tools.text.route_and_call", new_callable=lambda: AsyncMock(side_effect=err)):
        result = await _call(_real_server(), "llm", {"prompt": PROMPT, "task": "query"})
    assert result.is_error
    assert "Reconnect it with /mcp" in _text(result)
    assert "original error: AttributeError: module 'llm_router.router' has no attribute" in _text(result)


@pytest.mark.timeout(120)
def test_the_server_takes_its_snapshot_at_start(tmp_path):
    """The wiring: importing the server module is what starts the clock. The child gets a temp
    HOME on purpose: importing llm_router.server refreshes the hook files under
    $HOME/.claude/hooks, and a test must never be able to reach the real ones."""
    code = (
        "import llm_router.server\n"
        "from llm_router import mcp_door\n"
        "fp = mcp_door._START['fp']\n"
        "print('SNAP', fp is not None, mcp_door.stale_reason())\n"
    )
    env = {**os.environ, "HOME": str(tmp_path), "LLM_ROUTER_HOME": str(tmp_path / ".llm-router"),
           "PYTHONPATH": str(ROOT / "src")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(ROOT), timeout=120, env=env)
    assert out.returncode == 0, out.stderr[-500:]
    assert "SNAP True None" in out.stdout


# ── D. a busy local queue ────────────────────────────────────────────────────

async def _hang_on(models: list[str], seconds: float = 5.0) -> str:
    mcp_door.attach_trace(models)
    await asyncio.sleep(seconds)
    return "late"


@pytest.mark.asyncio
async def test_a_local_only_deadline_opens_the_busy_window(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "0.3")

    async def slow() -> str:
        return await _hang_on(["ollama/a"])

    assert not mcp_door.busy_window_open()
    text = _text(await _call(_probe(slow, name="llm"), "llm", {}))
    assert text.startswith("llm-router error: deadline 0.3s exceeded; tried: ollama/a")
    assert mcp_door.busy_window_open()


@pytest.mark.asyncio
async def test_a_cloud_attempt_in_the_trace_does_not_open_it(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "0.3")

    async def slow() -> str:
        return await _hang_on(["ollama/a", "openai/gpt-x"])

    await _call(_probe(slow, name="llm"), "llm", {})
    assert not mcp_door.busy_window_open()


@pytest.mark.timing  # asserts elapsed vs the deadline: serial `timing` CI lane
@pytest.mark.asyncio
async def test_in_the_window_a_still_local_call_is_given_up_early(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "3")
    monkeypatch.setattr(mcp_door, "BUSY_PROBE_S", 0.2)
    mcp_door._BUSY.update(until=time.monotonic() + 60, waited=90.0)

    async def slow() -> str:
        return await _hang_on(["ollama/a", "ollama/b"])

    t0 = time.monotonic()
    result = await _call(_probe(slow, name="llm"), "llm", {})
    elapsed = time.monotonic() - t0
    assert result.is_error and elapsed < 2.0, elapsed
    assert _text(result) == (
        "llm-router error: local models are busy: still waiting on ollama/a, ollama/b after "
        "0.2s, and the last call waited 90s for them; gave up early (limit 3s). "
        "Retry in a minute, use tier=fast, or handle it yourself."
    )


@pytest.mark.asyncio
async def test_in_the_window_a_call_that_reached_a_cloud_model_is_left_alone(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "3")
    monkeypatch.setattr(mcp_door, "BUSY_PROBE_S", 0.1)
    mcp_door._BUSY.update(until=time.monotonic() + 60, waited=90.0)

    async def slow_cloud() -> str:
        return await _hang_on(["ollama/a", "openai/gpt-x"], seconds=0.4)

    assert _text(await _call(_probe(slow_cloud, name="llm"), "llm", {})) == "late"


@pytest.mark.asyncio
async def test_an_answer_before_the_probe_is_untouched(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "3")
    monkeypatch.setattr(mcp_door, "BUSY_PROBE_S", 0.5)
    mcp_door._BUSY.update(until=time.monotonic() + 60, waited=90.0)

    async def quick() -> str:
        mcp_door.attach_trace(["ollama/a"])
        return "answered"

    assert _text(await _call(_probe(quick, name="llm"), "llm", {})) == "answered"


@pytest.mark.asyncio
async def test_outside_the_window_the_full_deadline_applies(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "3")
    monkeypatch.setattr(mcp_door, "BUSY_PROBE_S", 0.1)

    async def slowish() -> str:
        return await _hang_on(["ollama/a"], seconds=0.4)

    assert _text(await _call(_probe(slowish, name="llm"), "llm", {})) == "late"


@pytest.mark.timing  # asserts a time.monotonic() window: serial `timing` CI lane (test_timing_lane)
@pytest.mark.asyncio
async def test_a_deadline_opens_the_window_for_its_whole_length(monkeypatch):
    monkeypatch.setattr(mcp_door, "BUSY_WINDOW_S", 60.0)
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "0.2")

    async def slow() -> str:
        return await _hang_on(["ollama/a"])

    await _call(_probe(slow, name="llm"), "llm", {})
    left = mcp_door._BUSY["until"] - time.monotonic()
    assert 50.0 < left <= 60.0, left


def test_the_busy_window_closes():
    mcp_door._BUSY["until"] = time.monotonic() + 0.05
    assert mcp_door.busy_window_open()
    time.sleep(0.1)
    assert not mcp_door.busy_window_open()
    mcp_door.reset_busy()
    assert mcp_door._BUSY == {"until": 0.0, "waited": 0.0}


# ── the research call that cannot succeed, end to end ────────────────────────

@pytest.mark.asyncio
async def test_research_with_no_provider_is_an_answer_that_says_what_to_do(no_providers_env, monkeypatch):
    """The 4 bare errors of 2026-10-16: llm(task="research") on a machine with no research
    provider. Through the real server it now returns a refusal with the remedy, not
    ``Error executing tool llm`` and not a stack of provider noise."""
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    result = await _call(_real_server(), "llm", {"prompt": PROMPT, "task": "research"}, guard_s=60)
    text = _text(result)
    assert not result.is_error
    assert "Error executing tool" not in text
    assert "research refused (refused_no_research_provider)" in text, text
    assert "Do this research yourself" in text
