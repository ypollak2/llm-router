"""Two llm_local_task bugs observed 2026-10-08 with ollama/qwen3-coder:30b.

1. `changed_files` came back [] although `git status` showed ` M` on a file the
   model edited. Cause: the before/after snapshot stops after _SNAPSHOT_MAX_FILES
   files in os.walk order, so files in the unseen tail are never compared.
2. `acceptance_check="HOME=$(mktemp -d) pytest ..."` died with a bare
   FileNotFoundError on "HOME=$(mktemp". The no-shell rule stays (see
   test_local_task_authority); shell syntax must now be rejected up front, with
   a message that names the fix.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from llm_router.tools import local_task as lt


def _call(**kw):
    import asyncio
    return json.loads(asyncio.run(lt.llm_local_task(**kw)))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(root: Path, extra_files: int = 0) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "aaa").mkdir()
    for i in range(extra_files):          # sorts before "src" in walk order
        (root / "aaa" / f"f{i:04d}.txt").write_text(str(i))
    (root / "src").mkdir()
    (root / "src" / "target.py").write_text("x = 1\n")
    (root / "src" / "other.py").write_text("y = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def _fake_loop(project_root: Path):
    from llm_router.hooks.agent_loop import execute_tool

    def run(**kw):
        root = kw["project_root"]
        execute_tool("edit_file", {"path": "src/target.py",
                                   "old_string": "x = 1", "new_string": "x = 2"}, root)
        execute_tool("write_file", {"path": "src/new.py", "content": "z = 1\n"}, root)
        return "done"
    return run


@pytest.fixture(autouse=True)
def _apply_writes(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "apply")


@pytest.fixture
def wd(tmp_path):
    """The conftest puts a fake HOME under tmp_path; keep the workdir apart from it."""
    d = tmp_path / "work"
    d.mkdir()
    return d


def test_edit_and_write_paths_both_reported_in_git_repo(wd, monkeypatch):
    _repo(wd)
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _fake_loop(wd))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert out["changed_files"] == ["src/new.py", "src/target.py"], out


def test_large_repo_does_not_hide_the_edit(wd, monkeypatch):
    """The observed failure: more files than the snapshot cap before src/."""
    monkeypatch.setattr(lt, "_SNAPSHOT_MAX_FILES", 5)
    _repo(wd, extra_files=20)
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _fake_loop(wd))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert "src/target.py" in out["changed_files"], out
    assert "src/new.py" in out["changed_files"], out


def test_file_already_dirty_before_run_is_only_reported_if_edited_again(wd, monkeypatch):
    _repo(wd)
    (wd / "src" / "other.py").write_text("y = 99\n")      # dirty before the run
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _fake_loop(wd))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert "src/other.py" not in out["changed_files"], out
    assert "src/target.py" in out["changed_files"], out


def test_non_git_workdir_still_reports_changes(wd, monkeypatch):
    (wd / "src").mkdir()
    (wd / "src" / "target.py").write_text("x = 1\n")
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _fake_loop(wd))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert out["changed_files"] == ["src/new.py", "src/target.py"], out


@pytest.mark.parametrize("check", [
    "HOME=$(mktemp -d) pytest -q",
    "FOO=bar pytest -q",
    "pytest -q && echo ok",
    "pytest -q | tee out.txt",
    "pytest -q ; true",
    "pytest -q > out.txt",
])
def test_shell_syntax_is_rejected_with_a_clear_error(tmp_path, check):
    ok, msg = lt._run_check(check, tmp_path, timeout=5)
    assert ok is False
    assert "shell" in msg.lower() and "script" in msg.lower(), msg
    assert "FileNotFoundError" not in msg


def test_rejection_surfaces_in_the_tool_result(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", lambda **kw: "done")
    out = _call(objective="x", workdir=str(tmp_path),
                acceptance_check="HOME=$(mktemp -d) pytest -q")
    assert out["check_passed"] is False
    assert "script" in out["check_output"].lower()


def test_script_path_and_quoted_args_still_work(tmp_path):
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\nHOME=$(mktemp -d) true\n")
    script.chmod(0o755)
    assert lt._run_check(str(script), tmp_path, 10)[0] is True
    assert lt._run_check(["python3", "-c", "assert 1 > 0"],
                         tmp_path, 10)[0] is True
    assert lt._run_check('python3 -c "assert 1>0"', tmp_path, 10)[0] is True


# --- review round 1 (PR #358): each test below failed against the first cut -----

def _loop_doing(fn):
    def run(**kw):
        fn(kw["project_root"])
        return "done"
    return run


def test_snapshots_are_taken_inside_the_serialised_section(wd, monkeypatch):
    """Concurrent runs must not attribute each other's edits (A.0-3)."""
    _repo(wd)
    held = []
    real = lt._take_state
    monkeypatch.setattr(lt, "_take_state",
                        lambda *a, **k: (held.append(lt._AGENT_ENV_LOCK._is_owned()), real(*a, **k))[1])
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _fake_loop(wd))
    _call(objective="x", workdir=str(wd), apply_writes=True)
    assert held == [True, True], held


