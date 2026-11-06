"""Audit §2.8: enforcement must not block genuinely non-generative tool calls,
and its block message must be neutral data — not an instruction injected into
the calling agent's tool-error channel.

Fixes (keep blocking, remove the drift):
  §2.8.1 — read-only Bash (ls, cat, git status) and the LS tool are never
           generative, so hard mode must allow them (smart already did). Only
           strict, opt-in, still blocks them. D-396: Q&A tasks included.
  §2.8.3 — the block reason must not coerce the agent ("Return its output
           without modification", "Do NOT generate your own solution").

Write/Edit and mutating Bash STILL block in hard/smart — enforcement is kept.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENFORCE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _write_pending(home: Path, session_id: str, **overrides) -> None:
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "expected_tool": "llm_code",
        "task_type": "code",
        "complexity": "moderate",
        "issued_at": time.time(),
        "session_id": session_id,
        "original_prompt": "refactor the auth module",
    }
    data.update(overrides)
    (router_dir / f"pending_route_{session_id}.json").write_text(json.dumps(data))


def _run(home: Path, session_id: str, tool_name: str, mode: str,
         tool_input: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    # M-04: LLM_ROUTER_HOME now takes precedence over HOME, and the
    # autouse isolation fixture exports it. Pin it to the same sandbox
    # or the subprocess writes to the fixture's dir, not this one.
    env["LLM_ROUTER_HOME"] = str(home) + "/.llm-router"
    env["LLM_ROUTER_ENFORCE"] = mode
    payload = {"session_id": session_id, "tool_name": tool_name}
    if tool_input is not None:
        payload["tool_input"] = tool_input
    return subprocess.run(
        [sys.executable, str(ENFORCE_HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def _blocked(result) -> bool:
    out = result.stdout.strip()
    if not out:
        return False
    try:
        return json.loads(out).get("decision") == "block"
    except json.JSONDecodeError:
        return False


# ── §2.8.1: non-generative ops are never blocked (except strict) ──────────────

def test_hard_mode_allows_readonly_bash_ls(tmp_path):
    """The audit's exact case: `ls` on a code/moderate directive in hard mode."""
    _write_pending(tmp_path, "s1")
    r = _run(tmp_path, "s1", "Bash", "hard", {"command": "ls -la /some/dir"})
    assert not _blocked(r), f"read-only `ls` must not block in hard mode: {r.stdout[:200]}"


def test_hard_mode_allows_ls_tool(tmp_path):
    _write_pending(tmp_path, "s2")
    r = _run(tmp_path, "s2", "LS", "hard", {"path": "/some/dir"})
    assert not _blocked(r), "LS tool (directory listing) is non-generative"


def test_hard_mode_still_blocks_write(tmp_path):
    """Enforcement is KEPT: generative Write still blocks on a code task."""
    _write_pending(tmp_path, "s3")
    r = _run(tmp_path, "s3", "Write", "hard", {"file_path": "/x", "content": "y"})
    assert _blocked(r), "Write must still block — enforcement is retained"


def test_hard_mode_still_blocks_mutating_bash(tmp_path):
    _write_pending(tmp_path, "s4")
    r = _run(tmp_path, "s4", "Bash", "hard", {"command": "rm -rf build/"})
    assert _blocked(r), "mutating Bash must still block"


def test_strict_still_blocks_readonly_bash(tmp_path):
    """Strict is the deliberate opt-out of every escape valve — still blocks."""
    _write_pending(tmp_path, "s5")
    r = _run(tmp_path, "s5", "Bash", "strict", {"command": "ls -la"})
    assert _blocked(r), "strict mode intentionally blocks even read-only Bash"


# ── §2.8.3: block message is neutral, not an agent-directed instruction ───────

_COERCIVE_PHRASES = [
    "without modification",
    "Do NOT generate your own",
    "do not re-analyze",
    "Return its output — do not bypass",
    "Return the result as-is",
]


def test_block_message_is_not_instruction_injection(tmp_path):
    _write_pending(tmp_path, "s6")
    r = _run(tmp_path, "s6", "Write", "hard", {"file_path": "/x", "content": "y"})
    assert _blocked(r)
    reason = json.loads(r.stdout)["reason"]
    for phrase in _COERCIVE_PHRASES:
        assert phrase not in reason, (
            f"block reason injects an agent instruction: {phrase!r}"
        )
    # It should still explain WHY (neutral, factual) and offer the escape valve.
    assert "routing" in reason.lower()


@pytest.mark.parametrize("mode", ["hard", "smart", "suggest"])
@pytest.mark.parametrize("task,complexity", [("query", "simple"), ("research", "moderate")])
@pytest.mark.parametrize("command", ["git status", "rg tasks README.md | head -20", "sed -n '1,40p' README.md"])
def test_qa_inspection_never_pays_a_hold(tmp_path, mode, task, complexity, command):
    _write_pending(tmp_path, "qa", task_type=task, complexity=complexity,
                   original_prompt="Explain the difference between a mutex and a semaphore with examples please")
    r = _run(tmp_path, "qa", "Bash", mode, {"command": command})
    assert r.returncode == 0 and not r.stdout.strip(), r.stderr
    root = tmp_path / ".llm-router"
    assert (root / "pending_route_qa.json").exists(), "inspection does not satisfy routing"
    rows = root / "hold_events.jsonl"
    assert not rows.exists() or not any(json.loads(x)["event"] == "blocked" for x in rows.read_text().splitlines())


@pytest.mark.parametrize("mode", ["hard", "smart"])
@pytest.mark.parametrize("task", ["delegate", "coordination"])
@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_agentic_file_edits_never_pay_a_hold(tmp_path, mode, task, tool):
    _write_pending(tmp_path, "edit", task_type=task, expected_tool="llm_delegate",
                   original_prompt="Carry out the requested changes and report the result")
    r = _run(tmp_path, "edit", tool, mode, {"file_path": "/x", "content": "y"})
    assert r.returncode == 0 and not r.stdout.strip(), r.stderr


@pytest.mark.parametrize("command", ["rg tasks README.md > result.txt", "cat README.md; python change.py"])
def test_qa_mutating_or_unknown_bash_keeps_the_hold(tmp_path, command):
    _write_pending(tmp_path, "prose", task_type="query", complexity="simple",
                   original_prompt="Explain the difference between a mutex and a semaphore with examples please")
    r = _run(tmp_path, "prose", "Bash", "hard", {"command": command})
    assert r.returncode == 0 and _blocked(r), r.stderr
    assert "MANDATORY ROUTE" in json.loads(r.stdout)["reason"]


def test_strict_still_holds_agentic_file_edits(tmp_path):
    _write_pending(tmp_path, "se", task_type="delegate", expected_tool="llm_delegate",
                   original_prompt="Carry out the requested changes and report the result")
    r = _run(tmp_path, "se", "Edit", "strict", {"file_path": "/x", "old_string": "a", "new_string": "b"})
    assert _blocked(r), "strict keeps every hold"
