"""G2 gate — multi-turn / multi-session soak: routing must not decay (CHZ-EXT-201).

The audit's central finding was that external execution decayed to 0% from
per-session turn 10, invisible to single-turn tests. This soak drives the REAL
auto-route hook across several sessions of many turns each — realistic developer
sessions that interleave code edits with self-contained questions — and asserts
that self-contained prompts keep being routed FRESH (never inherit `code`) at
every turn position, including turn 10+. Under the pre-fix latch, self-contained
short prompts inherited `code` from turn 2 onward and the fresh-routing rate
collapsed to ~0.

This is the gate that a single-turn test structurally cannot be: session-age
effects only appear across many turns in one session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"

SESSIONS = 4
TURNS = 15  # > 10 so the historically-dead zone (turn 10+) is exercised

# Self-contained prompts — answerable by a stateless model; must never inherit
# code context regardless of session age. Varied so it's not one memoized string.
SELF_CONTAINED = [
    "what is a monad",
    "how do I sort a dict by value in python",
    "explain the cap theorem",
    "what is the difference between tcp and udp",
    "define idempotency",
    "who wrote the go programming language",
    "what is big-o of binary search",
    "summarize what a bloom filter is",
]

# Code prompts interleaved to keep last_route armed as "code" (the trigger).
CODE = [
    "refactor the auth module to use async db sessions",
    "add a retry decorator to the http client",
    "fix the flaky test in test_scheduler",
    "extract the parser into its own module",
]


def _run_turn(prompt: str, session_id: str, home: Path) -> dict | None:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["LLM_ROUTER_DISABLE_LLM_CLASSIFIERS"] = "1"
    env["LLM_ROUTER_DIRECT_EXECUTION"] = "0"  # classification only (hermetic, no provider)
    env["OPENAI_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""
    env["GOOGLE_API_KEY"] = ""
    payload = json.dumps({"prompt": prompt, "session_id": session_id})
    r = subprocess.run(
        [sys.executable, str(HOOK_PATH)], input=payload,
        capture_output=True, text=True, env=env, timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _inherited_code(out: dict | None) -> bool:
    if not out:
        return False
    ctx = ""
    if isinstance(out.get("hookSpecificOutput"), dict):
        ctx = out["hookSpecificOutput"].get("additionalContext", "") or ""
    ctx = ctx or out.get("additionalContext", "") or json.dumps(out)
    return "code-context-inherit" in ctx


@pytest.mark.slow
def test_soak_self_contained_prompts_never_latch():
    # per-turn-position: count self-contained turns and how many inherited code
    total_by_pos: dict[int, int] = defaultdict(int)
    inherited_by_pos: dict[int, int] = defaultdict(int)

    with tempfile.TemporaryDirectory(prefix="chz-soak-") as tmp:
        home = Path(tmp)
        (home / ".llm-router").mkdir(parents=True, exist_ok=True)
        for s in range(SESSIONS):
            sid = f"soak-{s}"
            for turn in range(TURNS):
                # Interleave: even turns are code (arm the latch), odd turns are
                # self-contained questions (the ones that must keep routing fresh).
                if turn % 2 == 0:
                    _run_turn(CODE[(s + turn) % len(CODE)], sid, home)
                    continue
                prompt = SELF_CONTAINED[(s + turn) % len(SELF_CONTAINED)]
                out = _run_turn(prompt, sid, home)
                total_by_pos[turn] += 1
                if _inherited_code(out):
                    inherited_by_pos[turn] += 1

    # 1) No self-contained prompt should EVER inherit code — at any position.
    total_inherited = sum(inherited_by_pos.values())
    total_self = sum(total_by_pos.values())
    assert total_self > 0
    assert total_inherited == 0, (
        f"CHZ-EXT-201 decay: {total_inherited}/{total_self} self-contained prompts "
        f"inherited code across the soak (by position: {dict(inherited_by_pos)})"
    )

    # 2) Explicitly assert the historically-dead zone (turn 10+) had fresh routing.
    late_positions = [p for p in total_by_pos if p >= 10]
    assert late_positions, "soak did not reach turn 10+"
    late_inherited = sum(inherited_by_pos[p] for p in late_positions)
    assert late_inherited == 0, "routing latched in the turn-10+ zone (the audit's 0% region)"
