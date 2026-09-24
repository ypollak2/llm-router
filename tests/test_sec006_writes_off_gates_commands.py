"""SEC-006: LLM_ROUTER_AGENT_WRITES=off/propose must also hold for run_command.

Audit 2026-09-24 (10_security_privacy.md SEC-006, reproduced in
13_verify_security_deps.md): with AGENT_WRITES=off, write_file was refused but
`python3 -c "open(path,'w')..."` through run_command wrote a file OUTSIDE the
project root. The write setting silently covered only one of the two ways the
loop can write. When writes are not applied, the command guard now refuses the
argv shapes that write: inline interpreter code, in-place sed, find actions.

This does not make run_command a sandbox (SEC-001, disclosed in SECURITY.md):
with AGENT_WRITES=apply these shapes are allowed exactly as before.
"""
from __future__ import annotations

import pytest

from llm_router.hooks.agent_writes import guard_command

WRITES = [
    ["python3", "-c", "open('/tmp/x','w').write('pwned')"],
    ["python", "-c", "import os"],
    ["node", "-e", "require('fs').writeFileSync('/tmp/x','p')"],
    ["node", "--eval", "1"],
    ["node", "-p", "1"],
    ["sed", "-i", "s/a/b/", "src/a.py"],
    ["sed", "--in-place", "s/a/b/", "src/a.py"],
    ["sed", "-i.bak", "s/a/b/", "src/a.py"],
    ["find", ".", "-name", "*.pyc", "-delete"],
    ["find", ".", "-exec", "rm", "{}", ";"],
]

READS = [
    ["python3", "-m", "pytest", "-q"],
    ["sed", "-n", "1,10p", "src/a.py"],
    ["find", ".", "-name", "*.py"],
    ["git", "status"],
    ["cat", "README.md"],
]


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_AGENT_COMMANDS", raising=False)


@pytest.mark.parametrize("mode", ["off", "propose"])
@pytest.mark.parametrize("argv", WRITES, ids=lambda a: " ".join(a)[:40])
def test_writing_commands_are_refused_when_writes_are_not_applied(monkeypatch, mode, argv):
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", mode)
    ok, why = guard_command(argv)
    assert not ok, f"{argv} ran with AGENT_WRITES={mode}"
    assert "AGENT_WRITES" in why, why  # the refusal names the setting that caused it


@pytest.mark.parametrize("mode", ["off", "propose"])
@pytest.mark.parametrize("argv", READS, ids=lambda a: " ".join(a)[:40])
def test_reading_commands_still_run(monkeypatch, mode, argv):
    """Premise: the guard did not become 'refuse everything'."""
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", mode)
    ok, why = guard_command(argv)
    assert ok, why


@pytest.mark.parametrize("argv", WRITES, ids=lambda a: " ".join(a)[:40])
def test_apply_mode_is_unchanged(monkeypatch, argv):
    """With writes applied the guard behaves exactly as before (SEC-001 scope)."""
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "apply")
    ok, _ = guard_command(argv)
    assert ok


def test_the_reproduction_writes_nothing(monkeypatch, tmp_path):
    """End to end through execute_tool, as the verifier reproduced it."""
    from llm_router.hooks.agent_loop import execute_tool
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "off")
    outside = tmp_path / "outside.txt"
    proj = tmp_path / "proj"
    proj.mkdir()
    out = execute_tool("run_command",
                       {"command": f"python3 -c \"open('{outside}','w').write('x')\""}, proj)
    assert not outside.exists(), out
    assert "REFUSED" in out
