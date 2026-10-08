"""GE1 action census: the tool-class table (``proxy.tool_classes``).

A technical operation is a step that only reads: Grep, Glob, LS, Read, NotebookRead,
TodoRead, or a Bash command every part of which is on the read-only list. Anything
that could write, run code or reach another command is ``exec``. The list is
conservative on purpose: a false ``technical_op`` would let a local model take a
step that changes the user's files.
"""
from __future__ import annotations

import pytest

from llm_router.proxy import tool_classes as tc

READ_ONLY = [
    "git status",
    "git status --short",
    "git log --oneline -5",
    "git -C /repo log -3",
    "git --no-pager diff HEAD~1",
    "git show HEAD:src/a.py",
    "git branch",
    "git branch -a",
    "git branch --show-current",
    "git blame src/a.py",
    "ls -la",
    "ls",
    "cat README.md",
    "head -50 src/a.py",
    "tail -n 20 log.txt",
    "wc -l src/*.py",
    "find . -name '*.py'",
    "rg -n 'def main' src",
    "grep -rn foo src | head -20",
    "tree -L 2",
    "cd /repo && git status",
    "cd /repo; ls",
    "git diff 2>&1 | head",
    "ls missing 2>/dev/null || ls",
    "LC_ALL=C grep -c x file",
    "git diff --no-ext-diff",
    "ls >/dev/null; pwd",
    "black --check src",
    "ruff format --check src",
    "isort --check-only src",
    "prettier --check .",
    "cargo fmt --check",
    "/usr/bin/grep -n x f",
    "pwd",
]

EXEC = [
    "rm -rf build",
    "python3 script.py",
    "pytest -q",
    "git commit -m x",
    "git push",
    "git checkout main",
    "git branch -D old",
    "git branch newname",
    "git diff --output=patch.txt",
    "ls > files.txt",
    "cat a >> b",
    "echo hi",
    "cd /repo && make",
    "git status && rm x",
    "ls; python3 x.py",
    "grep x f | xargs rm",
    "find . -name '*.pyc' -delete",
    "find . -exec rm {} \\;",
    "cat $(ls)",
    "cat `ls`",
    "ls &",
    "tree -o out.txt",
    "rg --pre ./unzip-and-run pattern",  # --pre runs a command on every file it searches
    "rg --pre=sh pattern src",
    "black src",
    "ruff check --fix src",
    "ruff format src",
    "sed -i s/a/b/ f",
    "cat <<EOF > f\nx\nEOF",
    "",
    "   ",
    "git",
    "head 'unterminated",
    "diff <(ls a) <(ls b)",
    # Programs that only read, made to run another program or to write (review of #314, 2026-10-08).
    "GIT_EXTERNAL_DIFF=/tmp/x git diff",    # the env prefix makes git run /tmp/x
    "GIT_PAGER=sh git log",
    "RIPGREP_CONFIG_PATH=/tmp/rc rg x",     # a config file can add --pre
    "git diff --ext-diff",
    "git log -p --ext-diff",
    "git show --textconv HEAD",
    "prettier --check --write .",
    "prettier --check -w .",
    "prettier --check --write=src .",
    "ruff format --check --fix src",
    "ls > /dev/nullx",                      # a file named /dev/nullx, not /dev/null
    "ls 2>/dev/null.log",
]


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only_bash_is_a_technical_op(command):
    assert tc.classify_bash(command) == tc.TECHNICAL_OP


@pytest.mark.parametrize("command", EXEC)
def test_anything_that_could_write_or_run_is_exec(command):
    assert tc.classify_bash(command) == tc.EXEC


def test_non_string_command_is_exec():
    assert tc.classify_bash(None) == tc.EXEC
    assert tc.classify_bash(["ls"]) == tc.EXEC


@pytest.mark.parametrize("name,expected", [
    ("Grep", tc.TECHNICAL_OP), ("Glob", tc.TECHNICAL_OP), ("LS", tc.TECHNICAL_OP),
    ("Read", tc.TECHNICAL_OP), ("NotebookRead", tc.TECHNICAL_OP), ("TodoRead", tc.TECHNICAL_OP),
    ("Edit", tc.EDIT), ("Write", tc.EDIT), ("MultiEdit", tc.EDIT), ("NotebookEdit", tc.EDIT),
    ("Task", tc.AGENT), ("Agent", tc.AGENT),
    ("WebFetch", tc.WEB), ("WebSearch", tc.WEB),
    ("mcp__llm_router__llm", tc.OTHER), ("AskUserQuestion", tc.OTHER), ("", tc.OTHER),
])
def test_tool_names_map_to_classes(name, expected):
    assert tc.classify_tool(name) == expected


def test_bash_is_classified_by_its_command_and_unknown_command_is_exec():
    assert tc.classify_tool("Bash", {"command": "git status"}) == tc.TECHNICAL_OP
    assert tc.classify_tool("bash", {"command": "git status"}) == tc.TECHNICAL_OP
    assert tc.classify_tool("Bash", {"command": "make"}) == tc.EXEC
    assert tc.classify_tool("Bash", None) == tc.EXEC  # no command seen: never assumed read-only
    assert tc.is_bash("Bash") and tc.is_bash("bash") and not tc.is_bash("Read")


def test_a_step_is_a_technical_op_only_when_every_tool_it_answers_is_one():
    assert tc.step_tool_class([]) is None
    assert tc.step_tool_class([("Read", {}), ("Grep", {})]) == tc.TECHNICAL_OP
    assert tc.step_tool_class([("Read", {}), ("Bash", {"command": "ls"})]) == tc.TECHNICAL_OP
    assert tc.step_tool_class([("Read", {}), ("Bash", {"command": "rm x"})]) == tc.EXEC
    assert tc.step_tool_class([("Read", {}), ("Edit", {})]) == tc.EDIT
    assert tc.step_tool_class([("WebFetch", {}), ("Agent", {})]) == tc.AGENT
    assert tc.step_tool_class([("mcp__x__y", {})]) == tc.OTHER
