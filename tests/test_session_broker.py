"""Tests for the session broker (P1) — transport, auth, dispatch.

Hermetic: uses a temp Unix socket + secret and a fake echo adapter, so no real
Codex CLI is exercised.
"""

import asyncio
import os
import secrets as _secrets
import stat
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from llm_router import session_broker as sb


@pytest.fixture
def broker_paths(tmp_path, monkeypatch):
    # AF_UNIX paths are capped at ~104 chars; pytest's tmp_path is long, so put
    # the socket under the system tmpdir with a short random name.
    sock = Path(tempfile.gettempdir()) / f"cz-{_secrets.token_hex(4)}.sock"
    secret = tmp_path / "broker.secret"
    monkeypatch.setenv("LLM_ROUTER_BROKER_SOCK", str(sock))
    monkeypatch.setenv("LLM_ROUTER_BROKER_SECRET_FILE", str(secret))
    yield sock, secret
    try:
        if sock.exists():
            os.unlink(sock)
    except OSError:
        pass


async def _echo_adapter(job: dict) -> dict:
    return {"status": "ok", "text": f"echo:{job.get('prompt', '')}",
            "usage": {"input_tokens": 1, "output_tokens": 1, "estimated_cost_usd": 0.0}}


@asynccontextmanager
async def _server(adapters=None):
    """Start a broker in THIS event loop (avoids cross-loop fixture issues)."""
    srv = sb.BrokerServer(adapters or {"fake": _echo_adapter})
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_ping_reports_providers(broker_paths):
    async with _server():
        resp = await sb.BrokerClient().ping(timeout=2.0)
    assert resp is not None
    assert resp["status"] == "ok"
    assert resp["providers"] == ["fake"]


@pytest.mark.asyncio
async def test_run_dispatches_to_adapter(broker_paths):
    async with _server():
        resp = await sb.BrokerClient().run("fake", "fake/model", "hello", timeout=5.0)
    assert resp["status"] == "ok"
    assert resp["text"] == "echo:hello"


@pytest.mark.asyncio
async def test_unknown_provider_rejected(broker_paths):
    async with _server():
        resp = await sb.BrokerClient().run("not-allowed", "x", "hi", timeout=5.0)
    assert resp["status"] == "error"
    assert "not allowed" in resp["error"]


@pytest.mark.asyncio
async def test_socket_is_owner_only(broker_paths):
    async with _server():
        mode = stat.S_IMODE(sb.broker_socket_path().stat().st_mode)
    assert mode == 0o600, f"socket must be 0600, got {oct(mode)}"


@pytest.mark.asyncio
async def test_bad_signature_rejected(broker_paths):
    """A frame signed with the WRONG secret must be rejected as unauthorized."""
    sock, _ = broker_paths
    async with _server():
        reader, writer = await asyncio.open_unix_connection(path=str(sock))
        body = {"v": sb.PROTOCOL_VERSION, "op": "ping"}
        forged = b'{"body": ' + sb._canonical(body) + b', "sig": "deadbeef"}\n'
        writer.write(forged)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()
    resp = sb._decode(sb.load_or_create_secret(), line)
    assert resp["status"] == "error"
    assert resp["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_broker_socket_present_helper(broker_paths):
    async with _server():
        assert sb.broker_socket_present() is True


# ── P1 phase 2: gateway/router delegation ─────────────────────────────────────

async def _codex_echo(job: dict) -> dict:
    return {"status": "ok", "text": f"BROKERED:{job.get('prompt', '')}",
            "usage": {"input_tokens": 5, "output_tokens": 3, "estimated_cost_usd": 0.0}}


@pytest.mark.asyncio
async def test_maybe_broker_dispatch_delegates_when_local_disabled(
    broker_paths, monkeypatch
):
    """When codex is locally disabled and the broker offers it, delegate."""
    from llm_router.router import _maybe_broker_dispatch

    sb._provider_cache = None  # reset capability cache
    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")
    async with _server({"codex": _codex_echo}):
        resp = await _maybe_broker_dispatch("codex", "gpt-5.5", "hello")
    assert resp is not None
    assert resp.provider == "codex"
    assert resp.model == "codex/gpt-5.5"
    assert resp.content == "BROKERED:hello"
    assert resp.cost_usd == 0.0


@pytest.mark.asyncio
async def test_maybe_broker_dispatch_skips_when_local_enabled(broker_paths, monkeypatch):
    """When codex is NOT disabled locally, don't delegate (use the local path)."""
    from llm_router.router import _maybe_broker_dispatch

    sb._provider_cache = None
    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    resp = await _maybe_broker_dispatch("codex", "gpt-5.5", "hello")
    assert resp is None


@pytest.mark.asyncio
async def test_maybe_broker_dispatch_none_when_broker_absent(broker_paths, monkeypatch):
    """Disabled locally but no broker running → None (caller falls back / errors)."""
    from llm_router.router import _maybe_broker_dispatch

    sb._provider_cache = None
    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex")
    resp = await _maybe_broker_dispatch("codex", "gpt-5.5", "hello")  # no _server()
    assert resp is None
