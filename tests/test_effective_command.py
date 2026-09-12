"""Compress by the command that PRODUCED the output, not the one that moved.

Measured on 989 real Bash outputs from live Claude Code transcripts: 710 of them
(72%) were classified as `cd`, because an agent's command is routinely
`cd /some/repo && git status` or a multi-line block opening with `cd`. The
filter was picked from the `cd`, so the git/pytest/grep filter that would have
compressed the output never ran — those calls saved 4.8% while a correctly
classified `sed` saved 36%.

After this, `cd` disappears from the strategy table entirely and total savings
go 9.0% -> 9.6% on the same transcripts.
"""
from __future__ import annotations

import pytest

from llm_router.compression.rtk_adapter import RTKAdapter, effective_command


@pytest.mark.parametrize("command,expected", [
    ("cd /repo && git status", "git status"),
    ("cd /repo; pytest -q", "pytest -q"),
    ("cd /a && cd /b && pytest -q", "pytest -q"),
    ("cd /repo\ngit status --short", "git status --short"),
    ("pushd /repo && git log", "git log"),
    ("env X=1 pytest tests", "pytest tests"),
    ("cd /r && env A=1 B=2 pytest -q", "pytest -q"),
    ("time git log", "git log"),
])
def test_the_positioning_prefix_is_stripped(command, expected):
    assert effective_command(command).split("\n")[0] == expected


@pytest.mark.parametrize("command", [
    "git log --oneline",
    "pytest -q",
    "cd /repo",          # a bare cd really is a cd
    "cdk deploy",        # not `cd`
])
def test_a_real_command_is_left_alone(command):
    assert effective_command(command) == command


def test_a_pipe_does_not_reclassify_the_output():
    """`git log | head` produces git-shaped output; the tail of a pipe does not
    decide what the text looks like."""
    assert effective_command("git log | head -5").startswith("git log")


@pytest.mark.parametrize("junk", ["", "   ", "&&", ";"])
def test_degenerate_input_never_raises(junk):
    effective_command(junk)


def test_a_long_prefix_chain_terminates():
    """Bounded, so a pathological command cannot spin."""
    assert effective_command("cd /a && " * 40 + "git status")


def test_the_adapter_now_reports_the_real_strategy():
    """The end-to-end point: the strategy name is what chose the filter."""
    adapter = RTKAdapter(enable=True)
    out = adapter.compress("cd /repo && git status",
                           "\n".join(f"line {i}" for i in range(40)))
    assert not out.strategy.startswith("cd:"), \
        f"still classified by the positioning command: {out.strategy}"
    assert out.strategy.startswith("git")


def test_compression_never_makes_output_larger():
    """A filter that inflates its input is worse than no filter — and the first
    measurement of this reported -7.3% because the harness stringified a result
    object. Instruments get this wrong too."""
    adapter = RTKAdapter(enable=True)
    for cmd in ("cd /r && git status", "pytest -q", "ls -la"):
        text = "\n".join(f"some output line {i}" for i in range(60))
        assert len(adapter.compress(cmd, text).output) <= len(text)
