"""Tests for routing enforcement behavior in the shipped hook scripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router.tool_surface import route_tool


ROOT = Path(__file__).resolve().parents[1]
AUTO_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"
ENFORCE_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _run_hook(
    hook_path: Path,
    payload: dict,
    *,
    home: Path,
    extra_env: dict[str, str] | None = None,
    inject_default_mode: str | None = "smart",
) -> subprocess.CompletedProcess[str]:
    # Strip shell-level enforcement overrides so tests are deterministic.
    #
    # The PRODUCT default is now "smart" (F01/North Star: enforce routing out of
    # the box). The helper injects LLM_ROUTER_ENFORCE="smart" only when the test
    # hasn't written its own routing.yaml and hasn't passed an explicit
    # LLM_ROUTER_ENFORCE — keeping the routing.yaml tests reading their yaml and the
    # blocking tests exercising smart, with no per-test churn. Tests that assert
    # the real resolver DEFAULT (now "smart") pass inject_default_mode=None.
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    _yaml_present = (home / ".llm-router" / "routing.yaml").exists()
    _explicit_mode = bool(extra_env and "LLM_ROUTER_ENFORCE" in extra_env)
    if inject_default_mode is not None and not _yaml_present and not _explicit_mode:
        env["LLM_ROUTER_ENFORCE"] = inject_default_mode
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
    """The product default is now 'smart' (North Star: enforce routing out of the
    box so offloadable work goes to cheaper models). Work tools are BLOCKED until
    routing is satisfied — no opt-in required. Relax with LLM_ROUTER_ENFORCE=soft/off."""
    session_id = "sess-smart-default"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
        inject_default_mode=None,  # exercise the real resolver default (now smart)
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block", "smart default must block Bash until routed"


def test_enforce_route_blocks_work_tools_in_smart_mode(tmp_path):
    """Opt-in smart mode still blocks work tools until routing is satisfied."""
    session_id = "sess-smart-explicit"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "smart"},
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
    assert "expected=llm" in log_text          # consolidated default → door name in the log


def test_read_tools_allowed_for_qa_in_hard_mode(tmp_path):
    """P1 / INV-ROUTE-001/002: read-only tools are ALLOWED for Q&A even in hard mode.

    Blocking Read/Grep/Glob while forcing the request through the text-only `llm`
    door (which cannot fetch a file) was a structural dead-end for any Q&A prompt
    about an unseen file. Read-only context-gathering is now never blocked; routing
    of the ANSWER is still enforced by the directive + stop-enforce override
    detection. Generative tools remain blocked (see the write-tool tests)."""
    for tool_name in ("Read", "Glob", "Grep", "LS"):
        session_id = f"sess-qa-{tool_name.lower()}"
        _write_pending(tmp_path, session_id, task_type="query")

        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool_name},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "", f"{tool_name} should be ALLOWED for Q&A (no dead-end)"


def test_read_tools_allowed_in_hard_mode_for_code_tasks(tmp_path):
    """Read-only tools are allowed for code tasks in hard mode too — reading files
    to gather context is non-generative and never a routing bypass."""
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
        assert result.stdout.strip() == "", f"{tool_name} should be allowed for code tasks"


def test_smart_mode_allows_read_for_code_tasks(tmp_path):
    """Smart mode allows Read/Glob/Grep/LS for code tasks (needed for implementation)."""
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


def test_smart_mode_allows_read_for_qa_tasks(tmp_path):
    """P1 / INV-ROUTE-001/002: smart mode ALLOWS Read/Glob/Grep/LS for Q&A tasks
    (previously blocked — a capability dead-end). The answer is still routed via
    the directive + stop-enforce override detection, not by blocking reads."""
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
        assert result.stdout.strip() == "", f"Read should be ALLOWED in smart mode for {task_type} tasks"


def test_write_tools_still_blocked_for_qa(tmp_path):
    """The enforcement intent is preserved: generative tools stay blocked until
    routing is satisfied, so Q&A answers can't be produced by Bash/Edit/Write."""
    for tool_name in ("Edit", "Write"):
        session_id = f"sess-qa-write-{tool_name.lower()}"
        _write_pending(tmp_path, session_id, task_type="query")
        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool_name},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )
        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert out["decision"] == "block", f"{tool_name} must still be blocked until routed"


def _write_routing_yaml(home: Path, content: str) -> Path:
    """Write a routing.yaml to the fake home's .llm_router directory."""
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
    """No env var + no routing.yaml → 'smart' default (F01): blocks the work tool
    (bare Bash) until routing is satisfied, for any task type."""
    for task_type, expected_tool in [("query", "llm_query"), ("code", "llm_code")]:
        session_id = f"sess-default-{task_type}"
        _write_pending(tmp_path, session_id, task_type=task_type, expected_tool=expected_tool)

        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": "Bash"},
            home=tmp_path,
            inject_default_mode=None,  # exercise the real resolver default (now smart)
        )

        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert out["decision"] == "block", (
            f"smart default must block bare Bash for {task_type} tasks until routed"
        )


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
    """routing.yaml exists but has no enforce: line → falls through to the 'smart' default (F01)."""
    _write_routing_yaml(tmp_path, "model_tier: auto\ndaily_budget: 5.00\n")
    session_id = "sess-yaml-no-enforce"
    _write_pending(tmp_path, session_id, task_type="query")

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
        inject_default_mode=None,  # no env → read yaml (no enforce) → smart default
    )

    assert result.returncode == 0
    # No enforce line → smart default → bare Bash is blocked until routed.
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
        # Test enforcement-logging behavior only. Without this, the hook attempts
        # real DIRECT execution (Ollama chain) in a subprocess, which under
        # full-suite memory pressure gets OOM-killed (returncode -9) — flaky
        # locally and red in CI (no Ollama). Disabling direct execution makes the
        # test hermetic and deterministic, matching test_auto_route_hook.py.
        extra_env={"LLM_ROUTER_DIRECT_EXECUTION": "0"},
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
    # Neutral framing (de-fanged): the prior-unrouted-turn notice names the task
    # and the tool it could have used, without "violation"/"escalated" language.
    assert "Last turn was not routed" in ctx
    assert route_tool("llm_query") in ctx and "query/simple" in ctx

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
    assert "Last turn was not routed" in ctx or "prior unrouted turn" in ctx


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


