"""Tests for routing enforcement behavior in the shipped hook scripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


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
    assert "Tool blocked:  Bash" in out["reason"]


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


def test_enforce_route_blocks_file_tools_for_qa_tasks(tmp_path):
    """Glob/Read/Grep/LS are blocked for Q&A tasks — reading files is equivalent to self-answering."""
    for tool_name in ("Read", "Glob", "Grep", "LS"):
        session_id = f"sess-qa-{tool_name.lower()}"
        _write_pending(tmp_path, session_id, task_type="query")

        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool_name},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )

        assert result.returncode == 0, f"{tool_name} should be blocked for query tasks"
        out = json.loads(result.stdout)
        assert out["decision"] == "block"
        assert tool_name in out["reason"]


def test_enforce_route_allows_file_tools_for_code_tasks(tmp_path):
    """Glob/Read/Grep/LS are allowed for code tasks — needed to find files before editing."""
    session_id = "sess-code-read-allowed"
    _write_pending(tmp_path, session_id, task_type="code", expected_tool="llm_code")

    for tool_name in ("Read", "Glob", "Grep", "LS"):
        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool_name},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "", f"{tool_name} should be allowed for code tasks"


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
    assert "Tool blocked:  Bash" in out["reason"]


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

    # Smart mode allows Bash for code tasks
    session_id_code = "sess-default-code"
    _write_pending(tmp_path, session_id_code, task_type="code", expected_tool="llm_code")

    result_code = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id_code, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result_code.returncode == 0
    assert result_code.stdout.strip() == "", "Smart default must allow Bash for code tasks"


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
    ctx = out["hookSpecificOutput"]["contextForAgent"]
    assert "PREVIOUS TURN VIOLATED ROUTING" in ctx
    assert "expected llm_query for query/simple" in ctx
    new_pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert new_pending["issued_at"] > old_pending["issued_at"]
    assert new_pending["task_type"] != old_pending["task_type"]

    log_text = (tmp_path / ".llm-router" / "enforcement.log").read_text(encoding="utf-8")
    assert "NO_ROUTE" in log_text
    assert "expected=llm_query" in log_text
    assert "task=query/simple" in log_text
    assert "prior unrouted turn" in out["systemMessage"]
