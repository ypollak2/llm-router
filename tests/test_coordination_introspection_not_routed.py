"""Coordination and introspection prompts are also answered by Claude directly.

Owner decision 2026-10-02, extending the Q&A decision in
``tests/test_qa_not_routed.py`` (2026-10-01) to the two purely conversational
``TaskType`` values ``coordinate`` and ``introspect``:

* ``coordinate`` (auto-route.py's ``_is_coordination_task`` fast path) is
  advisory-only multi-agent orchestration — a stateless routed model has no
  subagents to report on.
* ``introspect`` (``_is_introspection_task``) asks about THIS LLM Router
  install's own local state (routing decisions, hooks, ``~/.llm-router``
  files) — no routed model has access to that data.

enforce-route.py already exits unconditionally the moment a pending route's
``task_type`` is ``"coordinate"`` or ``"introspect"`` (see its own
``pending.get("task_type") == "introspect"`` / ``== "coordinate"`` early
exits) — no tool was ever actually held for either, in any mode. What was
wrong was auto-route.py's banner: under global ``hard`` enforce it still
showed the "⚡ ROUTE DIRECTIVE — HARD ENFORCEMENT ... holds tools" text for a
task enforce-route.py would never hold — the exact banner-honesty defect
``test_qa_not_routed.py`` exists to prevent for Q&A. This file pins the same
invariants for ``coordinate``/``introspect``:

* no route directive in the injected context,
* no pending-route state, and
* no draft.

NOT covered here, and deliberately so: the separate, older ``"coordination"``
(no trailing "e") heuristic bucket scored by ``score_categories`` /
``VALID_CATEGORIES``. That bucket stays enforced on purpose — see
``test_enf_coordination_bash_names_llm_act.py`` and the comment block in
enforce-route.py above its (intentionally absent) exemption for it.

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

COORDINATE_PROMPT = "dispatch specialists for research, coding, and review"
INTROSPECT_PROMPT = "tally today's routes"
CODE_PROMPT = "Write a Python function that parses ISO 8601 dates and returns a datetime"
CONVERSATIONAL_TYPES = ("coordinate", "introspect")
QA_TYPES = ("query", "research", "generate", "analyze")
SID = "sess-noconv-9k1"

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
    ar = _load("auto_route_noconv", "auto-route.py")
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


def _tool_call(monkeypatch, tmp_path, mode, tool, command="", **extra_env):
    """Run enforce-route.py main() for one tool call. Returns (exit_code, stdout)."""
    er = _load("enforce_route_noconv", "enforce-route.py")
    _env(monkeypatch, tmp_path, mode, **extra_env)
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
@pytest.mark.parametrize("prompt", [COORDINATE_PROMPT, INTROSPECT_PROMPT],
                         ids=["coordinate", "introspect"])
def test_conversational_prompt_gets_no_route_directive_pending_state_or_draft(
        monkeypatch, tmp_path, mode, prompt):
    out, decisions, drafted, _ = _run_prompt(monkeypatch, tmp_path, prompt, mode)

    # The prompt really was classified as coordinate/introspect (otherwise
    # this test proves nothing).
    assert decisions and decisions[0]["task_type"] in CONVERSATIONAL_TYPES, decisions
    ctx = _context(out)
    assert ctx, "a non-directive note is expected, not silence"
    assert not any(w in ctx for w in _ROUTE_WORDS), ctx
    assert _pending(tmp_path) == []
    assert drafted == []


@pytest.mark.parametrize("mode", ["smart", "hard"])
@pytest.mark.parametrize("prompt", [COORDINATE_PROMPT, INTROSPECT_PROMPT],
                         ids=["coordinate", "introspect"])
@pytest.mark.parametrize("tool,command", [("Edit", ""), ("Write", ""), ("Bash", "pytest -q"),
                                           ("Bash", "git push origin main")])
def test_no_tool_is_held_after_a_conversational_prompt_under_smart_and_hard(
        monkeypatch, tmp_path, mode, prompt, tool, command):
    _run_prompt(monkeypatch, tmp_path, prompt, mode)
    code, out = _tool_call(monkeypatch, tmp_path, mode, tool, command)
    assert code == 0
    assert "deny" not in out and "block" not in out.lower(), out


@pytest.mark.parametrize("mode", ["smart", "hard"])
def test_a_code_prompt_still_holds_edits_under_smart_and_hard(monkeypatch, tmp_path, mode):
    """Control: the coordinate/introspect quieting must not loosen code-task behaviour."""
    out, decisions, _, _ = _run_prompt(monkeypatch, tmp_path, CODE_PROMPT, mode)
    assert decisions[0]["task_type"] not in CONVERSATIONAL_TYPES + QA_TYPES, decisions
    assert "HARD ENFORCEMENT" in _context(out) or "SUGGESTED" in _context(out), _context(out)
    assert len(_pending(tmp_path)) == 1
    code, out = _tool_call(monkeypatch, tmp_path, mode, "Edit")
    assert code != 0 or "deny" in out or "block" in out.lower(), (code, out)


@pytest.mark.parametrize("mode", ["smart", "hard"])
@pytest.mark.parametrize("prompt", [COORDINATE_PROMPT, INTROSPECT_PROMPT],
                         ids=["coordinate", "introspect"])
def test_qa_routing_env_restores_the_previous_behaviour(monkeypatch, tmp_path, mode, prompt):
    out, decisions, _, _ = _run_prompt(monkeypatch, tmp_path, prompt, mode,
                                       LLM_ROUTER_QA_ROUTING="on")
    assert decisions[0]["task_type"] in CONVERSATIONAL_TYPES
    if mode == "hard":
        assert "HARD ENFORCEMENT" in _context(out)
        assert len(_pending(tmp_path)) == 1
    else:  # smart: coordinate/introspect were never force-hard, only quieted
        assert "SUGGESTED" in _context(out)
        assert len(_pending(tmp_path)) == 1


@pytest.mark.parametrize("prompt", [COORDINATE_PROMPT, INTROSPECT_PROMPT],
                         ids=["coordinate", "introspect"])
def test_routing_decision_is_still_logged(monkeypatch, tmp_path, prompt):
    """Coverage / telemetry is not part of what was switched off."""
    _, decisions, _, _ = _run_prompt(monkeypatch, tmp_path, prompt, "smart")
    assert len(decisions) == 1
    assert decisions[0]["task_type"] in CONVERSATIONAL_TYPES


# ── the older "coordination" heuristic bucket: conversational vs executable ──
#
# Live 2026-10-02: a planning prompt scored the "coordination" bucket from
# topic/format words alone ("tests", "now") and held Bash. The bucket still routes
# EXECUTABLE git/deploy asks (test_enf_coordination_bash_names_llm_act.py); only a
# prompt that names no concrete command is answered directly.

CONVERSATIONAL_COORDINATION_PROMPTS = [
    "If the OKF works now, we need to rerun the tests to understand how many prompts "
    "are routed and how the local models quality now",
    "we need to plan the version bump and the setup, what comes first?",
    "should we push now?",
    "when do we deploy?",
    "is the test suite green now?",
]
EXECUTABLE_COORDINATION_PROMPTS = [
    "push to main",
    "commit and push",
    "git push origin main",
    "deploy to production now",
    "merge the PR",
    "Run the test suite and commit the passing changes.",
]


@pytest.mark.parametrize("mode", ["smart", "hard"])
@pytest.mark.parametrize("prompt", CONVERSATIONAL_COORDINATION_PROMPTS)
def test_conversational_coordination_prompt_gets_no_directive_and_no_hold(
        monkeypatch, tmp_path, mode, prompt):
    out, decisions, drafted, _ = _run_prompt(monkeypatch, tmp_path, prompt, mode)
    # It really is the older "coordination" bucket (not the "coordinate" fast path).
    assert decisions[0]["task_type"] == "coordination", decisions
    ctx = _context(out)
    assert ctx, "a non-directive note is expected, not silence"
    assert not any(w in ctx for w in _ROUTE_WORDS), ctx
    assert _pending(tmp_path) == [] and drafted == []
    for tool, command in (("Bash", "pytest -q"), ("Edit", "")):
        code, tool_out = _tool_call(monkeypatch, tmp_path, mode, tool, command)
        assert code == 0 and "deny" not in tool_out and "block" not in tool_out.lower(), tool_out


@pytest.mark.parametrize("mode", ["smart", "hard"])
@pytest.mark.parametrize("prompt", EXECUTABLE_COORDINATION_PROMPTS)
def test_executable_coordination_prompt_keeps_its_directive_and_pending_route(
        monkeypatch, tmp_path, mode, prompt):
    out, decisions, _, _ = _run_prompt(monkeypatch, tmp_path, prompt, mode)
    assert decisions[0]["task_type"] == "coordination", decisions
    assert any(w in _context(out) for w in ("ROUTE", "SUGGESTED")), _context(out)
    assert len(_pending(tmp_path)) == 1


@pytest.mark.parametrize("mode", ["smart", "hard"])
def test_conversational_coordination_qa_routing_env_restores_the_directive(
        monkeypatch, tmp_path, mode):
    out, decisions, _, _ = _run_prompt(
        monkeypatch, tmp_path, CONVERSATIONAL_COORDINATION_PROMPTS[0], mode,
        LLM_ROUTER_QA_ROUTING="on")
    assert decisions[0]["task_type"] == "coordination"
    assert any(w in _context(out) for w in ("ROUTE", "SUGGESTED")), _context(out)
    assert len(_pending(tmp_path)) == 1


# A plan opener ("we need to ...") in front of a concrete command is still an ask
# to run it, so the precedence is: pure question -> conversational; otherwise any
# executable signal (intent verb / shell tool) -> executable; plan opener only
# conversational when nothing executable is named.
PLAN_OPENER_EXECUTABLE_PROMPTS = [
    "we have to run git push now",
    "we need to deploy this now",
    "we want to release this now",
    "we need to merge this PR before EOD",
    "we need to pytest -q now",
]


@pytest.mark.parametrize("prompt", PLAN_OPENER_EXECUTABLE_PROMPTS)
def test_plan_opener_before_a_command_is_executable_not_conversational(prompt):
    ar = _load("auto_route_noconv", "auto-route.py")
    assert ar._is_conversational_coordination(prompt) is False


@pytest.mark.parametrize("prompt", [
    "If the OKF works now, we need to rerun the tests to understand how many prompts "
    "are routed and how the local models quality now",
    "we need to plan the version bump and the setup, what comes first?",
    "should we push now?",
    "we need to run the tests again to see where the pipeline stands",
])
def test_plan_opener_without_a_command_is_still_conversational(prompt):
    """`rerun`/`run` are not in the bucket's intent layer, and "run" only counts with a
    shell-tool target (`run git push`); so the owner's prompt stays conversational."""
    ar = _load("auto_route_noconv", "auto-route.py")
    assert ar._is_conversational_coordination(prompt) is True


