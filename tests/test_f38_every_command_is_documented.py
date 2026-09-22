"""Every dispatchable subcommand must appear in `--help` — T-19.

Measured during the audit: **28 of 51 subcommands were absent from `--help`**,
and 22 appeared in no documentation anywhere. A command that ships, dispatches
and does real work but cannot be discovered is only marginally better than one
that does not exist — and the gap grows silently, because adding a dispatch
branch is one line and nothing checks for the matching help line.

This test is the check. It derives the command list from the dispatcher itself,
so a new command must be documented or the suite goes red.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _dispatchable() -> set[str]:
    """Subcommand names `cli.main` actually branches on."""
    src = (ROOT / "src" / "llm_router" / "cli.py").read_text(encoding="utf-8")
    names = set(re.findall(r'args\[0\] == "([a-z0-9-]+)"', src))
    for group in re.findall(r"args\[0\] in \(([^)]*)\)", src):
        names |= {t.strip().strip('"').strip("'") for t in group.split(",") if t.strip()}
    return {n for n in names if n and not n.startswith("-")}


def _help_text() -> str:
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['llm-router','--help'];"
         "from llm_router.cli import main; main()"],
        capture_output=True, text=True, cwd=str(ROOT),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"),
             "NO_COLOR": "1", "HOME": str(Path.home())},
    )
    return proc.stdout + proc.stderr


def test_every_dispatchable_command_appears_in_help():
    commands = _dispatchable()
    help_text = _help_text()
    missing = sorted(
        c for c in commands
        if not re.search(rf"llm-router\s+{re.escape(c)}(\s|$)", help_text, re.M)
    )
    assert not missing, (
        f"{len(missing)} dispatchable command(s) are absent from `--help`: "
        f"{missing}\n\nAdd a line for each to the module docstring in "
        "src/llm_router/cli.py. T-19 found 28 such commands."
    )


def test_the_scan_finds_the_commands_at_all():
    """Anti-vacuity: an empty command set passes the test above on air."""
    commands = _dispatchable()
    assert len(commands) >= 40, (
        f"the dispatcher scan found only {len(commands)} commands "
        f"({sorted(commands)[:5]}…) — the regex has stopped matching cli.py"
    )
    for expected in ("install", "doctor", "status", "gateway"):
        assert expected in commands, f"{expected!r} missing — the scan is wrong"


def test_help_actually_rendered():
    """And that `--help` produced something to search."""
    text = _help_text()
    assert "llm-router install" in text
    assert len(text) > 500, "help output is suspiciously short"
