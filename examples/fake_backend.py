"""Deterministic fake backend shared by every example. No provider is called and no key is real.

The examples run llm-router's real code (gateway, router, provider layer, ledger). Only the network
edge is replaced:

* ``litellm.acompletion`` (what the gateway and the MCP server call) answers from ``_Fake``: with a
  tool call when the request offers ``get_weather`` and no tool result is in the conversation yet,
  with ``FINAL`` otherwise. The same request always gets the same answer.
* ``urllib.request.urlopen`` (what the in-process SDK's direct executor calls) answers an OpenAI
  chat-completions request with ``FINAL``.
* Any other non-loopback connection is refused and recorded in ``BLOCKED``; each example asserts the
  list stayed empty.

    install()          set the environment and patch the edge (call it before importing llm_router)
    start_gateway()    run the real gateway on 127.0.0.1:<free port>, return its URL
    usage_rows()       the ledger rows (usage.db) the gateway, SDK or MCP server wrote
    python examples/fake_backend.py [--port N]     serve the gateway by hand (Ctrl-C to stop)
    python examples/fake_backend.py mcp-server     the stdio MCP server on this fake (mcp example)

Deliberately outside ``src/``: it is test scaffolding, not a part of the llm-routing wheel.
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROMPT = "what is the weather in Lisbon right now"
ARGS = {"city": "Lisbon"}
RESULT = "RESULT-SUNNY-21C"
FINAL = "It is sunny and 21C in Lisbon."
MODEL = "gpt-4o"            # the bare name a client sends; the gateway qualifies it as openai/gpt-4o
USAGE = (11, 7)             # prompt, completion tokens of every fake answer

BLOCKED: list[str] = []     # non-loopback destinations a call tried to reach
CALLS: list[dict] = []      # every request the fake answered, as the router sent it

_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_PROVIDER_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY", "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "XAI_API_KEY",
    "TOGETHER_API_KEY", "COHERE_API_KEY", "OLLAMA_BASE_URL", "OLLAMA_BUDGET_MODELS",
)


def expect(condition: object, message: str) -> None:
    """The examples' assertion: survives ``python -O`` and names what was expected."""
    if not condition:
        raise AssertionError(message)


class _Fake:
    """Stands in for ``litellm.acompletion``."""

    async def __call__(self, **kw):
        import litellm

        CALLS.append(copy.deepcopy({k: v for k, v in kw.items() if k != "timeout"}))
        names = {(t.get("function") or t).get("name") for t in kw.get("tools") or []}
        if "get_weather" in names and RESULT not in json.dumps(kw.get("messages")):
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_abc123", "type": "function",
                 "function": {"name": "get_weather", "arguments": json.dumps(ARGS)}}]}
        else:
            message = {"role": "assistant", "content": FINAL}
        await asyncio.sleep(0)
        return litellm.ModelResponse(
            model=kw["model"],
            choices=[{"index": 0, "message": message,
                      "finish_reason": "tool_calls" if message["content"] is None else "stop"}],
            usage={"prompt_tokens": USAGE[0], "completion_tokens": USAGE[1],
                   "total_tokens": sum(USAGE)})


def _loopback(host: str) -> bool:
    return host in _LOOPBACK


def _guard_sockets() -> None:
    real_connect = socket.socket.connect

    def guarded(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else str(address)
        if not _loopback(host) and self.family != getattr(socket, "AF_UNIX", None):
            BLOCKED.append(host)
            raise ConnectionRefusedError(f"examples run offline: refused {host}")
        return real_connect(self, address, *a, **kw)

    socket.socket.connect = guarded


def _patch_urlopen() -> None:
    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        host = urllib.parse.urlparse(url).hostname or ""
        if _loopback(host):
            return real_urlopen(req, *a, **kw)
        if url == "https://api.openai.com/v1/chat/completions":
            body = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": FINAL}}],
                "usage": {"prompt_tokens": USAGE[0], "completion_tokens": USAGE[1]}}).encode()
            return io.BytesIO(body)
        BLOCKED.append(host)
        raise urllib.error.URLError(f"examples run offline: refused {url}")

    urllib.request.urlopen = fake_urlopen


