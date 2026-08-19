"""Three trap-prevention guards in enforce-route.py.

Built after a session where a strong-confidence ``code-context-inherit``
route confidently misrouted a documentation-Edit prompt to ``llm_code``
and the user couldn't reach Edit/Write/Bash without burning a useless
``llm_*`` call. These guards prevent recurrence:

* **Pattern 1** — code-context-inherit downgrades to soft, joining
  heuristic-weak in the "uncertain methods" bucket.
* **Pattern 2** — prompt-shape sanity check: when the original prompt
  structurally looks like an Edit task (``add X to file.ext``,
  ``update docstring in Y``, ``write a section for Z``), the directive
  downgrades to soft regardless of classifier confidence — user
  intent wins.
* **Pattern 3** — same-tool-blocked-twice-in-one-turn auto-pivot:
  faster trigger than the existing 4-violation pivot, so the agent
  doesn't visibly stall before the escape valve fires.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENFORCE_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _run_hook(payload: dict, *, home: Path,
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


def _write_pending(home: Path, session_id: str, **overrides) -> None:
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "expected_tool": "llm_code",
        "task_type": "code",
        "complexity": "simple",
        "method": "heuristic",
        "issued_at": time.time(),
        "turn_id": 12345,
        "session_id": session_id,
    }
    data.update(overrides)
    (router_dir / f"pending_route_{session_id}.json").write_text(json.dumps(data))


def _log(home: Path) -> str:
    log = home / ".llm-router" / "enforcement.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


# ── Pattern 1 · code-context-inherit downgrades to soft ─────────────────


def test_code_context_inherit_downgrades_to_soft(tmp_path):
    """Just like heuristic-weak — write-Bash goes through, no block.

    Using a non-readonly Bash command so the smart-mode read-only
    exception doesn't fire first. The fact that THIS write Bash gets
    through is what proves the soft downgrade is firing.
    """
    session_id = "sess-cci-1"
    _write_pending(tmp_path, session_id, method="code-context-inherit")
    result = _run_hook(
        {"session_id": session_id,
         "tool_name": "Bash",
         "tool_input": {"command": "rm -rf /tmp/sess-cci-1-scratch"}},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "smart"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"code-context-inherit should not produce a block; "
        f"stdout: {result.stdout!r}"
    )
    assert "outcome=ALLOWED(soft)" in _log(tmp_path)


def test_code_context_inherit_downgrades_strict_too(tmp_path):
    """Even strict mode should soft-downgrade for uncertain methods —
    operators chose strict to discipline confident misroutes, not
    uncertain inheritance."""
    session_id = "sess-cci-strict"
    _write_pending(tmp_path, session_id, method="code-context-inherit")
    result = _run_hook(
        {"session_id": session_id, "tool_name": "Bash",
         "tool_input": {"command": "rm -rf /tmp/sess-cci-strict-scratch"}},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "strict"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_strong_heuristic_still_blocks_for_baseline(tmp_path):
    """Sanity: strong methods (heuristic, ollama, api) keep hard-blocking."""
    session_id = "sess-strong-baseline"
    _write_pending(tmp_path, session_id, method="heuristic")
    result = _run_hook(
        {"session_id": session_id, "tool_name": "Bash",
         "tool_input": {"command": "rm -rf /tmp/scratch"}},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    out = json.loads(result.stdout)
    assert out["decision"] == "block"


# ── Pattern 2 · prompt-shape sanity check ──────────────────────────────


@pytest.mark.parametrize("prompt", [
    "add a new section to README.md",
    "Add the explainer to llm_router doctor",
    "update the docstring in router.py to mention the new flag",
    "modify CHANGELOG.md to call out v0.2",
    "in test_router.py, add a regression for the new helper",
    "fix the typo in CONTRIBUTING.md",
    "document the new --posture flag in doctor.py",
])
def test_edit_shape_overrides_hard_block(tmp_path, prompt):
    """When the prompt's STRUCTURE says 'I want to edit a file', the
    enforcer must downgrade to soft regardless of classifier method."""
    session_id = f"sess-shape-{abs(hash(prompt))}"
    _write_pending(
        tmp_path, session_id,
        method="heuristic",                 # strong method
        original_prompt=prompt,             # the shape signal
    )
    result = _run_hook(
        {"session_id": session_id, "tool_name": "Edit",
         "tool_input": {"file_path": "foo.py", "old_string": "x",
                        "new_string": "y"}},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"edit-shape prompt should override hard-block: {prompt!r}; "
        f"got: {result.stdout!r}"
    )
    assert "SHAPE_OVERRIDE" in _log(tmp_path)


@pytest.mark.parametrize("prompt", [
    "What is the capital of France?",
    "Show me how decorators work in Python",
    "Generate a regex that validates emails",
    "Explain B-trees vs LSM-trees for write-heavy workloads",
    "Write a poem about confluence",
])
def test_non_edit_shapes_still_hard_block(tmp_path, prompt):
    """False-positive guard: pure question / generation prompts must
    still hard-block, otherwise the override becomes a routing-evasion
    loophole."""
    session_id = f"sess-genuine-{abs(hash(prompt))}"
    _write_pending(
        tmp_path, session_id, method="heuristic", original_prompt=prompt,
    )
    result = _run_hook(
        {"session_id": session_id, "tool_name": "Bash",
         "tool_input": {"command": "rm -rf /tmp/x"}},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    out = json.loads(result.stdout)
    assert out["decision"] == "block", (
        f"non-edit prompt must still block: {prompt!r}"
    )


# ── Pattern 3 · same-tool 2-blocks-in-one-turn auto-pivot ─────────────


def test_two_same_turn_blocks_trigger_autopivot(tmp_path):
    """First block fires; second block (same tool, same turn) auto-pivots."""
    session_id = "sess-trap-1"
    _write_pending(tmp_path, session_id, method="heuristic")

    # Try a non-edit-shaped Bash twice within the same turn (no shape
    # override, no soft method).
    payload = {
        "session_id": session_id, "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /tmp/llm_router-trap"},
    }
    r1 = _run_hook(payload, home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"})
    assert json.loads(r1.stdout)["decision"] == "block"

    # Re-write pending with the SAME turn_id so the second hit counts.
    _write_pending(tmp_path, session_id, method="heuristic", turn_id=12345)
    r2 = _run_hook(payload, home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"})
    # The auto-pivot fires — no block payload this time.
    assert r2.returncode == 0
    assert r2.stdout.strip() == "", (
        f"second same-tool block in same turn should auto-pivot; "
        f"got: {r2.stdout!r}"
    )
    assert "AUTO-PIVOT (trap)" in _log(tmp_path)


def test_counter_resets_on_new_turn(tmp_path):
    """A block in turn A must NOT count toward the auto-pivot for turn B."""
    session_id = "sess-trap-reset"
    _write_pending(tmp_path, session_id, method="heuristic", turn_id=111)
    payload = {
        "session_id": session_id, "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /tmp/llm_router-trap"},
    }
    r1 = _run_hook(payload, home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"})
    assert json.loads(r1.stdout)["decision"] == "block"

    # New turn — different turn_id. The first block of THIS turn must
    # still hard-block; the old counter must not carry over.
    _write_pending(tmp_path, session_id, method="heuristic", turn_id=222)
    r2 = _run_hook(payload, home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"})
    assert json.loads(r2.stdout)["decision"] == "block", (
        "block counter should NOT carry across turns"
    )


def test_different_tool_does_not_inherit_count(tmp_path):
    """Blocking Bash then trying Edit — Edit's counter is independent."""
    session_id = "sess-trap-multi-tool"
    _write_pending(tmp_path, session_id, method="heuristic", turn_id=12345)

    r1 = _run_hook(
        {"session_id": session_id, "tool_name": "Bash",
         "tool_input": {"command": "rm -rf /tmp/llm_router-trap"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    assert json.loads(r1.stdout)["decision"] == "block"

    # Now a different tool — its same-turn count is 1, not 2.
    _write_pending(tmp_path, session_id, method="heuristic", turn_id=12345)
    r2 = _run_hook(
        {"session_id": session_id, "tool_name": "Edit",
         "tool_input": {"file_path": "x.py", "old_string": "a", "new_string": "b"}},
        home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )
    out = json.loads(r2.stdout)
    assert out["decision"] == "block", (
        "Edit's counter is independent of Bash's; first Edit block must "
        "still fire"
    )


def test_strict_mode_disables_trap_autopivot(tmp_path):
    """Strict opts out of all escape valves — same-tool repeats stay
    blocked."""
    session_id = "sess-trap-strict"
    _write_pending(tmp_path, session_id, method="heuristic", turn_id=12345)
    payload = {
        "session_id": session_id, "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /tmp/llm_router-trap"},
    }
    r1 = _run_hook(payload, home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "strict"})
    assert json.loads(r1.stdout)["decision"] == "block"

    _write_pending(tmp_path, session_id, method="heuristic", turn_id=12345)
    r2 = _run_hook(payload, home=tmp_path, extra_env={"LLM_ROUTER_ENFORCE": "strict"})
    # Strict ignores the trap-detection — second block still fires.
    assert json.loads(r2.stdout)["decision"] == "block", (
        "strict mode must disable the trap auto-pivot"
    )


# ── Helper unit tests ───────────────────────────────────────────────────


def test_looks_like_edit_task_via_import():
    """Direct unit test on the helper so refactors can't quietly remove
    Pattern 2's regex without breaking a test."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_enforce_route_helper", ENFORCE_ROUTE_HOOK,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Positive shapes
    for prompt in [
        "add a section to README.md",
        "update the docstring in router.py",
        "Add the explainer to llm_router doctor",
        "in CHANGELOG.md, add a v0.2 entry",
    ]:
        assert module._looks_like_edit_task(prompt), prompt

    # Negative shapes (must stay non-Edit)
    for prompt in [
        "What is the capital of France",
        "Generate a regex for emails",
        "Show me how lists work",
        "",
    ]:
        assert not module._looks_like_edit_task(prompt), prompt
