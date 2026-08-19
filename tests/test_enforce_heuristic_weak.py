"""Heuristic-weak routes must downgrade to soft enforcement.

The classifier emits ``method=heuristic-weak`` when a prompt scored
positive on the keyword heuristic but didn't cross the strong-confidence
threshold. Treating those as hard-blocking routes traps introspection
prompts ("show me my X", "list my Y today") whose answer lives in
``~/.llm-router/usage.db`` rather than in any LLM — the user can't reach
their own data.

Contract pinned here:

1. ``smart`` / ``hard`` downgrade to ``soft`` when the pending route
   was issued by heuristic-weak. The route still appears in the log;
   the native tool call goes through.
2. ``strict`` downgrades to ``soft`` too — the operator opted into
   strict for bypass discipline, but a weak-confidence guess can't be
   the source of that discipline. Logging is preserved.
3. Strong-method routes (heuristic, ollama, api, ...) are NOT touched
   by the downgrade — those are confidence the user explicitly trusts.
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


def _run_hook(
    payload: dict,
    *,
    home: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ENFORCE_ROUTE_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _write_pending(home: Path, session_id: str, *, method: str | None,
                   **overrides) -> Path:
    """Write a pending-route file with the given classification method."""
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    pending = router_dir / f"pending_route_{session_id}.json"
    data = {
        "expected_tool": "llm_query",
        "task_type": "query",
        "complexity": "simple",
        "issued_at": time.time(),
        "session_id": session_id,
    }
    if method is not None:
        data["method"] = method
    data.update(overrides)
    pending.write_text(json.dumps(data), encoding="utf-8")
    return pending


def _enforcement_log(home: Path) -> str:
    log = home / ".llm-router" / "enforcement.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


# ── heuristic-weak downgrades enforcement ──────────────────────────────


def test_heuristic_weak_downgrades_smart_to_soft(tmp_path):
    """Smart mode would normally block Bash for query tasks; with a
    heuristic-weak pending, Bash must go through (logged as soft)."""
    session_id = "sess-weak-smart"
    _write_pending(tmp_path, session_id, method="heuristic-weak")

    result = _run_hook(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "sqlite3 ~/.llm-router/usage.db '.tables'"},
        },
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "smart"},
    )

    assert result.returncode == 0
    # No block payload — Bash was allowed through.
    assert result.stdout.strip() == "", (
        f"weak route should not produce a block; got stdout: {result.stdout!r}"
    )
    log = _enforcement_log(tmp_path)
    assert "outcome=ALLOWED(soft)" in log


def test_heuristic_weak_downgrades_hard_to_soft(tmp_path):
    session_id = "sess-weak-hard"
    _write_pending(tmp_path, session_id, method="heuristic-weak")

    result = _run_hook(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        },
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    log = _enforcement_log(tmp_path)
    assert "outcome=ALLOWED(soft)" in log


def test_heuristic_weak_downgrades_strict_to_soft(tmp_path):
    """Strict mode disables escape valves — heuristic-weak still wins.

    A weak-confidence classifier guess can't be the source of strict-
    mode discipline. Operators who want strict on weak routes can
    tighten the classifier; the route contract here is "log, don't
    block".
    """
    session_id = "sess-weak-strict"
    _write_pending(tmp_path, session_id, method="heuristic-weak")

    result = _run_hook(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        },
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "strict"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    log = _enforcement_log(tmp_path)
    assert "outcome=ALLOWED(soft)" in log


# ── Strong methods are untouched ───────────────────────────────────────


def test_strong_heuristic_still_blocks(tmp_path):
    """Strong-confidence routes must keep the existing hard-block."""
    session_id = "sess-strong"
    _write_pending(tmp_path, session_id, method="heuristic")

    result = _run_hook(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/scratch"},
        },
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    # Hard block fires with the usual JSON decision payload.
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block", (
        "strong heuristic must NOT inherit the weak downgrade"
    )


def test_missing_method_treated_as_strong(tmp_path):
    """Backwards-compat: a pending file written before the method field
    existed must still hard-block, not silently downgrade to soft."""
    session_id = "sess-no-method"
    _write_pending(tmp_path, session_id, method=None)

    result = _run_hook(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/scratch"},
        },
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block"


def test_block_output_carries_permission_decision_p0(tmp_path):
    """CLAUDE-CODE-CONFORMANCE P0: a PreToolUse block must emit the CURRENT
    hookSpecificOutput.permissionDecision='deny' contract, not only the deprecated
    top-level decision:block (which relies on a compat shim). Without this, a newer
    Claude Code that dropped the shim would silently no-op every block while the
    enforcement log still records BLOCKED — an enforcement + honesty failure."""
    session_id = "sess-p0-permdecision"
    _write_pending(tmp_path, session_id, method="heuristic")
    result = _run_hook(
        {"session_id": session_id, "tool_name": "Bash",
         "tool_input": {"command": "rm -rf /tmp/scratch"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block"  # legacy field retained for older Claude Code
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("hookEventName") == "PreToolUse"
    assert hso.get("permissionDecision") == "deny"
    assert hso.get("permissionDecisionReason"), "deny must carry a reason"