def test_edit_then_commit_during_run_is_reported(wd, monkeypatch):
    _repo(wd)

    def work(root):
        (root / "src" / "target.py").write_text("x = 5\n")
        _git(root, "commit", "-qam", "model commit")
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _loop_doing(work))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert out["changed_files"] == ["src/target.py"], out


def test_gitignored_edit_is_a_documented_limit(wd, monkeypatch):
    _repo(wd)
    (wd / ".gitignore").write_text("secret.env\n")
    (wd / "secret.env").write_text("a=1\n")
    _git(wd, "add", ".gitignore")
    _git(wd, "commit", "-qm", "ignore")

    def work(root):
        (root / "secret.env").write_text("a=2\n")
        (root / "src" / "target.py").write_text("x = 7\n")
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _loop_doing(work))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert out["changed_files"] == ["src/target.py"]
    assert "gitignored" in out["changed_files_scope"].lower()
    assert "gitignored" in (lt.llm_local_task.__doc__ or "").lower()


def test_non_git_dir_inside_repo_that_ignores_it_falls_back(wd, monkeypatch):
    _repo(wd)
    (wd / ".gitignore").write_text("scratch/\n")
    _git(wd, "add", ".gitignore")
    _git(wd, "commit", "-qm", "ignore")
    nested = wd / "scratch"
    nested.mkdir()
    (nested / "a.py").write_text("a = 1\n")
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop",
                        _loop_doing(lambda r: (r / "a.py").write_text("a = 2\n")))
    out = _call(objective="x", workdir=str(nested), apply_writes=True)
    assert out["changed_files"] == ["a.py"], out


def test_rename_lists_old_and_new_path(wd, monkeypatch):
    _repo(wd)
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop",
                        _loop_doing(lambda r: _git(r, "mv", "src/other.py", "src/moved.py")))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert out["changed_files"] == ["src/moved.py", "src/other.py"], out


def test_untracked_files_listed_individually_not_as_directory(wd, monkeypatch):
    _repo(wd)

    def work(root):
        (root / "newdir").mkdir()
        (root / "newdir" / "one.py").write_text("1\n")
        (root / "newdir" / "two.py").write_text("2\n")
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _loop_doing(work))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert out["changed_files"] == ["newdir/one.py", "newdir/two.py"], out


def test_subdirectory_workdir_strips_prefix_and_ignores_siblings(wd, monkeypatch):
    _repo(wd)

    def work(root):
        (root / "target.py").write_text("x = 3\n")                    # inside workdir
        (root.parent / "aaa" / "outside.txt").write_text("changed")   # sibling dir
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _loop_doing(work))
    out = _call(objective="x", workdir=str(wd / "src"), apply_writes=True)
    assert out["changed_files"] == ["target.py"], out


def test_already_dirty_file_edited_again_is_reported(wd, monkeypatch):
    _repo(wd)
    (wd / "src" / "other.py").write_text("y = 99\n")
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop",
                        _loop_doing(lambda r: (r / "src" / "other.py").write_text("y = 100\n")))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert out["changed_files"] == ["src/other.py"], out


def test_git_state_lost_after_run_says_so_instead_of_empty_list(wd, monkeypatch):
    _repo(wd)
    real, calls = lt._git_state, []

    def flaky(root, since=None):
        calls.append(1)
        return real(root, since) if len(calls) == 1 else None
    monkeypatch.setattr(lt, "_git_state", flaky)
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _fake_loop(wd))
    out = _call(objective="x", workdir=str(wd), apply_writes=True)
    assert out["changed_files"] == []
    assert "unknown" in out["changed_files_note"], out


@pytest.mark.parametrize("check", [
    "pytest -q 2> err.txt",
    "pytest -q &> all.txt",
    "pytest `which py`",
    "echo $(date)",
    "pytest -q 2>&1",
])
def test_more_unquoted_shell_syntax_rejected(tmp_path, check):
    ok, msg = lt._run_check(check, tmp_path, timeout=5)
    assert ok is False and "script" in msg.lower(), msg


@pytest.mark.parametrize("check", [
    "python3 -c \"assert '`' == chr(96)\"",
    "python3 -c 'assert \"`\" == chr(96)'",
    "python3 -c 'assert \"$(\" == chr(36)+chr(40)'",
])
def test_metacharacters_inside_quotes_are_literal_and_allowed(tmp_path, check):
    ok, msg = lt._run_check(check, tmp_path, timeout=10)
    assert ok is True, msg
