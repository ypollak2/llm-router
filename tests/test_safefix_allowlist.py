"""SAFEFIX (D-322): the local lanes are allowlist-only.

`write_policy.check_action` runs only programs on one allowlist and reads the
flags of every allowed program that can write, delete or run something. The
refusals are pinned row by row in SECURITY.md and driven through the real lanes
by tests/test_security_docs_match_the_code.py. This file pins the other side:
the ordinary read/test/build commands a local agent needs still pass, so the
allowlist does not quietly make the lanes useless. Behaviour only; nothing runs.
"""
from __future__ import annotations

import pytest

from llm_router import write_policy as wp


@pytest.fixture
def root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    r = tmp_path.resolve() / "project"
    (r / "src").mkdir(parents=True)
    (r / "a.txt").write_text("a\n")
    return r


@pytest.mark.parametrize("cmd", [
    "ls -la", "cat a.txt | head -5", "grep -rn x src", "rg -n x src", "find . -name '*.py'",
    "wc -l a.txt", "sort -o sorted.txt a.txt", "uniq a.txt out.txt", "tree -L 2",
    "git status", "git log --oneline -5", "git diff HEAD~1 --stat", "git show HEAD:a.txt",
    "git -c color.ui=never log", "git -c core.pager=cat log", "git -C src log",
    "git add a.txt", "git mv a.txt b.txt", "git rm a.txt", "git worktree list",
    "git reflog -n 5", "git archive -o out.tar HEAD", "git diff --output=d.patch",
    "pytest -q", "pytest tests/test_x.py::test_y -k foo -x", "python3 -m pytest -q",
    "python3 s.py", "python3 -m json.tool a.json", "python3 -m compileall src",
    "PYTHONPATH=src pytest -q", "uv run pytest -q", "uv run s.py", "timeout 30 pytest -q",
    "pytest --basetemp=.tmp/pt", "go test ./...", "go build -o bin/app .", "go vet ./...",
    "cargo test", "cargo build --target-dir target", "npm test", "npm run build",
    "ruff check src", "node s.js", "echo hi > out.txt", "mkdir -p build && cp a.txt build/",
    "rm a.txt", "cd src && ls", ": > a.txt",
])
def test_ordinary_agent_commands_still_pass(root, cmd):
    v = wp.check_action(cmd, root)
    assert v.decision == wp.ALLOW, (cmd, v.reason)


@pytest.mark.parametrize("cmd,decision", [
    ("cd .. && sort -o x a.txt", wp.REFUSE),               # output flag, after a cd out
    ("sort --output=../x a.txt", wp.REFUSE),
    ("sort --out=../x a.txt", wp.REFUSE),                  # GNU long-option prefix
    ("sort -uo ../x a.txt", wp.REFUSE),                    # clustered short option
    ("go test -coverprofile=../c.out ./...", wp.REFUSE),
    ("go test -exec rm ./...", wp.NEEDS_APPROVAL),
    ("cargo build --config build.rustc-wrapper=rm", wp.NEEDS_APPROVAL),
    ("npm --script-shell=/bin/rm run build", wp.NEEDS_APPROVAL),
    ("pytest -o cache_dir=../c", wp.REFUSE),
    ("pytest --junitxml=../r.xml", wp.REFUSE),
    ("pytest ../other/tests", wp.REFUSE),
    ("/usr/bin/crontab -r", wp.NEEDS_APPROVAL),             # judged by its basename
    ("env ls", wp.NEEDS_APPROVAL),                          # a wrapper not on the list
    ("PATH=/tmp/x ls", wp.NEEDS_APPROVAL),
    ("git -c include.path=../x status", wp.NEEDS_APPROVAL),
    ("git rm 'src/*'", wp.NEEDS_APPROVAL),                  # a glob pathspec deletes many
    ("git fetch", wp.NEEDS_APPROVAL),
    ("python3 -m pdb s.py", wp.NEEDS_APPROVAL),
    ("python3 -m compileall", wp.NEEDS_APPROVAL),           # no path: compiles sys.path
    ("make", wp.NEEDS_APPROVAL),
    ("uv run ../elsewhere/s.py", wp.REFUSE),
])
def test_flags_and_programs_outside_the_list_are_refused(root, cmd, decision):
    v = wp.check_action(cmd, root)
    assert v.decision == decision, (cmd, v)
    if decision == wp.NEEDS_APPROVAL:
        assert "approval is not wired, so treated as refused" in v.reason


def test_check_command_alone_is_still_the_tripwire(root):
    """The allowlist is check_action's; check_command keeps its P2.9-b contract."""
    assert wp.check_command("crontab -r", root).decision == wp.ALLOW
    assert wp.check_action("crontab -r", root).decision == wp.NEEDS_APPROVAL


def test_llm_local_task_inspection_list_is_inside_the_allowlist():
    """No program the llm_local_task inspection list runs is refused wholesale by
    the shared allowlist: that lane loses only flag-level forms."""
    import importlib.util
    from pathlib import Path
    path = Path(wp.__file__).parent / "hooks" / "agent_writes.py"
    spec = importlib.util.spec_from_file_location("_aw_safefix", path)
    aw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aw)
    names = {"python" if p.startswith("python") else p for p in aw._ALLOWED_PROGRAMS}
    assert names <= wp.allowed_programs(), names - wp.allowed_programs()


def test_toolkit_allowlist_is_inside_the_shared_allowlist():
    from llm_router.toolkit.policy import ALLOWED_PROGRAMS
    names = {"python" if p.startswith("python") else p for p in ALLOWED_PROGRAMS}
    assert names <= wp.allowed_programs(), names - wp.allowed_programs()
