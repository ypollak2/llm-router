"""The env-mutation window in `llm_local_task` must stay uninterruptible.

B4 of docs/ACTIONS_REMEDIATION_RUN.md. The external gap review filed this as a P0
cross-request race: `llm_local_task` mutates `os.environ`, runs, and restores in a
`finally`, so a concurrent request could observe the mutated environment.

Verified 2026-09-14: NOT reachable today. The MCP server dispatches cooperative
anyio tasks, and the function contains no `await` between the env set and the env
restore, so nothing else can be scheduled inside that window. The hook is a
separate OS process with its own environment entirely.

So there is no bug to fix — but the safety is accidental, resting on a property
nobody stated. The day someone makes `run_agent_loop` async, the window gains a
suspension point and the race becomes real, silently. This test states the
property so that change fails loudly instead.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from llm_router.tools import local_task as lt


def _function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_no_await_between_mutating_the_environment_and_restoring_it():
    tree = ast.parse(Path(lt.__file__).read_text())
    fn = _function(tree, "llm_local_task")

    sets = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Subscript)
            and isinstance(getattr(n, "value", None), ast.Attribute)
            and n.value.attr == "environ"]
    assert sets, "the env-mutation window has moved; this test needs updating"
    first, last = min(sets), max(sets)

    awaits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Await)
              and first <= n.lineno <= last]
    assert not awaits, (
        f"await at line(s) {awaits} sits inside the os.environ window "
        f"(lines {first}-{last}). On a cooperative event loop that is a real "
        "cross-request race: another task can observe LLM_ROUTER_AGENT_WRITES=apply "
        "that it never asked for. Either remove the suspension point or stop "
        "mutating process-global state and pass the capability explicitly."
    )


def test_the_environment_is_restored_even_when_the_body_raises(monkeypatch):
    """Was `"finally:" in inspect.getsource(...)` plus a text-slice-after-"finally:"
    membership check for each var name. A comment saying "finally:" (satisfying
    the first check) or repeating either var name after it (satisfying the
    second) would pass with no real restoration — the A-10 evasion again.
    This instead finds the real `ast.Try` node and requires a non-empty
    `finalbody`, then unparses ONLY that subtree (comments are not part of
    the AST, so they cannot appear here) and requires each var name to show
    up in the code that actually runs on the way out.
    """
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "propose")
    tree = ast.parse(inspect.getsource(lt.llm_local_task))
    fn = _function(tree, "llm_local_task")

    try_nodes = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert try_nodes, "no try/finally — an exception would leak the mutated env"
    finally_nodes = [n for n in try_nodes if n.finalbody]
    assert finally_nodes, "no finally — an exception would leak the mutated env"

    final_source = "\n".join(ast.unparse(s) for n in finally_nodes for s in n.finalbody)
    for var in ("LLM_ROUTER_AGENT_WRITES", "LLM_ROUTER_AGENT_COMMANDS"):
        assert var in final_source, f"{var} is not restored in the finally block"
