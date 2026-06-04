"""Tests for routing enforcement behavior in the shipped hook scripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUTO_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"
ENFORCE_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _run_hook(
    hook_path: Path,
    payload: dict,
    *,
    home: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # Strip shell-level enforcement overrides so tests are deterministic.
    # The hook defaults to "smart"; tests that need a specific mode pass extra_env.
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _write_pending(home: Path, session_id: str, **overrides) -> Path:
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    pending_path = router_dir / f"pending_route_{session_id}.json"
    data = {
        "expected_tool": "llm_query",
        "task_type": "query",
        "complexity": "simple",
        "issued_at": time.time(),
        "session_id": session_id,
    }
    data.update(overrides)
    pending_path.write_text(json.dumps(data), encoding="utf-8")
    return pending_path


def test_enforce_route_blocks_work_tools_by_default(tmp_path):
    """Hard enforcement is the default when no env override is provided."""
    session_id = "sess-hard-default"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block"
    assert "Directive:" in out["reason"]
    assert "Bash" in out["reason"] and "blocked" in out["reason"].lower()


def test_enforce_route_soft_mode_still_logs_but_allows(tmp_path):
    """Users can explicitly relax enforcement without losing violation logging."""
    session_id = "sess-soft-override"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "soft"},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    log_text = (tmp_path / ".llm-router" / "enforcement.log").read_text(encoding="utf-8")
    assert "VIOLATION" in log_text
    assert "expected=llm_query" in log_text


def test_enforce_route_allows_file_tools_to_prevent_stuck_patterns(tmp_path):
    """Glob/Read/Grep/LS are now allowed early to prevent stuck patterns where investigation tools keep failing.

    The enforce-route hook v12+ marks these as 'coding' operations and allows them silently
    to prevent deadlocks. This prevents the scenario where Claude can't investigate the hook
    because the hook blocks investigation tools.

    v13 behavior: In hard mode, ALL native tools (including Read/Glob/Grep/LS)
    are blocked until routing is satisfied. This prevents model from bypassing
    routing by jumping straight to file operations.
    """
    for tool_name in ("Read", "Glob", "Grep", "LS"):
        session_id = f"sess-qa-{tool_name.lower()}"
        _write_pending(tmp_path, session_id, task_type="query")

        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool_name},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )

        # v13: Read/Glob/Grep/LS are BLOCKED in hard mode for Q&A tasks
        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert out["decision"] == "block", f"{tool_name} should be blocked in hard mode for Q&A tasks"


def test_enforce_route_blocks_file_tools_in_hard_mode_for_code_tasks(tmp_path):
    """v13: In hard mode, Read/Glob/Grep/LS are blocked even for code tasks until routing satisfied."""
    for tool_name in ("Read", "Glob", "Grep", "LS"):
        session_id = f"sess-code-hard-{tool_name.lower()}"
        _write_pending(tmp_path, session_id, task_type="code", expected_tool="llm_code")

        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool_name},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )

        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert out["decision"] == "block", f"{tool_name} should be blocked in hard mode for code tasks"


def test_smart_mode_allows_read_for_code_tasks(tmp_path):
    """v13: Smart mode allows Read/Glob/Grep/LS for code tasks (needed for implementation)."""
    session_id = "sess-smart-code-read"
    _write_pending(tmp_path, session_id, task_type="code", expected_tool="llm_code")

    for tool_name in ("Read", "Glob", "Grep", "LS"):
        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool_name},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "smart"},
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "", f"{tool_name} should be allowed in smart mode for code tasks"


def test_smart_mode_blocks_read_for_qa_tasks(tmp_path):
    """v13: Smart mode blocks Read/Glob/Grep/LS for Q&A tasks."""
    for task_type in ("query", "research", "generate", "analyze"):
        session_id = f"sess-smart-qa-{task_type}"
        _write_pending(tmp_path, session_id, task_type=task_type)

        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": "Read"},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "smart"},
        )

        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert out["decision"] == "block", f"Read should be blocked in smart mode for {task_type} tasks"


def _write_routing_yaml(home: Path, content: str) -> Path:
    """Write a routing.yaml to the fake home's .llm-router directory."""
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = router_dir / "routing.yaml"
    yaml_path.write_text(content, encoding="utf-8")
    return yaml_path