def test_hard_mode_exempts_filesystem_task(tmp_path):
    """Fix #1: a prompt that needs local files/shell is auto-exempted even in
    hard mode — a stateless routed model can't satisfy it, so blocking the
    native tool would just trap the user. Reuses needs_claude_tools()."""
    session_id = "sess-fs-exempt"
    _write_pending(
        tmp_path,
        session_id,
        task_type="query",
        expected_tool="llm_query",
        original_prompt="verify run_agent_loop in src/llm_router/hooks/agent_loop.py works",
    )

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash",
         "tool_input": {"command": "grep -n run_agent_loop src/llm_router/hooks/agent_loop.py"}},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )

    # Downgraded to soft → allowed, no block JSON on stdout, clean exit.
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "filesystem task must not be blocked in hard mode"


def test_hard_mode_still_blocks_pure_qa(tmp_path):
    """Control: a self-contained Q&A prompt (no local-file need) is NOT exempted
    and still blocks in hard mode, so routing still fires where it should."""
    session_id = "sess-qa-control"
    _write_pending(
        tmp_path,
        session_id,
        task_type="query",
        expected_tool="llm_query",
        original_prompt="what is the capital of France",
    )

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash",
         "tool_input": {"command": "echo hello"}},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )

    # Not exempt → hard mode blocks (stderr message + non-zero exit).
    assert result.returncode != 0 or (result.stdout.strip() and json.loads(result.stdout).get("decision") == "block"), \
        "pure Q&A should still be enforced in hard mode"


def test_hard_mode_exempts_local_git_write(tmp_path):
    """v0.8.3: a local git WRITE (push --delete / branch -d) is non-routable —
    no stateless model can perform it — so it must not block, even in hard mode
    and even on a terse follow-up prompt (the git-branch-delete drift class)."""
    session_id = "sess-local-git"
    _write_pending(tmp_path, session_id, task_type="coordination",
                   expected_tool="llm_query", original_prompt="yes, delete the merged branch")
    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash",
         "tool_input": {"command": "git push origin --delete fix/foo"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "local git write must not block in hard mode"


def test_hard_mode_exempts_local_dev_tools(tmp_path):
    """Package managers, test runners, and fs mutations are local ops → allowed."""
    for cmd in ("npm install", "pytest -q tests/", "mkdir -p build && touch build/x", "uv sync"):
        session_id = f"sess-dev-{abs(hash(cmd))}"
        _write_pending(tmp_path, session_id, task_type="coordination",
                       expected_tool="llm_query", original_prompt="go ahead")
        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": cmd}},
            home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )
        assert result.returncode == 0 and result.stdout.strip() == "", f"{cmd!r} should be exempt"


def test_hard_mode_still_blocks_network_fetch_bash(tmp_path):
    """Control: curl-to-URL is offloadable research work → stays route-blocked,
    so the exemption doesn't become a routing bypass."""
    session_id = "sess-curl"
    _write_pending(tmp_path, session_id, task_type="query",
                   expected_tool="llm_query", original_prompt="what's the latest news")
    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash",
         "tool_input": {"command": "curl https://example.com/api/news"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    blocked = bool(result.stdout.strip()) and json.loads(result.stdout).get("decision") == "block"
    assert blocked, "network-fetch Bash should still route in hard mode"


def test_hard_mode_exempts_edit_on_operational_task(tmp_path):
    """v0.8.3 drift class #2: an Edit on an operational (non-QA, non-code) task
    is a local file mutation — never routable — so it must not block, even in
    hard mode on a terse follow-up ("yes, do it")."""
    session_id = "sess-edit-op"
    _write_pending(tmp_path, session_id, task_type="coordination",
                   expected_tool="llm_query", original_prompt="yes, do it")
    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Edit",
         "tool_input": {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "Edit on operational task must not block"


def test_hard_mode_still_gates_write_on_code_task(tmp_path):
    """Control: CODE tasks keep the route-first gate — Write blocks until the
    llm_code call clears the lock. The Edit exemption must not weaken this."""
    session_id = "sess-write-code"
    _write_pending(tmp_path, session_id, task_type="code", complexity="moderate",
                   expected_tool="llm_code")
    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Write",
         "tool_input": {"file_path": "/tmp/y", "content": "x"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    blocked = bool(result.stdout.strip()) and json.loads(result.stdout).get("decision") == "block"
    assert blocked, "Write on a code task should keep the route-first gate"


def test_hard_mode_exempts_read_on_operational_task(tmp_path):
    """Native local inspection (Read/Grep/Glob/LS) on an operational task is
    non-routable → allowed, same as the mutation tools."""
    for tool in ("Read", "Grep", "Glob", "LS"):
        session_id = f"sess-read-{tool}"
        _write_pending(tmp_path, session_id, task_type="coordination",
                       expected_tool="llm_query", original_prompt="show me the branches")
        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool,
             "tool_input": {"file_path": "/tmp/x"}},
            home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )
        assert result.returncode == 0 and result.stdout.strip() == "", f"{tool} should be exempt"
