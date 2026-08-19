"""North Star P5: agent-step parity.

Every *agent step* must route through the same pipeline as a user prompt. This is
inherent today — enforce-route.py keys only on (session_id, tool_name, tool_input)
and has no agent/subagent branch, so the operational->delegate redirect (and the
sandbox, and the R2 floor) apply identically to a tool call whether it originates
from a user prompt or a running subagent. This guard LOCKS that parity: a future
change must not secretly exempt agent-originated steps (which would silently break
the North Star for agent-generated work).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENFORCE_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"
_OP_PROMPT = "Fix the failing test in parser.py and make it pass."


def _run_hook(payload: dict, *, home: Path, extra_env: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ENFORCE_ROUTE_HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def _write_pending(home: Path, sid: str) -> None:
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"pending_route_{sid}.json").write_text(json.dumps({
        "expected_tool": "llm_code", "task_type": "code", "complexity": "moderate",
        "method": "heuristic", "issued_at": time.time(), "turn_id": 1, "session_id": sid,
        "original_prompt": _OP_PROMPT,
    }))


_ENV = {"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "on"}


def test_agent_step_gets_same_delegate_redirect_as_user_step(tmp_path):
    # A plain (user-origin) operational tool call → redirected to llm_delegate.
    _write_pending(tmp_path, "sess-user")
    user = _run_hook({"session_id": "sess-user", "tool_name": "Bash",
                      "tool_input": {"command": "python -m pytest -q"}},
                     home=tmp_path, extra_env=_ENV)

    # The SAME call carrying subagent-context fields → must behave identically.
    _write_pending(tmp_path, "sess-agent")
    agent = _run_hook({"session_id": "sess-agent", "tool_name": "Bash",
                       "tool_input": {"command": "python -m pytest -q"},
                       "subagent_type": "code-reviewer", "parent_session": "sess-user"},
                      home=tmp_path, extra_env=_ENV)

    assert "llm_act" in user.stdout, "user step must redirect to the agentic door"
    assert "llm_act" in agent.stdout, "agent step must get the SAME redirect (parity)"
    assert (json.loads(user.stdout)["decision"]
            == json.loads(agent.stdout)["decision"] == "block")
