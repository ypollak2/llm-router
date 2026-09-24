"""CFG-010: CLI subcommands must propagate their main()'s exit code.

Audit 2026-09-24 (05_config_cli.md, reproduced in 13_verify_routing_config.md):
`llm-router last` on an empty ledger printed its error and exited 0, because
cli.py called `_last_main(args[1:])` and discarded the return value — the same
class CHZ-PKG-005 fixed for `verify`. `retrospect`, `replay` and `snapshot`
had the identical dispatch. A script or CI gate keying on the exit code saw
success.
"""
from __future__ import annotations

import subprocess
import sys

import pytest


def _run(tmp_path, *argv):
    env = {"HOME": str(tmp_path), "LLM_ROUTER_HOME": str(tmp_path / ".llm-router"),
           "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, "-m", "llm_router.cli", *argv],
                          capture_output=True, text=True, env=env, timeout=120)


@pytest.mark.parametrize("cmd", ["last", "replay"])
def test_failure_on_a_missing_ledger_exits_nonzero(tmp_path, cmd):
    r = _run(tmp_path, cmd)
    assert "not found" in (r.stdout + r.stderr), "premise: the failure path ran"
    assert r.returncode == 1, (r.returncode, r.stdout[-300:])


def test_every_dispatch_propagates_the_return_code():
    """Structural: no `_x_main(args[1:])` whose return value is dropped."""
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "llm_router" / "cli.py"
    dropped = [
        n.lineno for n in ast.walk(ast.parse(src.read_text()))
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name) and n.value.func.id.endswith("_main")
        # Subcommand mains take argv and return an int; the zero-arg ones
        # (gateway, report, quickstart, MCP server) return None.
        and n.value.args
    ]
    assert not dropped, f"cli.py drops a subcommand's exit code at lines {dropped}"
