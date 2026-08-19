"""B2 — enforced operational→delegate redirect in enforce-route.py.

An operational prompt (change verb + verification demand) can't be done by a
stateless completion model, so enforcement redirects the demanded tool to
``llm_delegate`` (a real tool loop) instead of soft-exempting native tools.
Rails proven here: any llm_* call still clears the lock (never a trap), a kill
switch disables it, and non-operational prompts are untouched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENFORCE_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"

_OP_PROMPT = "Fix the failing test in parser.py and make it pass."          # operational
_NONOP_PROMPT = "Write a function that adds two numbers."                    # codegen, no verify


def _run_hook(payload: dict, *, home: Path, extra_env: dict[str, str] | None = None):
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ENFORCE_ROUTE_HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def _write_pending(home: Path, session_id: str, **overrides) -> None:
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "expected_tool": "llm_code", "task_type": "code", "complexity": "moderate",
        "method": "heuristic", "issued_at": time.time(), "turn_id": 999,
        "session_id": session_id,
    }
    data.update(overrides)
    (router_dir / f"pending_route_{session_id}.json").write_text(json.dumps(data))


def test_operational_prompt_redirects_to_delegate(tmp_path):
    """With LLM_ROUTER_DELEGATE=on: operational prompt + hard mode + a write Bash →
    blocked, demanding llm_delegate (and NOT soft-exempted despite the file ref)."""
    sid = "sess-op-1"
    _write_pending(tmp_path, sid, original_prompt=_OP_PROMPT)
    result = _run_hook(
        {"session_id": sid, "tool_name": "Bash",
         "tool_input": {"command": "python -m pytest -q"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "on"},
    )
    assert json.loads(result.stdout)["decision"] == "block"
    assert "llm_act" in result.stdout          # consolidated default → door name


def test_delegate_route_on_by_default(tmp_path):
    """Default (no LLM_ROUTER_DELEGATE): the operational→delegate redirect now fires —
    it's ON by default since the agentic executor is sandboxed (P1). A moderate+
    operational prompt is redirected to llm_delegate. (LLM_ROUTER_DELEGATE=off opts out.)"""
    sid = "sess-op-default-on"
    _write_pending(tmp_path, sid, original_prompt=_OP_PROMPT)  # complexity defaults to moderate
    result = _run_hook(
        {"session_id": sid, "tool_name": "Bash",
         "tool_input": {"command": "python -m pytest -q"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},  # no LLM_ROUTER_DELEGATE → default ON
    )
    assert "llm_act" in result.stdout          # consolidated default → door name


def test_llm_delegate_call_clears_the_lock(tmp_path):
    sid = "sess-op-2"
    _write_pending(tmp_path, sid, original_prompt=_OP_PROMPT)
    result = _run_hook(
        {"session_id": sid, "tool_name": "mcp__llm_router__llm_delegate",
         "tool_input": {"task": _OP_PROMPT}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_non_operational_prompt_is_not_redirected(tmp_path):
    """A codegen prompt with no verification demand keeps the baseline llm_code
    route — the delegate redirect must not over-fire."""
    sid = "sess-nonop"
    _write_pending(tmp_path, sid, original_prompt=_NONOP_PROMPT)
    result = _run_hook(
        {"session_id": sid, "tool_name": "Bash",
         "tool_input": {"command": "python -m pytest -q"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    assert json.loads(result.stdout)["decision"] == "block"
    assert "llm_delegate" not in result.stdout          # still llm_code


def test_kill_switch_disables_redirect(tmp_path):
    """LLM_ROUTER_DELEGATE=off falls back to the baseline route, no delegation."""
    sid = "sess-killswitch"
    _write_pending(tmp_path, sid, original_prompt=_OP_PROMPT)
    result = _run_hook(
        {"session_id": sid, "tool_name": "Bash",
         "tool_input": {"command": "python -m pytest -q"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "off"},
    )
    assert "llm_delegate" not in result.stdout


def test_delegate_route_is_logged_as_security_event(tmp_path):
    sid = "sess-op-log"
    _write_pending(tmp_path, sid, original_prompt=_OP_PROMPT)
    _run_hook(
        {"session_id": sid, "tool_name": "Bash",
         "tool_input": {"command": "python -m pytest -q"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "on"},
    )
    log = (tmp_path / ".llm-router" / "enforcement.log")
    assert log.exists() and "DELEGATE_ROUTE" in log.read_text(encoding="utf-8")


def test_simple_operational_does_not_delegate_r2_floor(tmp_path):
    """R2 cost floor (P2-core): a SIMPLE operational task keeps its completion route
    even with LLM_ROUTER_DELEGATE=on — a one-line fix isn't worth a multi-step agentic
    loop (which costs more than a single completion)."""
    sid = "sess-op-simple"
    _write_pending(tmp_path, sid, original_prompt=_OP_PROMPT, complexity="simple")
    result = _run_hook(
        {"session_id": sid, "tool_name": "Bash",
         "tool_input": {"command": "python -m pytest -q"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "on"},
    )
    assert "llm_delegate" not in result.stdout          # below the floor → not delegated


def test_moderate_operational_delegates_above_floor(tmp_path):
    """Above the R2 floor: moderate operational + delegate on → redirects to llm_delegate."""
    sid = "sess-op-moderate"
    _write_pending(tmp_path, sid, original_prompt=_OP_PROMPT, complexity="moderate")
    result = _run_hook(
        {"session_id": sid, "tool_name": "Bash",
         "tool_input": {"command": "python -m pytest -q"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "on"},
    )
    assert "llm_act" in result.stdout          # consolidated default → door name
