"""Tests for ``llm_router dev-refresh`` — the three-layer development refresh
subcommand.

The command runs in order:
  1. ``uv tool install --reinstall <source>`` — refreshes the package
  2. ``llm_router-install-hooks`` — copies updated hooks to ``~/.claude/hooks/``
  3. ``kill <mcp-server-pids>`` — sends SIGTERM to stale MCP servers

These tests pin the orchestration without actually running the
destructive subprocess calls — they mock them out and assert call
order, argument shape, and failure handling.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


def _import_cmd():
    from llm_router.commands.dev_refresh import cmd_dev_refresh
    return cmd_dev_refresh


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    """A minimal source dir with a pyproject.toml — passes the source
    detection guard so the test exercises the orchestration path."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"llm_router\"\n")
    return tmp_path


def test_help_returns_zero_without_running_anything(capsys) -> None:
    cmd = _import_cmd()
    with patch("subprocess.run") as mock_run:
        rc = cmd(["--help"])
    assert rc == 0
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    assert "dev-refresh" in captured.out.lower() or "refresh" in captured.out.lower()


def test_missing_source_returns_one(capsys, monkeypatch, tmp_path) -> None:
    cmd = _import_cmd()
    monkeypatch.delenv("LLM_ROUTER_DEV_SRC", raising=False)
    # Point llm_router.__file__ at a directory whose grandparent has no
    # pyproject.toml so the fallback detection fails.
    fake_dir = tmp_path / "fake_install" / "lib" / "site-packages" / "llm_router"
    fake_dir.mkdir(parents=True)
    (fake_dir / "__init__.py").write_text("")
    with patch("subprocess.run") as mock_run, patch("llm_router.__file__", str(fake_dir / "__init__.py")):
        rc = cmd([])
    assert rc == 1
    mock_run.assert_not_called()
    err = capsys.readouterr().err
    assert "source" in err.lower()


def test_dry_run_executes_no_subprocess_calls(source_tree, capsys) -> None:
    cmd = _import_cmd()
    with patch("subprocess.run") as mock_run, patch("subprocess.check_output") as mock_pgrep:
        mock_pgrep.return_value = ""
        rc = cmd(["--source", str(source_tree), "--dry-run"])
    assert rc == 0
    mock_run.assert_not_called()
    out = capsys.readouterr().out
    assert "dry run" in out.lower()


def test_full_refresh_runs_three_steps_in_order(source_tree, capsys) -> None:
    """Verify all three steps fire in the correct order with the
    expected argv shapes. Mocks subprocess so nothing real executes."""
    cmd = _import_cmd()

    # Mock subprocess.run to capture call argv.
    run_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        run_calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 0, stdout="Installed 5 executables: llm_router, ...", stderr=""
        )

    # Mock pgrep to return two stale server PIDs.
    fake_pgrep_out = (
        "11111 /Users/.../llm-routing/bin/python3 /Users/.../llm_router\n"
        "22222 /Users/.../llm-routing/bin/python3 /Users/.../llm_router\n"
    )
    killed: list[int] = []

    def fake_kill(pid, sig):
        killed.append(pid)

    with patch("subprocess.run", side_effect=fake_run), \
         patch("subprocess.check_output", return_value=fake_pgrep_out), \
         patch("os.kill", side_effect=fake_kill):
        rc = cmd(["--source", str(source_tree)])

    assert rc == 0
    # Step 1: uv tool install --reinstall <source>
    assert run_calls[0][:3] == ["uv", "tool", "install"]
    assert "--reinstall" in run_calls[0]
    assert str(source_tree) in run_calls[0]
    # Step 2: llm_router-install-hooks
    assert run_calls[1] == ["llm_router-install-hooks"]
    # Step 3: SIGTERM both stale servers
    assert killed == [11111, 22222]


def test_skip_mcp_kill_runs_first_two_steps_only(source_tree) -> None:
    cmd = _import_cmd()
    run_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        run_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run), \
         patch("subprocess.check_output") as mock_pgrep, \
         patch("os.kill") as mock_kill:
        rc = cmd(["--source", str(source_tree), "--skip-mcp-kill"])

    assert rc == 0
    assert len(run_calls) == 2
    mock_kill.assert_not_called()
    # pgrep should not even be invoked when MCP kill is skipped.
    mock_pgrep.assert_not_called()


def test_uv_install_failure_propagates_nonzero_and_skips_later_steps(
    source_tree, capsys,
) -> None:
    cmd = _import_cmd()

    call_count = {"n": 0}

    def fake_run(argv, **kwargs):
        call_count["n"] += 1
        # First call (uv tool install) fails; later calls should never happen.
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="uv: tool install failed"
        )

    with patch("subprocess.run", side_effect=fake_run), \
         patch("subprocess.check_output", return_value=""), \
         patch("os.kill") as mock_kill:
        rc = cmd(["--source", str(source_tree)])

    assert rc == 1
    assert call_count["n"] == 1  # bailed after step 1
    mock_kill.assert_not_called()
    err = capsys.readouterr().err
    assert "uv tool install failed" in err.lower()


def test_self_pid_excluded_from_mcp_kill_list(source_tree) -> None:
    """The dev-refresh process itself runs under the llm_router interpreter,
    so its parent (the shell) and possibly its own PID could match the
    MCP server pattern. Both must be excluded so we don't terminate the
    very process running the refresh.
    """
    cmd = _import_cmd()
    self_pid = os.getpid()
    parent_pid = os.getppid()
    # Pretend the process tree includes self and parent.
    fake_out = (
        f"{self_pid} ... llm-routing/bin/python3 ... llm_router\n"
        f"{parent_pid} ... llm-routing/bin/python3 ... llm_router\n"
        "99999 ... llm-routing/bin/python3 ... llm_router\n"
    )
    killed: list[int] = []

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run), \
         patch("subprocess.check_output", return_value=fake_out), \
         patch("os.kill", side_effect=lambda pid, sig: killed.append(pid)):
        rc = cmd(["--source", str(source_tree)])

    assert rc == 0
    assert self_pid not in killed
    assert parent_pid not in killed
    assert 99999 in killed


def test_cli_dispatch_routes_dev_refresh_to_cmd() -> None:
    """Source-level guard: cli.py must dispatch the ``dev-refresh`` arg
    to ``cmd_dev_refresh``."""
    cli_src = (
        Path(__file__).resolve().parent.parent / "src" / "llm_router" / "cli.py"
    ).read_text()
    assert 'args[0] == "dev-refresh"' in cli_src, (
        "dev-refresh subcommand dispatch missing from cli.py"
    )
    assert "from llm_router.commands.dev_refresh import cmd_dev_refresh" in cli_src, (
        "cli.py does not import cmd_dev_refresh — dispatch will fail at runtime"
    )