def install(home: str | os.PathLike | None = None) -> Path:
    """Isolate state, patch the network edge, return the scratch home (state is ``home/state``)."""
    root = Path(home or os.environ.get("LLM_ROUTER_EXAMPLE_HOME") or tempfile.mkdtemp(prefix="llm-router-ex-"))
    (root / "state").mkdir(parents=True, exist_ok=True)
    for var in _PROVIDER_KEYS:
        os.environ.pop(var, None)
    os.environ.update({
        "HOME": str(root),
        "LLM_ROUTER_HOME": str(root / "state"),
        "LLM_ROUTER_EXAMPLE_HOME": str(root),
        "OPENAI_API_KEY": "sk-test-not-real",           # makes the openai provider eligible; never sent
        "LLM_ROUTER_CLAUDE_SUBSCRIPTION": "false",
        "LLM_ROUTER_GEMINI_SUBSCRIPTION": "false",
        "LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS": "codex,gemini_cli",
        "LLM_ROUTER_ENFORCE": "off",
        "LLM_ROUTER_ALLOW_STUBS": "1",                  # lets a test-key run write the real ledger path
        "LLM_ROUTER_SEMANTIC_CACHE": "off",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        # Frameworks that phone home: off, and the socket guard would refuse them anyway.
        "CREWAI_DISABLE_TELEMETRY": "true", "OTEL_SDK_DISABLED": "true", "CREWAI_TRACING_ENABLED": "false",
        "ANONYMIZED_TELEMETRY": "false", "LANGCHAIN_TRACING_V2": "false", "DO_NOT_TRACK": "1",
        "OPENAI_AGENTS_DISABLE_TRACING": "1", "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    import litellm

    import llm_router.config as config_module
    from llm_router.dynamic_routing import reset_dynamic_routing

    litellm.acompletion = _Fake()
    config_module._config = None
    reset_dynamic_routing()
    _guard_sockets()
    _patch_urlopen()
    return root


def _serve(app, port: int = 0) -> tuple[str, "object"]:
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True).start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    expect(server.started, "the server did not start")
    return f"http://127.0.0.1:{sock.getsockname()[1]}", server


def start_gateway(port: int = 0) -> str:
    """The real llm-router gateway app on a loopback port; returns ``http://127.0.0.1:<port>``."""
    from llm_router.gateway import app

    url, _server = _serve(app, port)
    os.environ["LLM_ROUTER_GATEWAY_URL"] = url + "/v1"     # what the framework adapters read
    return url


def start_anthropic_upstream() -> str:
    """A loopback stand-in for api.anthropic.com's ``/v1/messages`` (the proxy example's upstream)."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def messages(request):
        body = await request.json()
        return JSONResponse({
            "id": "msg_fake", "type": "message", "role": "assistant", "model": body.get("model"),
            "content": [{"type": "text", "text": FINAL}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": USAGE[0], "output_tokens": USAGE[1]}})

    url, _server = _serve(Starlette(routes=[Route("/v1/messages", messages, methods=["POST"])]))
    return url


def usage_rows(expected: int = 1, timeout: float = 10.0) -> list[dict]:
    """The ledger's ``usage`` rows, waiting until at least ``expected`` exist (the write is async)."""
    from llm_router.config import get_config

    deadline = time.monotonic() + timeout
    while True:
        rows: list[dict] = []
        path = Path(str(get_config().llm_router_db_path))
        if path.exists():
            con = sqlite3.connect(str(path))
            con.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in con.execute("SELECT * FROM usage ORDER BY id")]
            except sqlite3.OperationalError:
                rows = []
            finally:
                con.close()
        if len(rows) >= expected or time.monotonic() >= deadline:
            return rows
        time.sleep(0.1)


def finish() -> None:
    """End of an example: nothing reached past the loopback."""
    expect(not BLOCKED, f"an example tried to reach the network: {sorted(set(BLOCKED))}")
    print("OK")


def _main(argv: list[str]) -> int:
    install()
    if argv[:1] == ["mcp-server"]:
        from llm_router.server import main

        main()
        return 0
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
    url = start_gateway(port)
    print(f"fake-backed llm-router gateway: {url}   (OPENAI_BASE_URL={url}/v1)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