# ── routing.yaml fallback tests ───────────────────────────────────────────────
# Fix: The enforcer previously defaulted to "smart" when LLM_ROUTER_ENFORCE was
# absent, silently ignoring routing.yaml's `enforce:` setting. Now it reads
# routing.yaml as a fallback before applying the built-in default.


def test_routing_yaml_enforce_hard_blocks_bash_for_code_tasks(tmp_path):
    """routing.yaml enforce: hard → Bash blocked even for code tasks (unlike smart mode)."""
    _write_routing_yaml(tmp_path, "enforce: hard\n")
    session_id = "sess-yaml-hard-code"
    _write_pending(tmp_path, session_id, task_type="code", expected_tool="llm_code")

    # No LLM_ROUTER_ENFORCE in extra_env — hook must read routing.yaml
    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block", "Hard mode from routing.yaml must block Bash for code tasks"
    assert "Bash" in out["reason"] and "blocked" in out["reason"].lower()


def test_routing_yaml_enforce_soft_allows_bash_but_logs(tmp_path):
    """routing.yaml enforce: soft → violation logged but Bash allowed."""
    _write_routing_yaml(tmp_path, "enforce: soft\n")
    session_id = "sess-yaml-soft"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "", "Soft mode must allow without blocking"
    log_text = (tmp_path / ".llm-router" / "enforcement.log").read_text(encoding="utf-8")
    assert "VIOLATION" in log_text


def test_routing_yaml_enforce_off_skips_all_enforcement(tmp_path):
    """routing.yaml enforce: off → hook exits immediately, no log written."""
    _write_routing_yaml(tmp_path, "enforce: off\n")
    session_id = "sess-yaml-off"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    log_path = tmp_path / ".llm-router" / "enforcement.log"
    assert not log_path.exists(), "Off mode must not write the enforcement log"


def test_routing_yaml_enforce_shadow_treated_as_off(tmp_path):
    """routing.yaml enforce: shadow → identical to 'off' (pure observation)."""
    _write_routing_yaml(tmp_path, "enforce: shadow\n")
    session_id = "sess-yaml-shadow"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_env_var_takes_priority_over_routing_yaml(tmp_path):
    """LLM_ROUTER_ENFORCE env var always overrides routing.yaml."""
    _write_routing_yaml(tmp_path, "enforce: soft\n")  # yaml says soft
    session_id = "sess-env-wins"
    _write_pending(tmp_path, session_id, task_type="code", expected_tool="llm_code")

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},  # env var says hard → must win
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block", "Env var 'hard' must override routing.yaml 'soft'"


def test_defaults_to_smart_when_neither_env_var_nor_yaml(tmp_path):
    """No env var + no routing.yaml → smart mode: blocks Q&A Bash, allows code Bash."""
    # Smart mode blocks Bash for Q&A tasks
    session_id_qa = "sess-default-qa"
    _write_pending(tmp_path, session_id_qa, task_type="query")

    result_qa = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id_qa, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result_qa.returncode == 0
    out_qa = json.loads(result_qa.stdout)
    assert out_qa["decision"] == "block", "Smart default must block Bash for Q&A tasks"

    # v13: Smart mode blocks Bash for ALL task types until routing satisfied
    session_id_code = "sess-default-code"
    _write_pending(tmp_path, session_id_code, task_type="code", expected_tool="llm_code")

    result_code = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id_code, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result_code.returncode == 0
    out_code = json.loads(result_code.stdout)
    assert out_code["decision"] == "block", "Smart default must block Bash for code tasks until routing satisfied"


def test_routing_yaml_with_leading_spaces_and_trailing_whitespace(tmp_path):
    """enforce: value is correctly parsed even with leading/trailing whitespace."""
    yaml_content = (
        "# LLM Router configuration\n"
        "model_tier: auto\n"
        "  enforce:  hard  \n"  # leading indent + trailing spaces
        "daily_budget: 5.00\n"
    )
    _write_routing_yaml(tmp_path, yaml_content)
    session_id = "sess-yaml-whitespace"
    _write_pending(tmp_path, session_id, task_type="code", expected_tool="llm_code")

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block", "Parser must strip whitespace from enforce: value"


