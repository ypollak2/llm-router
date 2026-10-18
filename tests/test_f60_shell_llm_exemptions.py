"""F60 - a shell-driven LLM call is not exempt from a routing hold.

Two pre-existing Bash exemptions let a model call run unrouted:
  1. `python -c 'import openai; ...'` passed because `python` is a local tool.
  2. `llm-router query ...` passed because F-1 exempted the whole router CLI.
Fix: inline python naming a provider SDK/host stays route-eligible, and the
own-CLI exemption is an allowlist of config/admin/diagnostic subcommands.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("enforce_route_f60", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REFUSED_PY = [
    "python -c 'import openai; print(openai.OpenAI().chat.completions.create())'",
    'python3 -c "import anthropic; anthropic.Anthropic().messages.create()"',
    "uv run python -c 'import litellm; litellm.completion()'",
    "python3 -c 'from google import genai; genai.Client()'",
    "python -c 'import google.generativeai as g'",
    "python3 -c 'import ollama; ollama.chat()'",
    "python - <<'EOF'\nimport openai\nEOF",
    "python3 <<EOF\nimport requests\nrequests.post('https://api.openai.com/v1/chat/completions')\nEOF",
    "python -c \"import httpx; httpx.post('https://api.anthropic.com/v1/messages')\"",
    "python3 -c \"import requests; requests.post('http://localhost:11434/api/generate')\"",
]
ALLOWED_PY = [
    "python script.py",
    "python3 -m pytest -q tests/",
    "python -c 'print(1)'",
    "python3 -c \"import json; print(json.dumps({}))\"",
    "python -c 'import requests; requests.get(\"http://localhost:8000/health\")'",
    "git status",
]
REFUSED_CLI = [
    "llm-router run --model m 'task'", "llm-router judge x", "llm-router benchmark",
    "llm-router probe", "llm-router soak", "llm-router calibrate", "llm-router demo",
    "llm-router replay x", "llm-router query 'what is 2+2'", "llm-router ask hi",
    "llm_router route hi", ".venv/bin/llm-router code 'x'", "llm-router research y",
    "llm-router act z", "llm-router delegate z", "llm-router tui", "llm-router serve",
    "llm-router status; llm-router run --model m t",
    "llm-router doctor && llm-router query hi",
    "LLM_ROUTER_HOME=/tmp llm-router generate x",
]
ALLOWED_CLI = [
    "llm-router set-enforce off", "llm-router set-enforce smart", "llm_router doctor",
    "llm-router status", "llm-router summary", "llm-router install", "llm-router uninstall",
    "llm-router provider list", "llm-router discover", "llm-router version",
    "llm-router --help", "llm-router help", ".venv/bin/llm-router gc",
    "cd /tmp && llm-router stats", "llm-router status | head",
    "llm-router doctor && llm-router status",
]


@pytest.mark.parametrize("cmd", REFUSED_PY)
def test_sdk_oneliner_is_not_local_only(hook, cmd):
    assert hook._is_local_only_bash(cmd) is False, cmd
    assert hook._bash_exempt_from_hold("coordination", cmd, False) is False, cmd
    assert hook._smart_ops_bash_exempt("smart", "coordination", cmd) is False, cmd


@pytest.mark.parametrize("cmd", ALLOWED_PY)
def test_ordinary_python_stays_local_only(hook, cmd):
    assert hook._is_local_only_bash(cmd) is True, cmd


@pytest.mark.parametrize("cmd", REFUSED_CLI)
def test_model_calling_router_cli_is_not_exempt(hook, cmd):
    assert hook._is_router_self_command(cmd) is False, cmd
    assert hook._is_local_only_bash(cmd) is False, cmd


@pytest.mark.parametrize("cmd", ALLOWED_CLI)
def test_admin_router_cli_still_exempt(hook, cmd):
    assert hook._is_router_self_command(cmd) is True, cmd
    assert hook._is_local_only_bash(cmd) is True, cmd


def test_non_empty_sets():
    assert len(REFUSED_PY) >= 10 and len(ALLOWED_PY) >= 5
    assert len(REFUSED_CLI) >= 15 and len(ALLOWED_CLI) >= 10


def test_admin_allowlist_names_exist_in_the_real_cli(hook):
    """Anti-drift: every allowlisted word (bar flags/aliases) is a real dispatch branch."""
    src = (ROOT / "src" / "llm_router" / "cli.py").read_text(encoding="utf-8")
    for name in sorted(hook._ROUTER_ADMIN_SUBCOMMANDS - {"discover", "version", "help"}):
        assert f'args[0] == "{name}"' in src, name
    # and no model-calling branch leaked into the allowlist
    for name in ("run", "judge", "benchmark", "probe", "soak", "calibrate", "demo",
                 "replay", "quickstart", "test", "tui", "serve", "gateway", "proxy", "broker"):
        assert name not in hook._ROUTER_ADMIN_SUBCOMMANDS, name


# ── End to end through the hook process (smart and hard, during a hold) ────────

def _run(cmd, home, enforce, task_type):
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    sid = "f60"
    (d / f"pending_route_{sid}.json").write_text(json.dumps({
        "expected_tool": "llm", "task_type": task_type, "complexity": "moderate",
        "method": "heuristic", "issued_at": time.time(), "turn_id": 1,
        "session_id": sid, "original_prompt": "do the thing"}))
    e = {k: v for k, v in os.environ.items()
         if k not in ("LLM_ROUTER_ENFORCE", "LLM_ROUTER_SLIM", "LLM_ROUTER_DELEGATE")}
    e.update(HOME=str(home), LLM_ROUTER_HOME=str(d), LLM_ROUTER_ENFORCE=enforce)
    r = subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True, env=e,
                       input=json.dumps({"session_id": sid, "tool_name": "Bash",
                                         "tool_input": {"command": cmd}}))
    return r.stdout.strip() and json.loads(r.stdout).get("decision") == "block"


@pytest.mark.parametrize("enforce", ["smart", "hard"])
@pytest.mark.parametrize("task", ["coordination", "research"])
def test_e2e_sdk_oneliner_is_held(tmp_path, enforce, task):
    assert _run("python3 -c 'import openai; openai.OpenAI()'", tmp_path, enforce, task)


@pytest.mark.parametrize("enforce", ["smart", "hard"])
def test_e2e_router_query_is_held(tmp_path, enforce):
    assert _run("llm-router query 'what is 2+2'", tmp_path, enforce, "research")


@pytest.mark.parametrize("enforce", ["smart", "hard"])
def test_e2e_admin_cli_never_held_even_for_qa(tmp_path, enforce):
    assert not _run("llm-router set-enforce off", tmp_path, enforce, "research")


@pytest.mark.parametrize("enforce", ["smart", "hard"])
def test_e2e_plain_python_still_exempt_for_non_qa(tmp_path, enforce):
    assert not _run("python3 -c 'print(1)'", tmp_path, enforce, "coordination")
