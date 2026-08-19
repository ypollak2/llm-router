"""S3: the agent loop's run_command must not use shell=True.

Locks in the fix so a regression (re-introducing shell=True) fails CI and lets
bandit run as a hard gate.
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "agent_loop.py"


def test_no_shell_true_anywhere_in_agent_loop():
    src = _SRC.read_text()
    assert "shell=True" not in src, "run_command must not spawn a shell (S3)"


def test_subprocess_run_calls_never_pass_shell_true():
    """AST-level: no subprocess.run(...) call passes shell=True."""
    tree = ast.parse(_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant):
                    assert kw.value.value is not True, "shell=True is forbidden (S3)"


def test_run_command_parses_with_shlex():
    """The command is split into an argv list before execution."""
    assert "shlex.split" in _SRC.read_text()
