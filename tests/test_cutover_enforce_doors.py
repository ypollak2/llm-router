"""North Star 1.0 cutover step 2 — enforce-route names the consolidated front door.

Under LLM_ROUTER_SLIM=consolidated the legacy tools (llm_query, llm_delegate, …) are
not registered, so the enforced directive must tell the caller to invoke the door
that IS registered (llm / llm_act). In every other tier the message is unchanged.
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
    e = {k: v for k, v in os.environ.items() if k not in ("LLM_ROUTER_ENFORCE", "LLM_ROUTER_SLIM")}
    e["HOME"] = str(home)
    e.update(env)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=e)


def _pending(home, sid, **ov):
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    data = {"expected_tool": "llm_query", "task_type": "query", "complexity": "simple",
            "method": "heuristic", "issued_at": time.time(), "turn_id": 1, "session_id": sid,
            "original_prompt": "What is the capital of France?"}
    data.update(ov)
    (d / f"pending_route_{sid}.json").write_text(json.dumps(data))


def test_consolidated_tier_directive_names_llm_door(tmp_path):
    _pending(tmp_path, "s1")
    r = _run({"session_id": "s1", "tool_name": "Write", "tool_input": {"file_path": "x", "content": "y"}},
             tmp_path, {"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_SLIM": "consolidated"})
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    # CHZ-SURF-01: the door CARRIES the task discriminator. A bare `llm` would be
    # callable but would throw away the specialization the classifier just chose.
    assert 'call llm(task="query")' in out["reason"]
    assert "call llm_query" not in out["reason"]


def test_default_unset_names_the_door(tmp_path):
    # 0.10.0: consolidated is the default, so an UNSET LLM_ROUTER_SLIM names the door.
    _pending(tmp_path, "s2")
    r = _run({"session_id": "s2", "tool_name": "Write", "tool_input": {"file_path": "x", "content": "y"}},
             tmp_path, {"LLM_ROUTER_ENFORCE": "hard"})  # no LLM_ROUTER_SLIM → default consolidated
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    assert 'call llm(task="query")' in out["reason"]
    assert "call llm_query" not in out["reason"]


def test_explicit_legacy_tier_preserves_old_name(tmp_path):
    # Opt back to a legacy tier and the enforced directive keeps the legacy tool name.
    _pending(tmp_path, "s2b")
    r = _run({"session_id": "s2b", "tool_name": "Write", "tool_input": {"file_path": "x", "content": "y"}},
             tmp_path, {"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_SLIM": "off"})
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    assert "call llm_query" in out["reason"]          # legacy name preserved under explicit off


def test_consolidated_operational_names_llm_act(tmp_path):
    _pending(tmp_path, "s3", original_prompt="Fix the failing test in parser.py and make it pass.",
             complexity="moderate")
    r = _run({"session_id": "s3", "tool_name": "Bash", "tool_input": {"command": "python -m pytest -q"}},
             tmp_path, {"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "on", "LLM_ROUTER_SLIM": "consolidated"})
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    assert "llm_act" in out["reason"] and "call llm_delegate" not in out["reason"]


def test_calling_llm_door_clears_lock_under_consolidated(tmp_path):
    _pending(tmp_path, "s4")
    r = _run({"session_id": "s4", "tool_name": "mcp__llm_router__llm", "tool_input": {"prompt": "hi"}},
             tmp_path, {"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_SLIM": "consolidated"})
    assert r.returncode == 0 and r.stdout.strip() == ""
