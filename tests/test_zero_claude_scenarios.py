"""End-to-end hook scenarios for strict zero-Claude routing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


class _OllamaHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self) -> None:
        body_size = int(self.headers.get("Content-Length", "0"))
        self.__class__.requests.append(json.loads(self.rfile.read(body_size)))
        response = json.dumps(
            {
                "message": {"content": "An external provider completed this answer without Claude."},
                "prompt_eval_count": 11,
                "eval_count": 8,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def fake_ollama() -> tuple[str, list[dict]]:
    _OllamaHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _OllamaHandler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run_zero_claude_hook(
    prompt: str,
    home_dir: Path,
    *,
    extra_payload: dict | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict | None:
    router_dir = home_dir / ".llm-router"
    router_dir.mkdir(exist_ok=True)
    (router_dir / "routing.yaml").write_text("enforce: smart\nmode: zero_claude\n")

    payload = {"prompt": prompt, "session_id": "zero-claude-scenario"}
    if extra_payload:
        payload.update(extra_payload)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home_dir),
            "PYTHONPATH": str(ROOT / "src"),
            "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
            "OLLAMA_BUDGET_MODELS": "scenario-model",
            "OPENAI_API_KEY": "",
            "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "",
        }
    )
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def test_simple_prompt_completes_via_external_direct_execution(
    tmp_path: Path, fake_ollama: tuple[str, list[dict]]
) -> None:
    endpoint, requests = fake_ollama
    out = _run_zero_claude_hook(
        "What is the quick definition of a REST API?",
        tmp_path,
        extra_env={"LLM_ROUTER_OLLAMA_URL": endpoint},
    )

    assert out is not None
    # In echo mode: decision=approve with contextForAgent or additionalContext
    # In block mode: decision=block with reason containing the response
    if out.get("decision") == "approve":
        hook_out = out.get("hookSpecificOutput", {})
        ctx = hook_out.get("contextForAgent", "") or hook_out.get("additionalContext", "")
        assert "An external provider completed this answer without Claude." in ctx
        assert "ZERO_CLAUDE BLOCKED" not in ctx
    else:
        assert out["decision"] == "block"
        assert "An external provider completed this answer without Claude." in out["reason"]
        assert "ZERO_CLAUDE BLOCKED" not in out["reason"]
    assert requests


def test_tool_task_fails_closed_when_external_agent_is_unavailable(tmp_path: Path) -> None:
    out = _run_zero_claude_hook(
        "Fix the bug in src/router.py and run its tests.",
        tmp_path,
        extra_env={"LLM_ROUTER_OLLAMA_URL": "http://127.0.0.1:1"},
    )

    assert out is not None
    assert out["decision"] == "block"
    assert "ZERO_CLAUDE BLOCKED" in out["reason"]
    assert "Claude was not invoked" in out["reason"]
    assert "hookSpecificOutput" not in out


def test_direct_failure_does_not_emit_a_native_route_instruction(tmp_path: Path) -> None:
    out = _run_zero_claude_hook(
        "What is the quick definition of a REST API?",
        tmp_path,
        extra_env={"LLM_ROUTER_OLLAMA_URL": "http://127.0.0.1:1"},
    )

    assert out is not None
    assert out["decision"] == "block"
    assert "ZERO_CLAUDE BLOCKED" in out["reason"]
    assert "MANDATORY ROUTE" not in out["reason"]
    assert "hookSpecificOutput" not in out


def test_native_mcp_tool_request_blocks_before_host_execution(tmp_path: Path) -> None:
    out = _run_zero_claude_hook(
        "List my open GitHub pull requests.",
        tmp_path,
        extra_payload={"tools": [{"name": "mcp__github__list_pull_requests"}]},
    )

    assert out is not None
    assert out["decision"] == "block"
    assert "requires native host tool execution" in out["reason"]
    assert "Claude was not invoked" in out["reason"]
