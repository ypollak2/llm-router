"""Development Q&A is answered by Claude directly; local edits and telemetry stay.

Owner decision 2026-10-01. Evidence (``~/.rsi/research/routing-experiment-2026-10-01``,
hook-routed prompts): local-model answers to Q&A were acceptable 3-17% of the
time against 60-100% for Sonnet, and 0 of 1,217 drafts were used. So the
UserPromptSubmit hook must not route a Q&A task, in ANY enforce mode:

* no route directive in the injected context,
* no pending-route state (so the PreToolUse hook has nothing to hold a tool
  against), and
* no draft.

What must NOT change: the routing decision is still logged (coverage /
telemetry), ``zero_claude_edit`` still serves bounded edits (it runs before the
mode is even resolved), and code tasks keep their per-mode behaviour.

Every test drives the real hooks' ``main()``; none reads source text.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks"

QA_PROMPT = "What does Python's functools.lru_cache decorator actually do under the hood?"
# Refers to "this repo", so it takes the context-dependent branch that used to
# emit its own "route WITH context" directive regardless of mode.
QA_CONTEXT_PROMPT = "Why does the auth middleware in this repo reject valid tokens?"
CODE_PROMPT = "Write a Python function that parses ISO 8601 dates and returns a datetime"
EDIT_PROMPT = (
    "Fix the clamp function in mathutils.py. It returns wrong values because "
    "hi and lo are swapped in the min/max nesting."
)
QA_TYPES = ("query", "research", "generate", "analyze")
SID = "sess-noqa-7q2"

_ROUTE_WORDS = ("llm(", "ROUTE", "SUGGESTED", "DIRECTIVE", "CONTEXT PROMPT", "route WITH")


def _load(name: str, filename: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, HOOKS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _env(monkeypatch, tmp_path, mode, **extra):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    (tmp_path / ".llm-router").mkdir(exist_ok=True)
    base = {
        "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
        "LLM_ROUTER_DIRECT_EXECUTION": "1",
        "LLM_ROUTER_ENFORCE": mode,
        "LLM_ROUTER_ZERO_CLAUDE": "off",
        "LLM_ROUTER_ZERO_CLAUDE_SCOPE": "",
        "LLM_ROUTER_QA_ROUTING": "",
    }
    base.update(extra)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def _run_prompt(monkeypatch, tmp_path, prompt, mode, **extra_env):
    """Run auto-route.py main() for one prompt. Returns (stdout, decisions, drafted, log)."""
    ar = _load("auto_route_noqa", "auto-route.py")
    _env(monkeypatch, tmp_path, mode, **extra_env)
    monkeypatch.setattr(ar, "_router_dir", lambda: tmp_path / ".llm-router", raising=False)
    decisions: list[dict] = []
    monkeypatch.setattr(ar, "log_routing_decision", lambda **kw: decisions.append(kw), raising=False)
    import llm_router.hooks.chain_builder as chain_builder
    import llm_router.hooks.direct_executor as de
    model = de.ModelSpec(provider="ollama", model="fake-model")
    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 10.0))
    monkeypatch.setattr(chain_builder, "build_chain", lambda c, z, t: [model])
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: False)
    drafted: list = []
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: drafted.append(1))
    monkeypatch.setattr(de, "execute_agent", lambda *a, **k: drafted.append(1))
    log: list[str] = []
    monkeypatch.setattr(ar, "_debug_log", log.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "session_id": SID})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        ar.main()
    except SystemExit as e:
        assert e.code in (0, None)
    return out.getvalue(), decisions, drafted, log


def _context(stdout: str) -> str:
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"] if stdout.strip() else ""


def _pending(tmp_path):
    return sorted((tmp_path / ".llm-router").glob("pending_route_*.json"))


def _tool_call(monkeypatch, tmp_path, mode, tool, command=""):
    """Run enforce-route.py main() for one tool call. Returns (exit_code, stdout)."""
    er = _load("enforce_route_noqa", "enforce-route.py")
    _env(monkeypatch, tmp_path, mode)
    payload = {"session_id": SID, "tool_name": tool,
               "tool_input": {"command": command, "file_path": str(tmp_path / "x.py")}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = 0
    try:
        er.main()
    except SystemExit as e:
        code = e.code or 0
    return code, out.getvalue()


@pytest.mark.parametrize("mode", ["smart", "hard", "advise", "suggest", "soft", "shadow", "off"])
@pytest.mark.parametrize("prompt", [QA_PROMPT, QA_CONTEXT_PROMPT], ids=["plain", "context-dependent"])
def test_qa_prompt_gets_no_route_directive_pending_state_or_draft(monkeypatch, tmp_path, mode, prompt):
    out, decisions, drafted, log = _run_prompt(monkeypatch, tmp_path, prompt, mode)

    # The prompt really was classified as Q&A (otherwise this proves nothing).
    assert decisions and decisions[0]["task_type"] in QA_TYPES, decisions
    ctx = _context(out)
    assert ctx, "a non-directive note is expected, not silence"
    assert not any(w in ctx for w in _ROUTE_WORDS), ctx
    assert _pending(tmp_path) == []
    assert drafted == []
    outcomes = [line for line in log
                if any(t in line for t in ("DIRECT:", "DIRECT SKIP:", "BYPASS", "CONTINUATION"))]
    assert len(outcomes) == 1, outcomes


@pytest.mark.parametrize("tool,command", [("Edit", ""), ("Write", ""), ("Bash", "pytest -q"),
                                           ("Bash", "git push origin main")])
def test_no_tool_is_held_after_a_qa_prompt_under_smart(monkeypatch, tmp_path, tool, command):
    _run_prompt(monkeypatch, tmp_path, QA_PROMPT, "smart")
    code, out = _tool_call(monkeypatch, tmp_path, "smart", tool, command)
    assert code == 0
    assert "deny" not in out and "block" not in out.lower(), out


def test_a_code_prompt_still_routes_and_still_holds_edits_under_smart(monkeypatch, tmp_path):
    """Control: the Q&A change must not have loosened code-task behaviour."""
    out, decisions, _, _ = _run_prompt(monkeypatch, tmp_path, CODE_PROMPT, "smart")
    assert decisions[0]["task_type"] not in QA_TYPES, decisions
    assert "SUGGESTED" in _context(out)
    assert len(_pending(tmp_path)) == 1
    code, out = _tool_call(monkeypatch, tmp_path, "smart", "Edit")
    assert code != 0 or "deny" in out or "block" in out.lower(), (code, out)


def test_qa_routing_env_restores_the_previous_behaviour(monkeypatch, tmp_path):
    out, decisions, _, _ = _run_prompt(monkeypatch, tmp_path, QA_PROMPT, "smart",
                                       LLM_ROUTER_QA_ROUTING="on")
    assert decisions[0]["task_type"] in QA_TYPES
    assert "HARD ENFORCEMENT" in _context(out)
    assert len(_pending(tmp_path)) == 1


def test_routing_decision_is_still_logged_for_qa(monkeypatch, tmp_path):
    """Coverage / telemetry is not part of what was switched off."""
    _, decisions, _, _ = _run_prompt(monkeypatch, tmp_path, QA_PROMPT, "smart")
    assert len(decisions) == 1
    assert decisions[0]["task_type"] in QA_TYPES


@pytest.mark.parametrize("mode", ["smart", "advise", "shadow", "off"])
def test_zero_claude_edit_still_serves_bounded_edits(monkeypatch, tmp_path, mode):
    """zero_claude_edit runs before the mode is resolved; Q&A quieting must not reach it."""
    from llm_router import zero_claude_edit as zce
    seen: list[str] = []

    def fake_maybe_replace(prompt, cwd, deadline_s):
        seen.append(prompt)
        return zce.ScopedEditOutcome(action="block", log_reason="ZERO_CLAUDE_EDIT APPLIED: test",
                                     message="edit applied by local model", applied=True)

    monkeypatch.setattr(zce, "maybe_replace", fake_maybe_replace)
    out, _, _, _ = _run_prompt(monkeypatch, tmp_path, EDIT_PROMPT, mode,
                               LLM_ROUTER_ZERO_CLAUDE_SCOPE="edit")
    assert seen == [EDIT_PROMPT]
    assert json.loads(out) == {"decision": "block", "reason": "edit applied by local model"}
