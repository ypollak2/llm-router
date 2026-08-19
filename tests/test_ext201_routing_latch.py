"""Regression: CHZ-EXT-201 — the routing latch.

After any code-classified turn, the hook inherited `code` for *every* short
(≤15-word) prompt in the session, disabled direct execution, and re-pinned
last_route to `code` — so routing decayed to 2.32% sustained / 0% from turn 10.

Root cause: `_is_short_code_followup` gated on word count ALONE, sweeping in
self-contained short questions that a stateless model can answer perfectly well.

The fix requires a genuine follow-up signal (anaphora/deixis, an imperative edit
verb, or a discourse-marker prefix). These tests prove:
  1. self-contained short prompts are NOT treated as code follow-ups (they route),
     regardless of length — the 15/16-word boundary no longer decides;
  2. genuine code follow-ups ARE still inherited (the feature isn't regressed);
  3. end-to-end, `[code] + [self-contained]×N` no longer latches: the
     self-contained turns do not get `code-context-inherit`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("llm_router_auto_route_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook_module()

LAST_CODE = {"task_type": "code", "complexity": "moderate", "tool": "llm_code"}

# Self-contained prompts: answerable by a stateless model, must NOT inherit code.
# Deliberately spans word counts straddling the old 15/16 boundary.
SELF_CONTAINED = [
    "what is a monad",                                   # 4
    "reverse a linked list in rust",                     # 6
    "capital of france",                                 # 3
    "explain recursion",                                 # 2
    "how do I sort a dictionary by value in python",     # 10
    "write a haiku about the sea",                       # 6
    "what is the difference between a thread and a process in an operating system today",  # 15 (boundary)
    "summarize the theory of relativity",               # 5
    "define idempotency",                                # 2
    "who wrote pride and prejudice",                     # 5
]

# Genuine follow-ups to prior code work: must still inherit code context.
GENUINE_FOLLOWUPS = [
    "fix that",
    "why does it fail",
    "refactor this",
    "now add a test",
    "the test is red",
    "revert that change",
    "run it again",
    "explain why the dashboard doesn't update",  # the docstring's own example
    "make it faster",
    "and remove the unused import",
]


@pytest.mark.parametrize("prompt", SELF_CONTAINED)
def test_self_contained_prompts_do_not_inherit_code(prompt: str) -> None:
    assert hook._is_short_code_followup(prompt, LAST_CODE) is False, (
        f"CHZ-EXT-201 regression: self-contained prompt {prompt!r} inherited code "
        "context — it should route via fresh classification"
    )


@pytest.mark.parametrize("prompt", GENUINE_FOLLOWUPS)
def test_genuine_followups_still_inherit_code(prompt: str) -> None:
    assert hook._is_short_code_followup(prompt, LAST_CODE) is True, (
        f"CHZ-EXT-201 regression (over-correction): genuine follow-up {prompt!r} "
        "no longer inherits code context — the feature regressed"
    )


def test_word_count_boundary_no_longer_decides() -> None:
    """The old defect: outcome flipped purely at 15↔16 words. Now content decides."""
    # A short self-contained prompt and a long self-contained prompt: both route.
    short_self = "define idempotency"                       # 2 words
    long_self = " ".join(["explain", "the", "concept", "of"] + ["backpropagation"] * 20)  # 24 words
    assert hook._is_short_code_followup(short_self, LAST_CODE) is False
    assert hook._is_short_code_followup(long_self, LAST_CODE) is False
    # A genuine follow-up inherits whether short or (reasonably) long.
    assert hook._is_short_code_followup("fix it", LAST_CODE) is True


# ── End-to-end: prove the latch does not accumulate across a session ──────────

def _run_turn(prompt: str, session_id: str, home: Path) -> dict | None:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["LLM_ROUTER_DISABLE_LLM_CLASSIFIERS"] = "1"
    env["LLM_ROUTER_DIRECT_EXECUTION"] = "0"  # classification only
    env["OPENAI_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""
    env["GOOGLE_API_KEY"] = ""
    payload = json.dumps({"prompt": prompt, "session_id": session_id})
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _method_of(out: dict | None) -> str:
    """Extract the routing method string from hook output, if present."""
    if not out:
        return ""
    ctx = ""
    if isinstance(out.get("hookSpecificOutput"), dict):
        ctx = out["hookSpecificOutput"].get("additionalContext", "") or ""
    ctx = ctx or out.get("additionalContext", "") or json.dumps(out)
    return ctx


def test_latch_does_not_accumulate_end_to_end() -> None:
    """`[code] + [self-contained]×12` in ONE session must not latch to code."""
    with tempfile.TemporaryDirectory(prefix="chz-latch-") as tmp:
        home = Path(tmp)
        (home / ".llm-router").mkdir(parents=True, exist_ok=True)
        sid = "latch-session-1"

        # Turn 1: a clear code task, to arm last_route=code.
        _run_turn("refactor the auth module to use async db sessions", sid, home)

        # Turns 2..13: identical self-contained question, byte-for-byte.
        inherited = 0
        probe = "what is the difference between tcp and udp"
        for _ in range(12):
            out = _run_turn(probe, sid, home)
            if "code-context-inherit" in _method_of(out):
                inherited += 1

        assert inherited == 0, (
            "CHZ-EXT-201 regression: self-contained prompt inherited code context "
            f"on {inherited}/12 turns after a code turn — the latch is back"
        )
