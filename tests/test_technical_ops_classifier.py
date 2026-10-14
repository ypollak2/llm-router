"""technical_ops.classify_tool_call: GE1 list, fail closed (PLAN v16 P2.1 task 2)."""
from __future__ import annotations

import pytest

from llm_router.technical_ops import OpClass as C
from llm_router.technical_ops import classify_tool_call

BASH_CASES = [
    # listed, read-only
    ("ls", C.LIST), ("ls -la src", C.LIST), ("tree -L 2", C.LIST), ("tree -L 2 -a", C.LIST),
    ("cat README.md", C.READ), ("head -n 20 a.py", C.READ), ("tail -f x.log", C.READ),
    ("wc -l a.py b.py", C.READ),
    ("grep -rn foo src", C.SEARCH), ("rg 'foo bar' src", C.SEARCH),
    ("rg -n 'a > b' src", C.SEARCH), ("find . -name '*.py'", C.SEARCH),
    ("git status", C.GIT_READ), ("git log --oneline -5", C.GIT_READ),
    ("git diff --stat", C.GIT_READ), ("git log --oneline", C.GIT_READ),
    ("git diff HEAD~1", C.GIT_READ), ("git show abc123", C.GIT_READ),
    ("git blame a.py", C.GIT_READ), ("git branch", C.GIT_READ),
    ("git branch -a", C.GIT_READ), ("git branch --list 'feat/*'", C.GIT_READ),
    ("git -C sub status", C.GIT_READ),
    ("black --check src", C.FORMAT_CHECK), ("isort --check src", C.FORMAT_CHECK),
    ("ruff format --check .", C.FORMAT_CHECK), ("prettier --check .", C.FORMAT_CHECK),
    ("cargo fmt --check", C.FORMAT_CHECK),
    # chains where every segment is listed
    ("ls && git status", C.LIST), ("git status; git diff", C.GIT_READ),
    ("cat a.py | head -5", C.READ), ("grep foo a.py | wc -l", C.SEARCH),
    # redirection in any form
    ("ls > out.txt", None), ("cat a >> b", None), ("cat < a", None),
    ("git log > log.txt", None), ("ls 2>&1", None), ("ls &> o", None),
    ("cat a >| b", None), ("cat <<EOF", None), ("cat <<< hi", None),
    ("ls 2>/dev/null", None),
    # chains to something not on the list
    ("ls && rm -rf x", None), ("ls; rm x", None), ("git status && git commit -m x", None),
    ("cat a | tee b", None), ("cat a | sort -o b", None), ("cd sub && ls", None),
    ("ls | xargs rm", None),
    # unsupported operators
    ("ls || true", None), ("sleep 1 &", None), ("ls & ls", None), ("(ls)", None),
    ("echo $(ls)", None), ("cat `ls`", None), ("ls\nrm x", None),
    # writes
    ("find . -delete", None), ("find . -name x -exec rm {} ;", None),
    ("find . -fprintf out.txt '%p\\n'", None), ("find . -fprintf=o x", None),
    ("find . -fprint o", None), ("find . -fprint0 o", None), ("find . -fls o", None),
    ("find . -ok rm {} ;", None), ("find . -okdir rm {} ;", None),
    ("find . -execdir ls {} ;", None), ("rg --pre ./x foo", None),
    ("tree -o out.txt", None), ("tree -L 2 -o f", None), ("tree --output=o", None),
    ("tree --output o", None), ("tree -ao f", None), ("tree -ofile", None), ("tree --out=o", None),
    ("git branch -D x", None), ("git branch newname", None), ("git branch -m a b", None),
    ("git commit -m x", None), ("git push", None), ("git checkout main", None),
    ("git diff --output=o.txt", None), ("git -c core.pager=x status", None),
    ("git diff --ext-diff", None), ("git log --ext-diff --textconv", None),
    ("git show --textconv HEAD", None), ("git diff --ext HEAD", None), ("git diff --text x", None),
    ("black src", None), ("black --check --fix src", None),
    ("prettier --check --write .", None), ("ruff format .", None), ("cargo fmt", None),
    # not listed / malformed
    ("rm x", None), ("sed -i s/a/b/ f", None), ("python -c 'print(1)'", None),
    ("FOO=1 ls", None), ("", None), ("   ", None), ("ls 'unbalanced", None),
    ("echo hi", None),
    # code review of #483: quoted operators are arguments, not separators
    ("find . '|' cat -delete", None), ("git branch '|' cat -D main", None),
    ("git diff ';' cat --output=x", None), ("tree '&&' cat -o out", None),
    ('grep "|" f', None),
    # a formatter must be the bare name, and --write= is a write
    ("./black --check src", None), ("/tmp/evil/black --check", None),
    ("prettier --check --write=true .", None), ("ruff format --check --fix=true", None),
    ("black --check src", C.FORMAT_CHECK),
    # quotes around ordinary words still classify
    ("grep 'foo bar' src", C.SEARCH), ("cat 'my file.txt'", C.READ),
]


def test_case_count():
    assert len(BASH_CASES) >= 60


@pytest.mark.parametrize("cmd,expected", BASH_CASES)
def test_bash(cmd, expected):
    assert classify_tool_call("Bash", {"command": cmd}) is expected


@pytest.mark.parametrize("tool,expected", [
    ("Grep", C.SEARCH), ("Glob", C.LIST), ("LS", C.LIST), ("Read", C.READ),
    ("NotebookRead", C.READ), ("TodoRead", C.READ),
    ("Edit", None), ("Write", None), ("MultiEdit", None), ("NotebookEdit", None),
    ("Task", None), ("WebFetch", None), ("WebSearch", None), ("Unknown", None),
])
def test_non_bash_tools(tool, expected):
    assert classify_tool_call(tool, {"pattern": "x"}) is expected


@pytest.mark.parametrize("bad", [None, "ls", ["ls"], {}, {"command": None}, {"command": 5}])
def test_bash_bad_input(bad):
    assert classify_tool_call("Bash", bad) is None
