"""D-154 (2): a cancelled Gemini CLI / Claude Code call must not leave its subprocess running.

The MCP door's deadline (D-141) cancels the routed call. ``codex_agent`` already killed its
process group on ``CancelledError``; ``gemini_cli_agent`` and ``claude_agent`` did not, and
the F7 reviewer saw the CLI child alive (state S, ppid 1) 2.5 s after the deadline. These
tests run each agent against a fake CLI that records its pid and its own child's pid, then
sleeps; they cancel the call and require both processes to be gone shortly after.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process-group kill is POSIX")


def _fake_cli(tmp_path: Path) -> tuple[Path, Path, Path]:
    pid_file, grandchild_file = tmp_path / "cli.pid", tmp_path / "grandchild.pid"
    script = tmp_path / "fake-cli"
    script.write_text(
        "#!/bin/sh\n"
        f"echo $$ > '{pid_file}'\n"
        "sleep 60 &\n"
        f"echo $! > '{grandchild_file}'\n"
        "wait\n"
    )
    script.chmod(0o755)
    return script, pid_file, grandchild_file


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return stat == "" or stat.startswith("Z")


async def _wait_for(path: Path, timeout: float = 10.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        await asyncio.sleep(0.05)
    raise AssertionError(f"fake CLI never wrote {path.name}")


async def _cancel_and_check(start_call, pid_file: Path, grandchild_file: Path) -> None:
    task = asyncio.create_task(start_call())
    pid = await _wait_for(pid_file)
    grandchild = await _wait_for(grandchild_file)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    end = time.monotonic() + 5.0
    while time.monotonic() < end and not (_gone(pid) and _gone(grandchild)):
        await asyncio.sleep(0.1)
    assert _gone(pid), f"CLI process {pid} still running after cancel"
    assert _gone(grandchild), f"CLI child process {grandchild} still running after cancel"


@pytest.mark.asyncio
async def test_cancelled_gemini_cli_call_kills_the_cli_tree(tmp_path, monkeypatch):
    from llm_router import gemini_cli_agent

    script, pid_file, grandchild_file = _fake_cli(tmp_path)
    monkeypatch.setattr(gemini_cli_agent, "find_gemini_binary", lambda: str(script))
    await _cancel_and_check(
        lambda: gemini_cli_agent.run_gemini_cli("hi", model="m", working_dir=str(tmp_path)),
        pid_file, grandchild_file)


@pytest.mark.asyncio
async def test_cancelled_claude_cli_call_kills_the_cli_tree(tmp_path, monkeypatch):
    from llm_router import claude_agent

    script, pid_file, grandchild_file = _fake_cli(tmp_path)
    monkeypatch.setattr(claude_agent, "find_claude_binary", lambda: str(script))
    await _cancel_and_check(
        lambda: claude_agent.run_claude("hi", model="sonnet", working_dir=str(tmp_path)),
        pid_file, grandchild_file)


def test_the_fake_cli_outlives_its_parent_without_the_fix(tmp_path):
    """Premise: the fake CLI's child really survives a plain kill of the CLI alone, so the
    tests above measure the group kill and not just the shell exiting on its own."""
    script, pid_file, grandchild_file = _fake_cli(tmp_path)
    proc = subprocess.Popen([str(script)], start_new_session=True)
    try:
        end = time.monotonic() + 10
        while time.monotonic() < end and not grandchild_file.exists():
            time.sleep(0.05)
        grandchild = int(grandchild_file.read_text().strip())
        proc.kill()
        proc.wait(5)
        time.sleep(0.3)
        assert not _gone(grandchild), "premise failed: the child died with the CLI"
    finally:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
