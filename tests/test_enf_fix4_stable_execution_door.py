"""ENF-FIX-4 (GAP-ENF-3) — the execution door is stable across classifier wobble.

Within one session the heuristic classifier gave materially-similar repo-work
prompts four different task_types (code, coordination, research, query), so the
enforced door flip-flopped turn to turn. ENF-FIX-1 only redirected code/
coordination execution work to the tool-capable door, so a turn that happened to
classify as research/query still dead-ended at the text-only door.

Because the execution signal is high-precision (it stays silent on prose /
explanation / pure authoring), it is safe to honor it regardless of the wobbling
task_type: any execution-needing prompt routes to ``llm_act``. This test pins that
the door no longer depends on which task_type the classifier happened to pick.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"

_ENV = {"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "on", "LLM_ROUTER_SLIM": "consolidated"}
_EXEC_PROMPT = "Run the database migration and commit the result."


def _run(payload, home, env):
    e = {k: v for k, v in os.environ.items() if k not in ("LLM_ROUTER_ENFORCE", "LLM_ROUTER_SLIM", "LLM_ROUTER_DELEGATE")}
    e["HOME"] = str(home)
    e.update(env)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=e)


def _block_reason(sid, task_type):
    """Return the enforce-route block reason for an execution prompt classified as
    *task_type*, or "" when the tool was allowed through (no block).

    Uses its OWN fresh home so no per-session enforcement state leaks between calls.
    Write is the probe (a generative tool, not subject to the read-only/local-Bash
    exemptions) so the enforced door is actually surfaced when the work IS blocked."""
    home = Path(tempfile.mkdtemp(prefix="enf4-"))
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"pending_route_{sid}.json").write_text(json.dumps({
        "expected_tool": "llm_code", "task_type": task_type, "complexity": "moderate",
        "method": "heuristic", "issued_at": time.time(), "turn_id": 1, "session_id": sid,
        "original_prompt": _EXEC_PROMPT,
    }))
    r = _run({"session_id": sid, "tool_name": "Write",
              "tool_input": {"file_path": "x.py", "content": "y"}}, home, _ENV)
    if not r.stdout.strip():
        return ""  # allowed through — no directive dead-end
    return json.loads(r.stdout).get("reason", "")


def _door_named(sid, task_type):
    reason = _block_reason(sid, task_type)
    assert reason, f"expected a block for {task_type!r} execution work"
    return reason


def test_research_typed_execution_prompt_names_llm_act():
    """Fail-before: a research-classified execution prompt fell outside the
    code/coordination gate and named the text-only door. Pass-after: it names
    llm_act — the wobble no longer dead-ends it."""
    reason = _door_named("r1", "research")
    assert "llm_act" in reason, f"execution work classified 'research' must reach llm_act: {reason!r}"
    assert "→ call llm\n" not in reason


def test_query_typed_execution_prompt_names_llm_act():
    reason = _door_named("q1", "query")
    assert "llm_act" in reason, f"execution work classified 'query' must reach llm_act: {reason!r}"
    assert "→ call llm\n" not in reason


def test_no_task_type_dead_ends_execution_at_the_text_only_door():
    """The stability invariant: whichever task_type the classifier picks this turn,
    execution work is NEVER routed to the text-only `llm` door. Each is either
    blocked toward the tool-capable `llm_act` door, or allowed through — never a
    text-only dead-end."""
    for i, tt in enumerate(("code", "coordination", "research", "query")):
        reason = _block_reason(f"s{i}", tt)
        assert "→ call llm\n" not in reason, (
            f"execution work classified {tt!r} was dead-ended at the text-only door: {reason!r}")
        if reason:  # when it DID block, it must name the tool-capable door
            assert "llm_act" in reason, f"{tt!r} block must name llm_act, got {reason!r}"