def test_routing_yaml_without_enforce_line_defaults_to_smart(tmp_path):
    """routing.yaml exists but has no enforce: line → falls through to smart default."""
    _write_routing_yaml(tmp_path, "model_tier: auto\ndaily_budget: 5.00\n")
    session_id = "sess-yaml-no-enforce"
    _write_pending(tmp_path, session_id, task_type="query")

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result.returncode == 0
    # Smart mode for Q&A → Bash is blocked
    out = json.loads(result.stdout)
    assert out["decision"] == "block"


def test_auto_route_logs_unrouted_previous_turn_on_next_prompt(tmp_path):
    """A pending route that survives to the next prompt is recorded as NO_ROUTE."""
    session_id = "sess-unrouted-prior-turn"
    pending_path = _write_pending(tmp_path, session_id)
    old_pending = json.loads(pending_path.read_text(encoding="utf-8"))

    result = _run_hook(
        AUTO_ROUTE_HOOK,
        {
            "session_id": session_id,
            "prompt": "Write a blog post about routing economics",
        },
        home=tmp_path,
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    # Hook may return:
    # - contextForAgent (Claude pass-through path)
    # - decision:block + reason (block mode direct execution)
    # - decision:approve + additionalContext (echo mode direct execution)
    hook_out = out.get("hookSpecificOutput", {})
    if "contextForAgent" in hook_out:
        ctx = hook_out["contextForAgent"]
    elif "additionalContext" in hook_out:
        ctx = hook_out["additionalContext"]
    elif out.get("decision") == "block":
        ctx = out.get("reason", "")
    else:
        pytest.fail(f"Unexpected hook output format: {out}")
    assert "PREVIOUS TURN VIOLATED ROUTING" in ctx
    assert "expected llm_query for query/simple" in ctx

    # With direct execution (block or echo mode), pending state may or may not exist.
    # With Claude pass-through path (MANDATORY ROUTE directive), pending state is updated.
    # Echo mode also uses contextForAgent but doesn't write pending state.
    if pending_path.exists() and out.get("decision") != "block":
        new_pending = json.loads(pending_path.read_text(encoding="utf-8"))
        if new_pending["issued_at"] > old_pending["issued_at"]:
            assert new_pending["task_type"] != old_pending["task_type"]

    log_text = (tmp_path / ".llm-router" / "enforcement.log").read_text(encoding="utf-8")
    assert "NO_ROUTE" in log_text
    assert "expected=llm_query" in log_text
    assert "task=query/simple" in log_text
    # Prior unrouted turn context is now in contextForAgent, not systemMessage
    assert "PREVIOUS TURN VIOLATED ROUTING" in ctx or "prior unrouted turn" in ctx


# ── Read-only Bash allowlist (smart mode, code tasks) ─────────────────────────


READONLY_BASH_CASES = [
    "ls /tmp",
    "find . -name '*.py'",
    "cat README.md",
    "git status",
    "git log --oneline -5",
    "git diff HEAD",
    "git show HEAD:path/to/file.py",
    "gh pr view 132",
    "gh run list --limit 5",
    "git log --oneline | head -10",
    "grep -r foo src/",
    "wc -l file.txt",
]

WRITE_BASH_CASES = [
    "rm -rf /tmp/data",
    "git push origin main",
    "git commit -m msg",
    "git checkout main",
    "git reset --hard HEAD",
    "gh pr comment 132 --body /evaluate",
    "gh pr merge 132",
    "npm install",
    "uv sync",
    "pip install requests",
    "sudo apt-get update",
    "curl -X POST https://example.com",
    "echo hi > file.txt",
    "mv a b",
]


@pytest.mark.parametrize("command", READONLY_BASH_CASES)
def test_readonly_bash_allowed_for_code_tasks(tmp_path, command):
    """Smart mode: investigation-style Bash passes through for code tasks."""
    session_id = "sess-bash-readonly"
    _write_pending(tmp_path, session_id, task_type="code", complexity="moderate",
                   expected_tool="llm_code")

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
        home=tmp_path,
    )

    assert result.returncode == 0, f"hook failed: {result.stderr}"
    # Empty stdout = allow (no block decision emitted)
    assert result.stdout.strip() == "", (
        f"expected allow for read-only Bash {command!r}, got: {result.stdout}"
    )