@pytest.mark.parametrize("prompt", [
    "we have to run git push now", "we want to release this now", "we need to pytest -q now"])
def test_plan_opener_before_a_command_keeps_its_pending_route(monkeypatch, tmp_path, prompt):
    """Same pending route (so the same hold semantics) as before the quieting."""
    _, decisions, _, _ = _run_prompt(monkeypatch, tmp_path, prompt, "hard",
                                     LLM_ROUTER_DELEGATE="on")
    assert decisions[0]["task_type"] == "coordination", decisions
    assert len(_pending(tmp_path)) == 1


def test_plan_opener_before_git_push_keeps_the_llm_act_redirect(monkeypatch, tmp_path):
    """hard + LLM_ROUTER_DELEGATE=on: Bash is held and the block names llm_act."""
    _run_prompt(monkeypatch, tmp_path, "we have to run git push now", "hard",
                LLM_ROUTER_DELEGATE="on")
    _, tool_out = _tool_call(monkeypatch, tmp_path, "hard", "Bash",
                             "pytest -q && git commit -am done",
                             LLM_ROUTER_DELEGATE="on", LLM_ROUTER_SLIM="consolidated")
    assert tool_out.strip(), "the Bash command must be held, not exempted to native"
    verdict = json.loads(tool_out)
    assert verdict["decision"] == "block"
    assert "llm_act" in verdict["reason"], verdict["reason"]
