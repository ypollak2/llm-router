"""ENF-FIX-1 (GAP-ENF-1 / INV-ROUTE-006) — execution work names the tool-capable door.

Execution/repo work that lacks a verification cue does not trip
``detect_operational`` (correctly — that predicate is for delegate-worthy work).
Before this fix such a request under hard enforcement was routed to the text-only
``llm`` door and then blocked the moment it reached Bash: a structural dead-end.

Enforcement now consults a SEPARATE high-precision execution signal, so a
code/coordination request that needs local execution names ``llm_act`` — while an
explanatory prompt with the same task_type still names the text-only door (the
signal, not the task_type, gates the redirect).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _run(payload, home, env):
    e = {k: v for k, v in os.environ.items() if k not in ("LLM_ROUTER_ENFORCE", "LLM_ROUTER_SLIM", "LLM_ROUTER_DELEGATE")}
    e["HOME"] = str(home)
    e.update(env)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=e)


def _pending(home, sid, **ov):
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    data = {"expected_tool": "llm_code", "task_type": "code", "complexity": "moderate",
            "method": "heuristic", "issued_at": time.time(), "turn_id": 1, "session_id": sid,
            "original_prompt": "Run the test suite and commit the passing changes."}
    data.update(ov)
    (d / f"pending_route_{sid}.json").write_text(json.dumps(data))


_ENV = {"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "on", "LLM_ROUTER_SLIM": "consolidated"}


def test_execution_request_without_verify_cue_names_llm_act(tmp_path):
    """Fail-before: a code task needing local execution but with no verification
    cue is routed to the text-only 'llm' door. Pass-after: it names 'llm_act'."""
    _pending(tmp_path, "e1", original_prompt="Run the database migration and commit the result.")
    r = _run({"session_id": "e1", "tool_name": "Bash", "tool_input": {"command": "git commit -am x"}},
             tmp_path, _ENV)
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    assert "llm_act" in out["reason"], f"execution work must name the tool-capable door: {out['reason']!r}"


def test_explanatory_prompt_same_task_type_stays_text_only(tmp_path):
    """The signal — not the task_type — gates the redirect: an explanatory prompt
    with the SAME code task_type must NOT be pushed to llm_act (no over-routing)."""
    _pending(tmp_path, "e2",
             original_prompt="Explain how to run the database migration and commit the result.")
    r = _run({"session_id": "e2", "tool_name": "Bash", "tool_input": {"command": "git commit -am x"}},
             tmp_path, _ENV)
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    assert "llm_act" not in out["reason"], f"explanation must not be routed to llm_act: {out['reason']!r}"
    # CHZ-SURF-01: the text-only door, carrying the task the pending state set.
    assert 'call llm(task="code")' in out["reason"]


def test_calling_llm_act_clears_the_execution_lock(tmp_path):
    """Never a trap: invoking llm_act satisfies the redirected directive."""
    _pending(tmp_path, "e3", original_prompt="Deploy the service to the staging cluster.")
    r = _run({"session_id": "e3", "tool_name": "mcp__llm_router__llm_act", "tool_input": {"task": "deploy"}},
             tmp_path, _ENV)
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_delegate_off_disables_the_execution_redirect(tmp_path):
    """LLM_ROUTER_DELEGATE=off must disable the redirect (same escape hatch as the
    operational redirect)."""
    _pending(tmp_path, "e4", original_prompt="Run the test suite and commit the passing changes.")
    env = dict(_ENV)
    env["LLM_ROUTER_DELEGATE"] = "off"
    r = _run({"session_id": "e4", "tool_name": "Bash", "tool_input": {"command": "git commit -am x"}},
             tmp_path, env)
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    assert "llm_act" not in out["reason"]
