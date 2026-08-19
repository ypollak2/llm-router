"""Regression: the UserPromptSubmit hook must emit ONLY JSON on stdout.

Audit finding §2.1 (CRITICAL): when direct execution reaches a provider, the
routing decision is logged via ``llm_router.model_tracking``. The hook process never
calls ``configure_logging()``, so structlog falls back to its default
``PrintLogger`` which writes to *stdout*. The debug line

    ... [debug    ] Tracked: qwen3.5:latest for query/simple (heuristic)

lands ahead of the JSON payload, and Claude Code parses hook stdout as JSON —
``json.loads`` then fails with "Extra data" and the entire routing decision is
silently discarded.

The line-2580 guard in auto-route.py tries to muzzle this by raising the stdlib
logger level and redirecting ``sys.stdout``, but structlog's PrintLogger ignores
both, so the leak survives. These tests pin the invariant end-to-end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


class _StubOllama(BaseHTTPRequestHandler):
    """Minimal Ollama-compatible stub (mirrors the auditor's harness stub)."""

    # Only advertise models the audit showed as *available* — the chain must not
    # blindly pick qwen3.5:latest (that overlap is finding §2.4).
    TAGS = {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.2:3b"}]}

    def log_message(self, *_a):  # silence stderr access log
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
            self._send(self.TAGS)
        else:
            self._send({"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        model = payload.get("model")
        if self.path.startswith("/api/chat"):
            self._send({"model": model, "done": True, "prompt_eval_count": 10,
                        "eval_count": 5,
                        "message": {"role": "assistant", "content": "STUB_ANSWER_FROM_OLLAMA"}})
        elif self.path.startswith("/v1/chat"):
            self._send({"choices": [{"message": {"role": "assistant",
                        "content": "STUB_ANSWER_FROM_OLLAMA"}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        else:
            self._send({"model": model, "response": "STUB_ANSWER_FROM_OLLAMA",
                        "done": True, "prompt_eval_count": 10, "eval_count": 5})


@pytest.fixture
def stub_ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _run_hook(prompt: str, home: Path, ollama_url: str, extra_env=None):
    (home / ".llm-router").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["LLM_ROUTER_OLLAMA_URL"] = ollama_url
    # Force the *direct-execution* path (the one that logs a routing decision).
    env.pop("LLM_ROUTER_DIRECT_EXECUTION", None)
    env["LLM_ROUTER_DISABLE_LLM_CLASSIFIERS"] = "1"  # deterministic heuristic classify
    # Ensure the chain does not wander onto paid providers during the test.
    env["OPENAI_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""
    env["GOOGLE_API_KEY"] = ""
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit",
                          "prompt": prompt, "session_id": "stdout-purity"}),
        capture_output=True, text=True, env=env,
    )


def test_stdout_is_pure_json_when_provider_reached(tmp_path, stub_ollama):
    """The whole of stdout must parse as a single JSON object — no log preamble."""
    result = _run_hook("What is the capital of France?", tmp_path, stub_ollama)
    out = result.stdout
    assert out.strip(), f"hook produced no stdout (stderr: {result.stderr[:400]})"
    # First non-space char must open the JSON object — a leaked log line fails here.
    assert out.lstrip()[0] == "{", (
        f"stdout does not start with JSON — leaked preamble:\n{out[:200]!r}"
    )
    # And the entire payload must be valid JSON (no trailing/leading garbage).
    try:
        json.loads(out)
    except json.JSONDecodeError as exc:  # pragma: no cover - failure message
        pytest.fail(f"stdout is not valid JSON ({exc}); raw head:\n{out[:200]!r}")


def test_stdout_pure_json_even_at_debug_level(tmp_path, stub_ollama):
    """Even with DEBUG logging requested, stdout stays JSON-only (logs → stderr)."""
    result = _run_hook(
        "What is the capital of France?", tmp_path, stub_ollama,
        extra_env={"LLM_ROUTER_LOG_LEVEL": "DEBUG"},
    )
    out = result.stdout
    assert out.strip(), f"hook produced no stdout (stderr: {result.stderr[:400]})"
    assert "Tracked:" not in out, (
        f"model-tracking debug line leaked onto stdout:\n{out[:200]!r}"
    )
    json.loads(out)  # must not raise
