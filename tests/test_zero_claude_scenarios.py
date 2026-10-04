"""End-to-end hook scenarios for strict zero-Claude routing."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Exercises the Q&A routing machinery, which is off by default (LLM_ROUTER_QA_ROUTING).
pytestmark = pytest.mark.usefixtures("qa_routing_on")

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load_auto_route():
    """Import auto-route.py in-process (hyphenated filename, not a package
    module) so the subprocess timeout below can be derived from the hook's
    own real budget rather than guessed. Same pattern as
    tests/test_pressure_override_keys.py / tests/test_auto_route_signals.py."""
    cached = sys.modules.get("auto_route_under_test_zero_claude_scenarios")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "auto_route_under_test_zero_claude_scenarios", HOOK_PATH
    )
    assert spec and spec.loader, f"Could not load spec for {HOOK_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_under_test_zero_claude_scenarios"] = module
    spec.loader.exec_module(module)
    return module


# Root cause of a chronic CI flake (TimeoutExpired on this subprocess call,
# seen on main and on PRs #261/#263 across 2026-10-03/04): the old flat
# timeout=10 had no relationship to how long this hook subprocess is
# actually allowed to run. Under pytest-xdist (`-n auto --dist loadgroup`),
# sibling concurrency/timeout-stress tests sharing the same small CI runner
# core count starve this hook of CPU; in isolation it finishes in well
# under 1s. The hook's OWN documented budget is `_hook_budget_s()`
# (55s default: 5s under the 60s timeout Claude Code registers in
# settings.json) — so the test grants the same budget plus the same 5s
# margin, instead of picking another arbitrary number.
HOOK_SUBPROCESS_TIMEOUT = _load_auto_route()._hook_budget_s() + 5

# pyproject.toml sets a blanket pytest-timeout ceiling of 30s for every test
# in the suite. That is now BELOW our own subprocess timeout above (60s by
# default), so without an override pytest's own watchdog would cut the test
# off at 30s before the subprocess timeout we just derived ever gets a
# chance to matter — silently re-introducing the same flake one layer up.
# Give these specific tests (the ones that exercise the slow DIRECT-success
# path via fake_ollama, as opposed to the dead-port tests that fail in
# milliseconds) the same budget as the subprocess call itself, plus margin
# for interpreter/pytest overhead around it.
TEST_TIMEOUT = HOOK_SUBPROCESS_TIMEOUT + 15


class _OllamaHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_GET(self) -> None:
        # Respond to GET /api/tags (pre-flight health check from ollama_is_alive())
        response = json.dumps({"models": [{"name": "scenario-model:latest"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

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
            "LLM_ROUTER_HOME": str(router_dir),
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
        timeout=HOOK_SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return None
    # E6 fix: debug/tracking lines may precede the JSON payload on stdout.
    # Find the first line that looks like a JSON object and parse that.
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return json.loads(result.stdout)


@pytest.mark.timeout(TEST_TIMEOUT)
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


def _hook_debug_log(home_dir: Path) -> Path:
    # Under pytest the hook writes the .test.log sibling (_debug_log_path).
    return home_dir / ".llm-router" / "auto-route-debug.test.log"


def _replaced_flags(home_dir: Path) -> list[bool]:
    """Per-invocation northstar verdict for the hook's own debug log."""
    from llm_router import northstar as ns
    records = ns._parse_debug_log(_hook_debug_log(home_dir))
    return [ns._invocation_replaced_turn(r["msgs"]) for r in records.values()]


@pytest.mark.timeout(TEST_TIMEOUT)
def test_replaced_turn_is_logged_so_northstar_counts_it_direct(
    tmp_path: Path, fake_ollama: tuple[str, list[dict]]
) -> None:
    """#212: the block that carries the answer must leave the one debug-log
    line northstar keys ``direct`` on; nothing else may."""
    endpoint, _requests = fake_ollama
    out = _run_zero_claude_hook(
        "What is the quick definition of a REST API?",
        tmp_path,
        extra_env={"LLM_ROUTER_OLLAMA_URL": endpoint, "LLM_ROUTER_RENDER_MODE": "block"},
    )
    assert out is not None and out["decision"] == "block"
    assert "An external provider completed this answer without Claude." in out["reason"]
    log = _hook_debug_log(tmp_path).read_text()
    assert "ZERO_CLAUDE REPLACED: render_mode=block" in log
    assert _replaced_flags(tmp_path) == [True]


@pytest.mark.timeout(TEST_TIMEOUT)
def test_echo_draft_is_not_logged_as_replaced(
    tmp_path: Path, fake_ollama: tuple[str, list[dict]]
) -> None:
    endpoint, _requests = fake_ollama
    out = _run_zero_claude_hook(
        "What is the quick definition of a REST API?",
        tmp_path,
        extra_env={"LLM_ROUTER_OLLAMA_URL": endpoint, "LLM_ROUTER_RENDER_MODE": "echo"},
    )
    assert out is not None and out.get("decision") != "block"
    log = _hook_debug_log(tmp_path).read_text()
    assert "DIRECT SUCCESS" in log
    assert "ZERO_CLAUDE REPLACED" not in log
    assert _replaced_flags(tmp_path) == [False]
