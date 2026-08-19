"""P0: context-aware routing — no blind drafts for context-dependent prompts.

Two guarantees:
  1. `_is_context_dependent` catches prompts that reference the user's local
     code / files / history / state (the failure mode that drafted `npm run
     start` for a Python repo), while leaving self-contained prompts draftable.
  2. The hook, for a context-dependent prompt, suppresses the blind DIRECT draft
     and instead emits a CONTEXT-DEPENDENT directive pointing at real context /
     llm_query(context=…).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load_auto_route():
    cached = sys.modules.get("auto_route_ctx_test")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_ctx_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_ctx_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ar():
    return _load_auto_route()


# ── Detector: must catch context-dependent prompts ───────────────────────────
CONTEXT_DEPENDENT = [
    "how do I run this app",            # the npm-for-python case
    "how do I start the server",        # noun not in old list
    "run it",                            # bare deictic pronoun
    "why doesn't it work",              # deictic, no listed noun
    "fix the failing tests",            # adjective between determiner and noun
    "what does this do",                # deictic
    "refactor the parser",              # operational verb
    "explain the build error",          # adjective + noun
    "the app won't start",
    "debug my python script",
    "why is the deployment failing",
    "look at src/main.py",              # file path
    "continue where we left off",       # prior-conversation
    "you said earlier to use redis",    # prior-conversation
    # G-ENF-1 regressions: operational commands on prior/session state that were
    # mis-classified query/simple and hard-blocked Bash this session.
    "yes, check the 5 and stop the rest",  # operational verb + anaphora
    "stop the rest",                       # definite anaphora
    "kill the other worktrees",            # operational verb
    "delete the remaining ones",           # operational verb + anaphora
    "clean up the stray files",            # "clean up" operational verb
    "cancel the running jobs",             # operational verb
]


@pytest.mark.parametrize("prompt", CONTEXT_DEPENDENT)
def test_context_dependent_prompts_detected(ar, prompt):
    assert ar._is_context_dependent(prompt) is True, prompt


# ── Detector: must NOT flag self-contained prompts (drafts still useful) ─────
SELF_CONTAINED = [
    "what is the capital of France",
    "add a function to parse dates",
    "write a haiku about autumn",
    "explain how TCP handshakes work",
    "summarize the theory of relativity",
    "what's the difference between a list and a tuple in Python",
]


@pytest.mark.parametrize("prompt", SELF_CONTAINED)
def test_self_contained_prompts_not_flagged(ar, prompt):
    assert ar._is_context_dependent(prompt) is False, prompt


def test_empty_prompt_is_not_context_dependent(ar):
    assert ar._is_context_dependent("") is False
    assert ar._is_context_dependent(None) is False


def test_long_prompt_with_deictic_not_overcaught(ar):
    # The deictic heuristic only fires for SHORT prompts; a long general-knowledge
    # prompt that merely contains "this" must not be flagged on the pronoun alone.
    long_general = (
        "I am studying for an exam and want a thorough explanation of how "
        "photosynthesis converts light energy, walking through each stage of "
        "this biological process in detail with the relevant chemistry"
    )
    # (No code noun, no operational verb, >12 words → not context-dependent.)
    assert ar._is_context_dependent(long_general) is False


# ── End-to-end: hook suppresses the blind draft + emits the context directive ─
def _run_hook(prompt: str, home: Path, *, direct: bool = True) -> dict | None:
    payload = json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "session_id": "ctx-test",
    })
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    env["LLM_ROUTER_ENFORCE"] = "suggest"
    if not direct:
        # Disable live model drafting so stdout is a clean directive JSON.
        env["LLM_ROUTER_DIRECT_EXECUTION"] = "off"
    (home / ".llm-router").mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(HOOK)], input=payload,
        capture_output=True, text=True, env=env,
    )
    out = result.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def _context_text(out: dict) -> str:
    hso = out.get("hookSpecificOutput", {})
    return hso.get("additionalContext") or hso.get("contextForAgent") or out.get("reason", "")


def test_context_dependent_prompt_gets_no_draft_and_a_context_directive(tmp_path):
    out = _run_hook("how do I run this app", tmp_path)
    assert out is not None
    ctx = _context_text(out)
    # The context-needed directive is present...
    assert "CONTEXT-DEPENDENT PROMPT" in ctx
    # CHZ-SURF-01: the directive must name a tool that is registered under the
    # ACTIVE tier — llm_query(context=…) on a legacy tier, and
    # llm(task="query", context=…) under the consolidated default.
    from llm_router.tool_surface import route_call
    assert route_call("llm_query", "context=…") in ctx
    # ...and no blind draft was relayed.
    assert "UNVERIFIED DRAFT" not in ctx
    assert "ROUTING NOTICE" not in ctx


def test_self_contained_prompt_has_no_context_directive(tmp_path):
    out = _run_hook("what is the capital of France", tmp_path, direct=False)
    # A normal route directive — but NOT the context-needed note.
    if out is not None:
        assert "CONTEXT-DEPENDENT PROMPT" not in _context_text(out)


# ── Part B: context-dependent prompts write NO pending state (no bounce-back) ──
def _pending_path(home: Path) -> Path:
    return home / ".llm-router" / "pending_route_ctx-test.json"


def test_context_dependent_prompt_writes_no_pending_state(tmp_path):
    # The bounce-back fix: a context-dependent prompt must NOT write pending
    # enforcement state, so enforce-route.py cannot block Bash/Edit/Read and the
    # throwaway-llm_query dance never triggers. Handled directly in one pass.
    _run_hook("refactor the parser in src/main.py", tmp_path, direct=False)
    assert not _pending_path(tmp_path).exists(), (
        "context-dependent prompt must not write pending route state"
    )


def test_self_contained_routable_prompt_still_writes_pending_state(tmp_path):
    # Control: a routable, context-FREE prompt still writes pending state, so
    # routing enforcement stays intact for prompts that genuinely can route.
    _run_hook("write a haiku about autumn", tmp_path, direct=False)
    assert _pending_path(tmp_path).exists(), (
        "context-free routable prompt should still write pending state"
    )
