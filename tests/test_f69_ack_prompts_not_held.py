"""F69: short acknowledgement / owner-action-report prompts must not inherit a
routable class or hold Bash in smart mode.

Observed 2026-10-18 (enforce=smart, after F59): "Yeah, I reconnected the mcp" was
classified analyze/moderate via context-inherit and Bash was held (violation 1/4).
Fix: ``_is_acknowledgement`` (hooks/auto-route.py) feeds ``_is_coordination_task``, so
the existing F59 inherit-skip and smart-mode coordination exemption apply.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENFORCE = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"
AUTO = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"

ACK_PROMPTS = [
    "ok", "done", "Yeah, I reconnected the mcp", "yeah I did it", "I ran the command",
    "copied the hooks", "yes, go ahead", "thanks",
]
FOLLOWUP_QUESTIONS = ["why?", "and the tests?", "what about X", "why did it fail",
                      "reconnected the mcp, why does it fail"]
OPS_BASH = "launchctl kickstart -k gui/501/com.x.router"


def _env(home: Path, **extra) -> dict:
    e = {k: v for k, v in os.environ.items()
         if k not in ("LLM_ROUTER_ENFORCE", "LLM_ROUTER_SLIM", "LLM_ROUTER_DELEGATE")}
    e.update(HOME=str(home), LLM_ROUTER_HOME=str(home / ".llm-router"),
             LLM_ROUTER_DISABLE_LLM_CLASSIFIERS="1", LLM_ROUTER_DIRECT_EXECUTION="0",
             OPENAI_API_KEY="", GEMINI_API_KEY="", GOOGLE_API_KEY="")
    e.update(extra)
    return e


def _turn(prompt, sid, home, **extra):
    return subprocess.run([sys.executable, str(AUTO)],
                          input=json.dumps({"prompt": prompt, "session_id": sid}),
                          capture_output=True, text=True, env=_env(home, **extra))


def _bash(home, sid, mode):
    return subprocess.run(
        [sys.executable, str(ENFORCE)],
        input=json.dumps({"session_id": sid, "tool_name": "Bash",
                          "tool_input": {"command": OPS_BASH}}),
        capture_output=True, text=True,
        env=_env(home, LLM_ROUTER_ENFORCE=mode, LLM_ROUTER_DELEGATE="on",
                 LLM_ROUTER_SLIM="consolidated"))


def _held(r) -> bool:
    return bool(r.stdout.strip()) and json.loads(r.stdout).get("decision") == "block"


def _seed_qa_turn(home, sid):
    (home / ".llm-router").mkdir(parents=True, exist_ok=True)
    _turn("explain the difference between a mutex and a semaphore in detail", sid, home)


@pytest.fixture(scope="module")
def auto_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("auto_route_f69", AUTO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("prompt", ACK_PROMPTS)
def test_ack_recognised(auto_mod, prompt):
    assert auto_mod._is_acknowledgement(prompt)
    assert auto_mod._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", FOLLOWUP_QUESTIONS + [
    "ok now refactor the auth module", "fix the bug", "explain how a hash map works"])
def test_non_ack_not_recognised(auto_mod, prompt):
    assert not auto_mod._is_acknowledgement(prompt)


def _route_line(out) -> str:
    return next((ln for ln in (out.stdout + out.stderr).splitlines() if "routed" in ln), "")


def _pending_from(home, sid, line):
    """Seed the pending route the way the hook records it: class taken from the
    route line the hook printed (so the enforce half judges the real classification)."""
    import re
    m = re.search(r"-> ?(\w+)/(\w+)|\u2192 (\w+)/(\w+)", line)
    if not m:  # no route line -> the hook issued no route, so nothing is pending
        return
    kind, cx = m.group(1) or m.group(3), m.group(2) or m.group(4)
    if kind == "coordinate":
        kind = "coordination"
    d = home / ".llm-router"
    (d / f"pending_route_{sid}.json").write_text(json.dumps({
        "expected_tool": "llm", "task_type": kind, "complexity": cx,
        "method": "heuristic", "issued_at": time.time(), "turn_id": 1,
        "session_id": sid, "original_prompt": "x"}))


@pytest.mark.parametrize("prompt", ACK_PROMPTS)
def test_ack_does_not_inherit_routable_class_and_smart_bash_free(tmp_path, prompt):
    _seed_qa_turn(tmp_path, "a1")
    out = _turn(prompt, "a1", tmp_path)
    line = _route_line(out)
    assert "context-inherit" not in line
    assert "analyze/" not in line and "query/" not in line
    _pending_from(tmp_path, "a1", line)
    assert not _held(_bash(tmp_path, "a1", "smart"))


def test_followup_question_inherits_as_before(tmp_path):
    """Negative control: a genuine short follow-up still context-inherits the Q&A class."""
    _seed_qa_turn(tmp_path, "q1")
    line = _route_line(_turn("why?", "q1", tmp_path))
    assert "context-inherit" in line and "analyze/" in line


def test_hard_mode_still_holds_bash_for_pending_qa(tmp_path):
    """Hard-mode semantics unchanged: a pending Q&A route still holds Bash."""
    d = tmp_path / ".llm-router"
    d.mkdir(parents=True)
    (d / "pending_route_h1.json").write_text(json.dumps({
        "expected_tool": "llm", "task_type": "query", "complexity": "simple",
        "method": "heuristic", "issued_at": time.time(), "turn_id": 1,
        "session_id": "h1", "original_prompt": "what is the capital of France"}))
    assert _held(_bash(tmp_path, "h1", "hard"))


# ── command-shaped prompts (coordinator scope addition) ──────────────────────
CMD_PROMPTS = ["llm-router summary`", "`llm-router summary`", "git status", "gh pr list",
               "$ uv sync --frozen", "pytest -q tests/test_x.py", "ls -a ~/Projects"]
CMD_QUESTIONS = ["why does git rebase fail?", "git rebase fails when I push",
                 "explain how git rebase works", "what does llm-router summary do?",
                 "git push origin main"]  # shared-state commands keep the older executable handling


@pytest.mark.parametrize("prompt", CMD_PROMPTS)
def test_command_shaped_recognised(auto_mod, prompt):
    assert auto_mod._is_command_shaped(prompt)
    assert auto_mod._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", CMD_QUESTIONS)
def test_command_mentioning_questions_not_command_shaped(auto_mod, prompt):
    assert not auto_mod._is_command_shaped(prompt)


@pytest.mark.parametrize("prompt", CMD_PROMPTS)
def test_command_prompt_not_inherited_and_smart_bash_free(tmp_path, prompt):
    _seed_qa_turn(tmp_path, "c1")
    line = _route_line(_turn(prompt, "c1", tmp_path))
    assert "context-inherit" not in line
    assert "analyze/" not in line and "research/" not in line and "query/" not in line
    _pending_from(tmp_path, "c1", line)
    assert not _held(_bash(tmp_path, "c1", "smart"))


def test_question_mentioning_command_still_routes(tmp_path):
    line = _route_line(_turn("why does git rebase fail?", "c2", tmp_path))
    assert "coordination-fast-path" not in line  # unchanged: classified by the normal heuristic
