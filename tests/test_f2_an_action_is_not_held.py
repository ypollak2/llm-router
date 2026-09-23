"""F-2 — a prompt that needs an ACTION is not held by the enforcement hook.

audit/27, Phase 0.5b. `capabilities.detect_capabilities()` is documented as
"the shared predicate for ALL routing / exemption / provisioning / permission
decisions", and `enforce-route.py` had never imported it. Both halves of
capability-awareness existed and nothing joined them.

Measured over n=1594 real prompts (`scripts/measure_capability_mismatch.py`):

    need an ACTION (write / run / git) ....... 337/1594 = 21.1%
    need reads only .......................... 41/1594  =  2.6%

The distinction carries the whole fix. A prompt needing to READ the repo is
routable — the router reads the file and passes it as `context`. A prompt
needing to WRITE, RUN or COMMIT is not: `_call_text` returns plain text and
`gateway._refuse_tools_if_present` returns HTTP 400 for anything with tools.

86 of the action-needing prompts classify as `query` — "commit this", "commit
the symlink fix", "ship llm_local_task and commit" — a QA type, which is where
the hold is strictest and where the local-tool valve is switched off. The
prompts least servable by routing were held hardest.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

HOOK = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "llm_router" / "hooks" / "enforce-route.py"
)


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("enforce_route_f2", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── The predicate ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", [
    "commit this and push",
    "commit the symlink fix and the trace mode",
    "run the test suite",
    "ship llm_local_task and commit the comparison",
])
def test_an_action_is_detected(hook, prompt: str) -> None:
    """Every case here is a REAL prompt from the measured corpus.

    An invented one ("write the results to a file") was in this list first and
    failed — `detect_capabilities` is regex-based with, in its own words,
    "documented brittleness", and does not catch that phrasing. The detector was
    NOT widened to make it pass: tuning a heuristic against a synthetic example
    is how a filter starts agreeing with its author instead of with traffic.
    The miss is recorded in `test_the_detector_misses_phrasings_it_does_not_match`
    below, which is the honest form of the same information.
    """
    assert hook._prompt_needs_an_action(prompt, "query") is True, prompt


def test_the_detector_misses_phrasings_it_does_not_match(hook) -> None:
    """A known, deliberate limitation — pinned so it is not mistaken for a bug.

    `detect_capabilities` is regex-based. These ask for an action and are not
    detected, so they are still held. Widening the regexes against invented
    examples is refused; the case for widening is a MEASUREMENT on real traffic,
    which is what `scripts/measure_capability_mismatch.py` exists to produce.

    If one of these starts being detected, that is fine — delete it from this
    list and say what changed. The test exists so the gap is visible, not so it
    stays open.
    """
    missed = [p for p in (
        "write the results to a file",
        "put this in a new module",
    ) if not hook._prompt_needs_an_action(p, "query")]
    assert missed, (
        "the detector now catches phrasings it used to miss — good. Update this "
        "list and note what changed, rather than deleting the test."
    )


@pytest.mark.parametrize("prompt", [
    "what is a monad?",
    "read src/llm_router/router.py and explain it",
    "explain how the bandit picks a model",
    "",
])
def test_a_read_or_a_question_is_NOT_an_action(hook, prompt: str) -> None:
    """The load-bearing half.

    If reads counted as actions, 2.6% more prompts would stop being held for no
    reason — a read IS routable, by passing the file as `context`. Getting this
    wrong in the permissive direction is how enforcement gets gutted while
    looking like a capability fix.
    """
    assert hook._prompt_needs_an_action(prompt, "query") is False, prompt


def test_a_detector_failure_fails_open(hook, monkeypatch) -> None:
    """A broken detector must never block a tool call."""
    import llm_router.capabilities as caps

    def boom(*a, **k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(caps, "detect_capabilities", boom)
    assert hook._prompt_needs_an_action("commit this", "query") is False


# ── End to end ────────────────────────────────────────────────────────────


def _run_hook(tmp_path, command: str, prompt: str, task_type: str,
              mode: str = "hard", complexity: str = "simple"):
    session = "f2testsession"
    home = tmp_path / "router"
    home.mkdir(parents=True, exist_ok=True)
    (home / f"pending_route_{session}.json").write_text(json.dumps({
        "task_type": task_type, "complexity": complexity,
        "original_prompt": prompt,
        "issued_at": time.time(), "expires_at": time.time() + 600,
        "route_id": "f2", "satisfied": False,
    }))
    env = {**os.environ, "HOME": str(tmp_path), "LLM_ROUTER_HOME": str(home),
           "LLM_ROUTER_ENFORCE": mode, "LLM_ROUTER_BASH_INTERCEPT": "off"}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({
            "session_id": session, "tool_name": "Bash",
            "tool_input": {"command": command},
        }),
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_a_commit_prompt_is_not_held(tmp_path) -> None:
    """The measured case: 86 action-needing prompts classify as `query`."""
    r = _run_hook(tmp_path, "git commit -m x", "commit this and push", "query")
    assert '"deny"' not in r.stdout, (
        "a prompt asking to commit was held, and no routed text model can "
        f"commit.\n{r.stdout[:400]}"
    )


def test_a_question_is_STILL_held(tmp_path) -> None:
    """Anti-vacuity, and the guard against gutting enforcement.

    If this stops being denied, the exemption has widened to everything and the
    test above proves nothing.
    """
    r = _run_hook(tmp_path, "cat src/llm_router/router.py",
                  "what is a monad?", "research")
    assert '"deny"' in r.stdout, (
        "the hook denied nothing — the F-2 exemption has swallowed ordinary "
        f"Q&A routing.\nstdout={r.stdout[:300]} stderr={r.stderr[:300]}"
    )


def test_the_exemption_defers_to_the_tool_capable_redirect(hook) -> None:
    """When `_delegate_redirect_fires`, the work goes to llm_act — a door that
    CAN perform it — which beats letting Claude do it natively.

    Exempting there would silently defeat the redirect. That is the #29
    reasoning, one level up. AST-asserted so a comment cannot satisfy it.
    """
    import ast

    tree = ast.parse(HOOK.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        calls = {
            c.func.id for c in ast.walk(node.test)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        if "_prompt_needs_an_action" not in calls:
            continue
        assert "_delegate_redirect_fires" in calls, (
            "the action exemption does not consult _delegate_redirect_fires, so "
            "it will defeat the redirect that sends this work to llm_act"
        )
        negated = any(
            isinstance(u, ast.UnaryOp) and isinstance(u.op, ast.Not)
            and any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == "_delegate_redirect_fires"
                for c in ast.walk(u)
            )
            for u in ast.walk(node.test)
        )
        assert negated, (
            "_delegate_redirect_fires must be NEGATED — exempt only when the "
            "redirect will NOT fire"
        )
        return
    pytest.fail("no branch calls _prompt_needs_an_action — the fix is dead code")
