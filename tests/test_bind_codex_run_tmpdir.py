"""S1 (BIND W1): a delegated Codex run cannot write ``/tmp`` or ``$TMPDIR``, so it gets a temp dir of its own INSIDE its
writable root: created before the run, removed after, ignored by git, and never allowed to point outside the root.

The sandbox side of this (that ``/tmp`` really is closed and the run's own dir really is writable) is checked on the
real binary in ``tests/test_bind_codex_sandbox_live.py``. This file checks the plumbing around it with a stand-in
``codex`` executable that records its environment.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
import time

import pytest

from llm_router.codex_agent import RUN_TMP_PARENT, make_run_tmpdir, remove_run_tmpdir, run_codex
from tests._bind_support import init_repo, stub_calls, write_codex_stub

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell stub")


def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_a_run_dir_is_private_inside_the_root_and_git_never_sees_it(tmp_path):
    repo = init_repo(tmp_path / "repo")
    path = make_run_tmpdir(str(repo))
    assert path and os.path.isdir(path)
    assert os.path.dirname(path) == os.path.join(str(repo), RUN_TMP_PARENT)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    (repo / RUN_TMP_PARENT / path.rsplit("/", 1)[1] / "scratch.txt").write_text("x")
    assert (repo / RUN_TMP_PARENT / ".gitignore").read_text() == "*\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""  # not even untracked
    assert _git(repo, "check-ignore", "-q", os.path.join(path, "scratch.txt")).returncode == 0
    assert _git(repo, "add", "-A").returncode == 0 and _git(repo, "diff", "--cached", "--name-only").stdout == ""


def test_each_run_gets_its_own_dir_and_removal_leaves_the_parent(tmp_path):
    repo = init_repo(tmp_path / "repo")
    a, b = make_run_tmpdir(str(repo)), make_run_tmpdir(str(repo))
    assert a and b and a != b
    remove_run_tmpdir(a)
    assert not os.path.exists(a) and os.path.isdir(b)  # a second run's dir is not touched
    assert (repo / RUN_TMP_PARENT / ".gitignore").exists()
    remove_run_tmpdir(None)  # no dir: nothing to do, no error


def test_a_dir_a_killed_run_left_is_pruned_once_it_is_a_day_old_and_nothing_else_is(tmp_path):
    repo = init_repo(tmp_path / "repo")
    first = make_run_tmpdir(str(repo))
    parent = repo / RUN_TMP_PARENT
    old, fresh = parent / "0123456789ab", parent / "abcdef012345"
    unrelated_old, a_file = parent / "notes", parent / "deadbeef0000"
    for d in (old, fresh, unrelated_old):
        d.mkdir()
        (d / "f").write_text("x")
    a_file.write_text("not a dir")
    for entry in (old, unrelated_old, a_file):
        _age(entry, 2 * 24 * 3600)
    assert make_run_tmpdir(str(repo))
    assert not old.exists(), "a run dir over a day old is removed"
    assert fresh.exists() and unrelated_old.exists() and a_file.exists() and os.path.isdir(first)


def test_a_symlinked_parent_is_refused_and_nothing_outside_the_root_is_touched(tmp_path):
    """A repository can ship the parent as a symlink; a delegated run can swap it in (the root is writable). Following
    it would create the run dir, and prune old dirs, outside the work tree."""
    repo = init_repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    victim = elsewhere / "0123456789ab"
    victim.mkdir(parents=True)
    _age(victim, 5 * 24 * 3600)
    (repo / RUN_TMP_PARENT).symlink_to(elsewhere, target_is_directory=True)
    assert make_run_tmpdir(str(repo)) is None
    assert victim.exists() and not (elsewhere / ".gitignore").exists()
    assert sorted(p.name for p in elsewhere.iterdir()) == ["0123456789ab"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_root_that_cannot_be_written_gives_no_temp_dir_instead_of_an_error(tmp_path):
    repo = init_repo(tmp_path / "repo")
    repo.chmod(0o500)
    try:
        assert make_run_tmpdir(str(repo)) is None
    finally:
        repo.chmod(0o700)


def test_removing_a_swapped_in_symlink_unlinks_it_and_spares_the_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    remove_run_tmpdir(str(link))
    assert not link.exists() and (target / "keep.txt").exists()


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    log = tmp_path / "argv.jsonl"
    stub = write_codex_stub(tmp_path / "bin", argv_log=log)
    monkeypatch.setenv("CODEX_PATH", str(stub))
    repo = init_repo(tmp_path / "repo")

    def go(sandbox, **kw):
        return asyncio.run(run_codex("say hi", model="stub-model", working_dir=str(repo), timeout=kw.pop("timeout", 60),
                                     sandbox=sandbox, **kw))

    return go, repo, log


def test_run_codex_points_the_childs_tmpdir_at_a_dir_inside_the_root_and_removes_it_after(stubbed):
    go, repo, log = stubbed
    res = go("workspace-write")
    assert res.success, res.content
    (call,) = stub_calls(log)
    tmp = call["tmpdir"]
    assert tmp and call["tmpdir_exists"], call  # created before the child started
    assert os.path.realpath(os.path.dirname(tmp)) == os.path.realpath(repo / RUN_TMP_PARENT)
    assert not os.path.exists(tmp)  # gone after the run
    assert (repo / RUN_TMP_PARENT / ".gitignore").exists()


def test_the_temp_dir_is_removed_when_the_run_fails_or_times_out(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    log = tmp_path / "argv.jsonl"
    for mode, sleep_s, timeout in (("fail", 0.0, 60), ("answer", 30.0, 1)):
        stub = write_codex_stub(tmp_path / f"bin-{mode}", mode=mode, argv_log=log, sleep_s=sleep_s)
        monkeypatch.setenv("CODEX_PATH", str(stub))
        res = asyncio.run(run_codex("p", model="m", working_dir=str(repo), timeout=timeout, sandbox="workspace-write"))
        assert not res.success
        assert [p for p in (repo / RUN_TMP_PARENT).iterdir() if p.is_dir()] == [], mode


@pytest.mark.parametrize("sandbox", ["read-only", None])
def test_only_a_write_run_gets_a_temp_dir(stubbed, sandbox):
    """A read-only run writes nothing, a pure-LLM call never gets an agent's sandbox: neither leaves a directory in the repo."""
    go, repo, log = stubbed
    assert go(sandbox).success
    assert not (repo / RUN_TMP_PARENT).exists()