@pytest.mark.parametrize("command", WRITE_BASH_CASES)
def test_write_bash_still_blocked_for_code_tasks(tmp_path, command):
    """Smart mode: write/destructive Bash still requires routing."""
    session_id = "sess-bash-write"
    _write_pending(tmp_path, session_id, task_type="code", complexity="moderate",
                   expected_tool="llm_code")

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
        home=tmp_path,
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block", (
        f"expected block for write Bash {command!r}, got: {out}"
    )


def test_readonly_bash_blocked_for_qa_tasks(tmp_path):
    """Q&A tasks must route — even read-only Bash bypasses the cheap model."""
    session_id = "sess-bash-qa"
    _write_pending(tmp_path, session_id, task_type="query", complexity="simple",
                   expected_tool="llm_query")

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
        },
        home=tmp_path,
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block"


# ── Loop detection → auto-pivot ───────────────────────────────────────────────


def test_loop_detection_triggers_auto_pivot(tmp_path):
    """3+ blocked same-tool calls in 2 min should release the lock."""
    session_id = "sess-loop"
    _write_pending(tmp_path, session_id, task_type="query", complexity="simple",
                   expected_tool="llm_query")

    # Seed tool history with 3 prior Bash calls in the last 2 minutes.
    router_dir = tmp_path / ".llm-router"
    history_path = router_dir / f"tool_history_{session_id}.json"
    now = time.time()
    history_path.write_text(
        json.dumps({
            "calls": [
                {"tool": "Bash", "timestamp": now - 30},
                {"tool": "Bash", "timestamp": now - 20},
                {"tool": "Bash", "timestamp": now - 10},
            ]
        }),
        encoding="utf-8",
    )

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi > /tmp/out"},  # write op, normally blocked
        },
        home=tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"loop should have released lock; got block: {result.stdout}"
    )

    # Pending state should be cleared so subsequent tools also pass.
    pending_path = router_dir / f"pending_route_{session_id}.json"
    assert not pending_path.exists(), "loop pivot should clear pending state"

    # Log entry should be present.
    log_text = (router_dir / "enforcement.log").read_text(encoding="utf-8")
    assert "AUTO-PIVOT (loop)" in log_text


def test_violation_count_pivot_at_4(tmp_path):
    """Auto-pivot triggers at violation 4 (matches the updated UX messaging)."""
    session_id = "sess-count-pivot"
    _write_pending(tmp_path, session_id, task_type="query", complexity="simple",
                   expected_tool="llm_query")

    # Seed violation counter at 3 — next blocked call hits 4 and triggers pivot.
    router_dir = tmp_path / ".llm-router"
    counter_path = router_dir / f"violations_{session_id}.json"
    counter_path.write_text(
        json.dumps({"count": 3, "last_violation_at": time.time()}),
        encoding="utf-8",
    )

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {
            "session_id": session_id,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"},
        },
        home=tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"4th violation should pivot; got block: {result.stdout}"
    )
    log_text = (router_dir / "enforcement.log").read_text(encoding="utf-8")
    assert "AUTO-PIVOT (count)" in log_text


# ── Messaging consistency ─────────────────────────────────────────────────────


def test_block_message_shows_correct_threshold(tmp_path):
    """Block message should reference /4 (matches actual threshold), not /2."""
    session_id = "sess-msg-threshold"
    _write_pending(tmp_path, session_id, task_type="query", complexity="simple",
                   expected_tool="llm_query")

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        },
        home=tmp_path,
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    reason = out["reason"]
    assert "1/4" in reason or "/4" in reason, (
        f"block message should mention /4 threshold, got: {reason[:300]}"
    )
    # Old misleading text must not reappear.
    assert "1/2" not in reason
    assert "2/2+" not in reason


def test_block_message_documents_escape_valve(tmp_path):
    """Block message must mention the llm_* clear-lock escape."""
    session_id = "sess-escape"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        },
        home=tmp_path,
    )

    out = json.loads(result.stdout)
    assert "Escape valves" in out["reason"]
    assert "llm_" in out["reason"]
    assert "loop" in out["reason"].lower() or "retry the same tool" in out["reason"].lower()
