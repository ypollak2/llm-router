"""Session broker — execute "free-but-gated" backends for the headless gateway.

Problem (P1): LLM Router's always-on gateway daemon runs headless (launchd), so it
cannot reach the interactively-authenticated backends the terminal session can —
Codex (ChatGPT subscription CLI), Gemini CLI, etc. Complex-tier routes then
degrade to local Ollama only.

Solution: a small broker process launched *from the interactive user session*
(`llm_router broker run`). It listens on a per-user Unix socket. The gateway keeps
ALL routing/policy/metering; when it picks a broker-only provider it delegates
just that one backend call to the broker, which runs the CLI adapter with the
user's live credentials and streams the result back.

Security model (local-only, defence in depth):
  * Unix domain socket at ~/.llm-router/broker.sock, mode 0600, owner-only.
  * Every message is HMAC-signed with a per-user secret (~/.llm-router/broker.secret,
    mode 0600). Tampered or unsigned messages are rejected.
  * Providers are an explicit allowlist (only registered adapters run).
  * Adapters exec exact argv arrays (no shell) — inherited from codex_agent.
  * Per-adapter concurrency cap (default 1 for interactive CLIs).

This module is transport + auth + dispatch only. It makes NO routing decisions.
Phase 1: ping + run, Codex adapter. Gemini/Claude adapters and the gateway-side
delegation wiring layer on top unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from llm_router.logging import get_logger

log = get_logger("llm_router.session_broker")

PROTOCOL_VERSION = 1
_MAX_MSG_BYTES = 4 * 1024 * 1024  # 4 MiB cap on a single framed message
_DEFAULT_CONCURRENCY = 1

# An adapter takes a validated job dict and returns a result dict
# {"status": "ok", "text": str, "usage": {...}} or {"status": "error", "error": str}.
Adapter = Callable[[dict], Awaitable[dict]]


def _llm_router_dir() -> Path:
    d = Path.home() / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    return d


def broker_socket_path() -> Path:
    """Per-user Unix socket path. Override with LLM_ROUTER_BROKER_SOCK (tests)."""
    override = os.environ.get("LLM_ROUTER_BROKER_SOCK")
    return Path(override) if override else _llm_router_dir() / "broker.sock"


def _secret_path() -> Path:
    override = os.environ.get("LLM_ROUTER_BROKER_SECRET_FILE")
    return Path(override) if override else _llm_router_dir() / "broker.secret"


def load_or_create_secret() -> bytes:
    """Return the shared HMAC secret, creating a random one (0600) if absent."""
    path = _secret_path()
    if path.exists():
        return path.read_bytes().strip()
    secret = secrets.token_hex(32).encode()
    # Create with owner-only perms from the start (no race window at 0644).
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    return secret


# ── Framing + auth ────────────────────────────────────────────────────────────
# Wire format: one JSON object per line: {"body": <obj>, "sig": <hex hmac>}.
# The signature covers the canonical (sorted-keys, compact) encoding of `body`.

def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _sign(secret: bytes, body: dict) -> str:
    return hmac.new(secret, _canonical(body), hashlib.sha256).hexdigest()


def _verify(secret: bytes, body: dict, sig: str) -> bool:
    expected = _sign(secret, body)
    # Constant-time compare; hmac.compare_digest tolerates str.
    return hmac.compare_digest(expected, sig or "")


def _encode(secret: bytes, body: dict) -> bytes:
    return (json.dumps({"body": body, "sig": _sign(secret, body)}) + "\n").encode()


def _decode(secret: bytes, line: bytes) -> dict:
    """Parse and authenticate a framed message; return the inner body.

    Raises ValueError on malformed or unauthenticated messages.
    """
    try:
        env = json.loads(line)
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed frame: {e}") from e
    if not isinstance(env, dict) or "body" not in env or "sig" not in env:
        raise ValueError("frame missing body/sig")
    body = env["body"]
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    if not _verify(secret, body, env["sig"]):
        raise ValueError("bad signature")
    if body.get("v") != PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version: {body.get('v')}")
    return body


# ── Server ────────────────────────────────────────────────────────────────────
class BrokerServer:
    """Async Unix-socket broker. Dispatches `run` jobs to registered adapters."""

    def __init__(
        self,
        adapters: dict[str, Adapter],
        *,
        socket_path: Path | None = None,
        secret: bytes | None = None,
        concurrency: int = _DEFAULT_CONCURRENCY,
    ) -> None:
        self._adapters = dict(adapters)
        self._socket_path = socket_path or broker_socket_path()
        self._secret = secret or load_or_create_secret()
        self._sem = asyncio.Semaphore(concurrency)
        self._server: asyncio.AbstractServer | None = None

    @property
    def providers(self) -> list[str]:
        return sorted(self._adapters)

    async def start(self) -> None:
        # Remove a stale socket file so bind() succeeds after an unclean exit.
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass
        # Bind under a restrictive umask so the socket is owner-only (0600).
        old_umask = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=str(self._socket_path)
            )
        finally:
            os.umask(old_umask)
        try:
            os.chmod(self._socket_path, 0o600)  # belt-and-suspenders
        except OSError:
            pass
        log.info("session_broker_started path=%s providers=%s",
                 self._socket_path, self.providers)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            if not line or len(line) > _MAX_MSG_BYTES:
                return
            try:
                body = _decode(self._secret, line)
            except ValueError as e:
                # Never echo details that could aid probing; log locally only.
                log.warning("broker_reject reason=%s", e)
                await self._send(writer, {"status": "error", "error": "unauthorized"})
                return
            resp = await self._dispatch(body)
            await self._send(writer, resp)
        except Exception as e:  # never let one connection kill the server
            log.debug("broker_handler_error error=%s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _send(self, writer: asyncio.StreamWriter, body: dict) -> None:
        body = {"v": PROTOCOL_VERSION, **body}
        writer.write(_encode(self._secret, body))
        await writer.drain()

    async def _dispatch(self, body: dict) -> dict:
        op = body.get("op")
        if op == "ping":
            return {"status": "ok", "providers": self.providers}
        if op == "run":
            provider = body.get("provider", "")
            adapter = self._adapters.get(provider)
            if adapter is None:
                return {"status": "error", "error": f"provider not allowed: {provider}"}
            async with self._sem:  # per-adapter concurrency cap
                try:
                    return await adapter(body)
                except Exception as e:
                    log.warning("broker_adapter_error provider=%s error=%s", provider, e)
                    return {"status": "error", "error": f"adapter failure: {e}"}
        return {"status": "error", "error": f"unknown op: {op}"}


# ── Client ────────────────────────────────────────────────────────────────────
class BrokerClient:
    """Client the gateway/router uses to delegate a single backend call."""

    def __init__(
        self, *, socket_path: Path | None = None, secret: bytes | None = None
    ) -> None:
        self._socket_path = socket_path or broker_socket_path()
        self._secret = secret or load_or_create_secret()

    async def _roundtrip(self, body: dict, timeout: float) -> dict:
        body = {"v": PROTOCOL_VERSION, **body}
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path=str(self._socket_path)), timeout=timeout
        )
        try:
            writer.write(_encode(self._secret, body))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        finally:
            writer.close()
        if not line:
            raise ConnectionError("broker closed connection with no response")
        return _decode(self._secret, line)

    async def ping(self, timeout: float = 2.0) -> dict | None:
        """Return the ping result ({"status","providers"}) or None if unreachable."""
        try:
            return await self._roundtrip({"op": "ping"}, timeout)
        except (OSError, asyncio.TimeoutError, ValueError, ConnectionError):
            return None

    async def run(
        self, provider: str, model: str, prompt: str, *, timeout: float = 300.0
    ) -> dict:
        """Delegate one backend call. Returns the adapter result dict."""
        return await self._roundtrip(
            {"op": "run", "provider": provider, "model": model, "prompt": prompt},
            timeout,
        )


def broker_socket_present() -> bool:
    """Cheap sync check (socket file exists). Use BrokerClient.ping for liveness."""
    try:
        return broker_socket_path().is_socket()
    except OSError:
        return False


# Short-TTL cache of which providers the broker offers, so the router can check
# on every dispatch without a socket round-trip each time. Negative results are
# cached too (broker down), bounding delegation attempts when it's absent.
_provider_cache: "tuple[frozenset[str], float] | None" = None
_PROVIDER_CACHE_TTL = 10.0


async def broker_providers(timeout: float = 1.0) -> frozenset[str]:
    """Return the providers the running broker offers (empty set if unreachable).

    Cached for ~10s. Fast path: if there's no socket file, skip the ping.
    """
    global _provider_cache
    now = time.monotonic()
    if _provider_cache is not None and now - _provider_cache[1] < _PROVIDER_CACHE_TTL:
        return _provider_cache[0]
    if not broker_socket_present():
        _provider_cache = (frozenset(), now)
        return _provider_cache[0]
    resp = await BrokerClient().ping(timeout=timeout)
    provs = (
        frozenset(resp.get("providers", []))
        if resp and resp.get("status") == "ok"
        else frozenset()
    )
    _provider_cache = (provs, now)
    return provs


# ── Adapters ──────────────────────────────────────────────────────────────────
async def codex_adapter(job: dict) -> dict:
    """Run one Codex CLI call with the interactive session's credentials."""
    from llm_router.codex_agent import run_codex

    model = job.get("model", "")
    # Strip a leading provider prefix ("codex/gpt-5.5" -> "gpt-5.5").
    model_name = model.split("/", 1)[1] if "/" in model else model
    result = await run_codex(job.get("prompt", ""), model=model_name)
    if not result.success:
        return {"status": "error", "error": f"codex exit {result.exit_code}: "
                                             f"{result.content[:200]}"}
    return {
        "status": "ok",
        "text": result.content,
        "usage": {
            # Codex is a prepaid subscription — marginal cost ≈ 0. Token counts
            # are best-effort estimates; the gateway meters the real figures.
            "input_tokens": max(1, len(job.get("prompt", "")) // 4),
            "output_tokens": max(1, len(result.content) // 4),
            "estimated_cost_usd": 0.0,
        },
    }


def default_adapters() -> dict[str, Adapter]:
    """Adapters available from THIS interactive session (allowlist)."""
    from llm_router.codex_agent import is_codex_available

    adapters: dict[str, Adapter] = {}
    if is_codex_available():
        adapters["codex"] = codex_adapter
    return adapters


async def run_broker_server() -> None:
    """Entry point for `llm_router broker run` — serve until interrupted."""
    adapters = default_adapters()
    if not adapters:
        log.warning("session_broker: no interactive backends available "
                    "(is Codex CLI installed + authenticated?)")
    # Concurrency > 1 so a parallel swarm (loophole) delegating to Codex/Gemini
    # doesn't serialize at the broker. CLIs run as independent processes; the
    # subscription rate-limiter is the real cap (and it degrades gracefully).
    try:
        concurrency = max(1, int(os.environ.get("LLM_ROUTER_BROKER_CONCURRENCY", "4")))
    except ValueError:
        concurrency = 4
    server = BrokerServer(adapters, concurrency=concurrency)
    await server.start()
    print(f"⚡ LLM Router session broker listening at {server._socket_path}")
    print(f"   backends: {', '.join(server.providers) or '(none)'}")
    print("   the headless gateway will delegate gated calls here. Ctrl-C to stop.")
    try:
        await server.serve_forever()
    finally:
        await server.stop()
