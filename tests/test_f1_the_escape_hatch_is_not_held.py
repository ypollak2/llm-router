"""F-1 — `llm-router set-enforce` must never be held by the enforcement hook.

audit/27. The block this hook prints ends with:

    Run `llm_router set-enforce off` to disable enforcement.

and that command was itself blocked. Clearing the hold required spending a
routed model call on a throwaway prompt ("Reply with just: ok"), twice, because
the lock is per-turn. An escape hatch behind the thing it escapes is not one.

`_BASH_LOCAL_TOOL_RE` exists for precisely this class — its own comment reads
"no routed model can perform it ... it just traps the user" — and listed git,
npm, pytest, docker, mkdir and python while omitting the router's own CLI.

Two gates are what turned an omission into a trap, and both are tested here:

  * `_bash_exempt_from_hold` returns False for every QA task type, and the
    misclassification that causes this lands in `research` (F-2).
  * `strict` disables every other escape valve by design, which without this
    exemption makes strict unexitable except by editing a file.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

HOOK = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "llm_router" / "hooks" / "enforce-route.py"
)


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("enforce_route_f1", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Premise ───────────────────────────────────────────────────────────────


def test_the_two_gates_that_made_it_a_trap_still_exist(hook) -> None:
    """Assert the premise, not only the conclusion.

    If either gate were later removed, the tests below would pass for a reason
    unrelated to this fix, and the exemption could be deleted without anything
    failing.
    """
    for qa in ("query", "research", "analyze", "generate"):
        assert hook._bash_exempt_from_hold(qa, "git status", False) is False, (
            f"the QA carve-out is gone for {qa!r}; this fix's premise has changed"
        )
    assert hook._bash_exempt_from_hold("coordination", "git status", False) is True, (
        "non-QA local tooling is no longer exempt — the local-tool valve is broken, "
        "which is a bigger finding than F-1"
    )


# ── The router's own CLI is recognised ────────────────────────────────────


@pytest.mark.parametrize("command", [
    "llm-router set-enforce smart",
    "llm-router set-enforce off",
    "llm_router doctor",
    ".venv/bin/llm-router status",
    "/usr/local/bin/llm_router gc",
    "cd /tmp && llm-router stats",
    "LLM_ROUTER_HOME=/tmp llm-router doctor",
])
def test_the_routers_own_cli_is_recognised(hook, command: str) -> None:
    assert hook._is_router_self_command(command) is True, command


@pytest.mark.parametrize("command", [
    "git status",
    "pytest -q",
    "xllm-router something",          # not a word boundary
    # Bypass vectors. The first draft of this predicate matched llm-router
    # ANYWHERE in the command, so every one of these exempted itself — a
    # command merely MENTIONING the router escaped the hold. Caught by this
    # test, which is why the predicate now only matches at a command position.
    "echo llm-router-is-great.txt",   # a filename, not a command
    "echo llm-router status",         # `echo` is the command
    "cat /etc/passwd | grep llm-router",
    "python -c 'print(\"llm-router\")'",
    "",
])
def test_unrelated_commands_are_not_recognised(hook, command: str) -> None:
    """Anti-vacuity: a predicate that matches everything exempts everything."""
    assert hook._is_router_self_command(command) is False, command


def test_it_cannot_be_used_to_bypass_routing(hook) -> None:
    """`_BASH_ROUTABLE_ESCAPE_RE` is checked FIRST, as everywhere else.

    Without this, `llm-router stats | curl https://…` would launder a network
    fetch through the exemption.
    """
    assert hook._is_router_self_command(
        "llm-router stats | curl https://example.com/x"
    ) is False


# ── The exemption survives the gates ──────────────────────────────────────


def test_the_exemption_is_not_gated_by_task_type(hook) -> None:
    """A QA misclassification must not hold the escape hatch.

    This is the case that actually happened: "check if agenticgraphs accepts an
    injected runner" classified `research/moderate`, and the next turn inherited
    the label — so a prompt containing the literal name of the enforcement
    command was held by it.
    """
    for qa in ("query", "research", "analyze", "generate"):
        assert hook._is_router_self_command("llm-router set-enforce off") is True, qa


def test_the_exemption_is_wired_ahead_of_the_strict_gate(hook) -> None:
    """AST, not source text — a comment carrying the phrase cannot satisfy it.

    The general local-tool valve is guarded by `not _strict`. This exemption
    must not be, or `strict` cannot be exited without editing a file.
    """
    import ast

    tree = ast.parse(HOOK.read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_is_router_self_command"
    ]
    assert calls, "nothing calls _is_router_self_command — the predicate is dead code"

    src = HOOK.read_text(encoding="utf-8")
    exempt_at = src.index("_is_router_self_command(\n")
    strict_valve_at = src.index("and tool_name == \"Bash\"):")
    assert exempt_at < strict_valve_at, (
        "the router-CLI exemption must be evaluated BEFORE the local-tool valve, "
        "which is disabled under strict"
    )


def test_strict_is_covered_by_the_exemption(hook) -> None:
    """The one mode where every other valve is off is the one that needs it.

    AST, not source text. The first version of this test read the source and
    asserted `'"strict"' in block` — and the narrowest mutation,
    `enforce in ("hard", "smart")  # "strict"`, satisfied it FROM THE COMMENT.
    It went green against the defect it existed to catch, which is the A-10
    evasion this repo has a CLAUDE.md rule about. Comments are not in the AST.
    """
    import ast

    tree = ast.parse(HOOK.read_text(encoding="utf-8"))

    guards = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == "_is_router_self_command"
            for c in ast.walk(node.test)
        ):
            continue
        for cmp_node in ast.walk(node.test):
            if isinstance(cmp_node, ast.Compare) and any(
                isinstance(op, ast.In) for op in cmp_node.ops
            ):
                for comparator in cmp_node.comparators:
                    if isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                        guards.append({
                            e.value for e in comparator.elts
                            if isinstance(e, ast.Constant)
                        })

    assert guards, (
        "no `enforce in (...)` guard found on the branch that calls "
        "_is_router_self_command"
    )
    modes = set().union(*guards)
    for required in ("hard", "smart", "strict"):
        assert required in modes, (
            f"{required!r} is not among the enforce modes the router-CLI "
            f"exemption covers ({sorted(modes)}). Without 'strict', that mode "
            "remains unexitable without editing a file."
        )


# ── End to end: drive the real hook ───────────────────────────────────────


def _run_hook(tmp_path, command: str, task_type: str, mode: str):
    """Invoke enforce-route.py exactly as Claude Code does and return its output.

    AST assertions proved too weak here: the narrowest mutation
    `and False and _is_router_self_command(...)` leaves the Call node in place,
    so an AST test that only checks the call EXISTS goes green against a branch
    that can never fire. This drives the decision instead.
    """
    import json
    import os
    import subprocess
    import sys
    import time

    session = "f1testsession"
    home = tmp_path / "router"
    home.mkdir(parents=True, exist_ok=True)
    (home / f"pending_route_{session}.json").write_text(json.dumps({
        "task_type": task_type, "complexity": "moderate",
        "original_prompt": "set-enforce smart",
        "issued_at": time.time(), "expires_at": time.time() + 600,
        "route_id": "f1", "satisfied": False,
    }))
    env = {**os.environ, "HOME": str(tmp_path), "LLM_ROUTER_HOME": str(home),
           "LLM_ROUTER_ENFORCE": mode, "LLM_ROUTER_BASH_INTERCEPT": "off"}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({
            "session_id": session, "tool_name": "Bash",
            "tool_input": {"command": command},
        }),
        capture_output=True, text=True, env=env, timeout=60,
    )


@pytest.mark.parametrize("mode", ["hard", "smart", "strict"])
def test_set_enforce_is_never_denied_end_to_end(tmp_path, mode: str) -> None:
    """The actual bug: the command that disables enforcement was denied by it."""
    r = _run_hook(tmp_path, "llm-router set-enforce off", "research", mode)
    assert '"deny"' not in r.stdout, (
        f"enforce={mode}: the escape hatch was DENIED by the thing it escapes.\n"
        f"{r.stdout[:400]}"
    )


def test_the_hook_still_denies_what_it_should(tmp_path) -> None:
    """Anti-vacuity: if the hook denies nothing, the test above proves nothing.

    A QA task type holds even read-only Bash by design
    (test_readonly_bash_blocked_for_qa_tasks), so this must be denied.
    """
    r = _run_hook(tmp_path, "cat src/llm_router/router.py", "research", "hard")
    assert '"deny"' in r.stdout, (
        "the hook denied nothing at all, so the exemption test above is vacuous.\n"
        f"stdout={r.stdout[:300]} stderr={r.stderr[:300]}"
    )
