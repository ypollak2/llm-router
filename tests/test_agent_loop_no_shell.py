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
    # The tokenizer moved to the toolkit (agent_loop.py calls it): one executor.
    assert _tokenizes_with_shlex((_SRC.parent.parent / "toolkit" / "tools.py").read_text())


def _hook_calls_the_executor(src: str) -> bool:
    """AST: the hook's _run_command_line really hands argv to the toolkit executor and tokenizes with
    the toolkit parser (not a lookalike that this file no longer contains)."""
    tree = ast.parse(src)
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    aliased = any(isinstance(n, ast.Assign) and ast.unparse(n.value) == "_kit.parse_command_line"
                  and any(getattr(t, "id", "") == "_parse_command_line" for t in n.targets) for n in ast.walk(tree))
    return "_kit.run_pipelines" in calls and aliased


def test_the_hook_path_executes_through_the_toolkit_executor():
    """The property above is asserted on tools.py ONLY because the hook calls it: prove the call."""
    assert _hook_calls_the_executor(_SRC.read_text())
    tools_src = (_SRC.parent.parent / "toolkit" / "tools.py").read_text()
    assert "shell=True" not in tools_src
    for node in ast.walk(ast.parse(tools_src)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                assert not (kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True)


def test_the_hook_path_runs_no_shell_at_runtime(tmp_path):
    """Behavioural, through the hook's own entry point: a shell would expand these, argv does not."""
    from llm_router.hooks import agent_loop
    out = agent_loop._run_command_line("echo $HOME `id`", tmp_path)
    assert "$HOME" in out and "uid=" not in out
    assert agent_loop._run_command_line("echo $(id)", tmp_path).startswith("REFUSED")
    assert (agent_loop._run_command_line("echo a > out.txt", tmp_path).startswith("REFUSED")
            and not (tmp_path / "out.txt").exists())


def test_the_ast_check_is_not_vacuous():
    assert not _hook_calls_the_executor("def _run_command_line(c, r):\n    return 1\n")


def _tokenizes_with_shlex(src: str) -> bool:
    """AST, not text (K7): a call to shlex.split or shlex.shlex exists. S
    (2026-09-24) moved run_command to a shlex.shlex tokenizer so `&&`/`|` are
    split and chained without a shell; either form keeps the argv guarantee."""
    import ast as _ast
    return any(isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
               and n.func.attr in ("split", "shlex")
               and getattr(n.func.value, "id", "") == "shlex"
               for n in _ast.walk(_ast.parse(src)))
