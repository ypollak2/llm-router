"""#29 — a coordination LOCAL_BASH command whose redirect fires names llm_act.

`LOCAL_BASH_EXEMPT` soft-allows a non-routable local command (git/build/test) so
enforcement doesn't trap a bare deterministic shell op. But it lacked the carve-out
that the sibling exemptions have, so a SUBSTANTIAL coordination execution task (whose
operational/execution signal fires and would route to the tool-capable llm_act door)
had its local bash command soft-exempted to native instead — silently defeating the
North-Star redirect and letting Claude run the shell work instead of a cheap delegated
agent. The exemption now defers to the redirect exactly when the redirect will fire.
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
    e = {k: v for k, v in os.environ.items()
         if k not in ("LLM_ROUTER_ENFORCE", "LLM_ROUTER_SLIM", "LLM_ROUTER_DELEGATE")}
    e["HOME"] = str(home)
    e.update(env)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=e)


def _pending(home, sid, **ov):
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    data = {"expected_tool": "llm", "task_type": "coordination", "complexity": "moderate",
            "method": "heuristic", "issued_at": time.time(), "turn_id": 1, "session_id": sid,
            "original_prompt": "Run the test suite and commit the passing changes."}
    data.update(ov)
    (d / f"pending_route_{sid}.json").write_text(json.dumps(data))


_ENV = {"LLM_ROUTER_ENFORCE": "hard", "LLM_ROUTER_DELEGATE": "on", "LLM_ROUTER_SLIM": "consolidated"}


def test_substantial_coordination_bash_names_llm_act(tmp_path):
    """Fail-before: the coordination execution task's local bash is soft-exempted to
    native (empty stdout) so the redirect never fires. Pass-after: it blocks and
    names the tool-capable door llm_act."""
    _pending(tmp_path, "c1")  # coordination, moderate, execution signal fires
    r = _run({"session_id": "c1", "tool_name": "Bash",
              "tool_input": {"command": "pytest -q && git commit -am done"}},
             tmp_path, _ENV)
    assert r.stdout.strip(), "substantial coordination execution bash must not be native-exempted"
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    assert "llm_act" in out["reason"], f"must name the tool-capable door: {out['reason']!r}"


def test_simple_coordination_bash_still_native_exempt(tmp_path):
    """No over-correction: a SIMPLE coordination command (redirect won't fire — the
    cost floor keeps trivial one-liners off the heavy agentic loop) still soft-exempts
    to native, the cheapest capable executor for a specified command."""
    _pending(tmp_path, "c2", complexity="simple",
             original_prompt="Run the test suite and commit the passing changes.")
    r = _run({"session_id": "c2", "tool_name": "Bash",
              "tool_input": {"command": "pytest -q && git commit -am done"}}, tmp_path, _ENV)
    assert r.stdout.strip() == "" or json.loads(r.stdout).get("decision") != "block"


def test_delegate_off_keeps_native_exempt(tmp_path):
    """The redirect (and this carve-out) is gated on LLM_ROUTER_DELEGATE=on; off ⇒ the
    coordination command falls back to the native exemption."""
    _pending(tmp_path, "c3")
    env = dict(_ENV)
    env["LLM_ROUTER_DELEGATE"] = "off"
    r = _run({"session_id": "c3", "tool_name": "Bash",
              "tool_input": {"command": "pytest -q && git commit -am done"}}, tmp_path, env)
    assert r.stdout.strip() == "" or json.loads(r.stdout).get("decision") != "block"
