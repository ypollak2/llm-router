"""A project with no semantic index gets one built, automatically, once.

Owner decision (semantic_audit/REPORT.md follow-up): routing/retrieval must
not run forever against a project that was never indexed. These tests drive
`llm_router.semantic.autoindex` directly, and through its one call site in
`semantic.pack.build()`, against a real (tiny) git repo.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router.semantic import autoindex


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                    capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real, tiny git repo, isolated LLM_ROUTER_HOME, and no inherited HOME.

    pytest's own ``tmp_path`` lives under the system scratch dir, which is
    exactly what the scratch-prefix guard (finding 1) exists to exclude — so
    this fixture disables that guard by default. Tests that exercise the
    guard itself (below) explicitly re-enable it with ``monkeypatch.delenv``.
    """
    router_home = tmp_path / "router-home"
    router_home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(router_home))
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES", "")
    (tmp_path / "user-home").mkdir()

    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("def f():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _wait_for_spawn(fn, timeout=5.0):
    """Popen returning doesn't mean the lock file exists on every OS/FS; poll briefly."""
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.05)
    return result


# ── the gate: empty vs built ─────────────────────────────────────────────────


def test_a_project_with_no_index_is_empty(repo):
    assert autoindex.index_is_empty(repo) is True


def test_a_built_index_is_not_empty(repo):
    from llm_router.semantic import indexer

    indexer.index(root=repo)
    assert autoindex.index_is_empty(repo) is False


def test_an_index_file_with_zero_rows_still_counts_as_empty(repo):
    from llm_router.semantic import store as sstore

    sstore.connect(repo).close()  # creates the file, indexes nothing
    assert autoindex.index_is_empty(repo) is True


# ── maybe_start_background_index: the guards ────────────────────────────────


def test_starts_a_build_for_an_unindexed_git_repo(repo):
    started = autoindex.maybe_start_background_index(repo)
    assert started is True
    deadline = time.monotonic() + 10
    from llm_router.semantic import store as sstore
    while time.monotonic() < deadline:
        if sstore.index_path(repo).exists() and not autoindex.index_is_empty(repo):
            break
        time.sleep(0.1)
    assert not autoindex.index_is_empty(repo), "background build never completed"


def test_does_not_start_outside_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES", "")
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    started = autoindex.maybe_start_background_index(not_a_repo)
    assert started is False


def test_does_not_start_for_home_itself(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _git(fake_home, "init", "-q")  # dotfiles: HOME can be a repo
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router-home"))
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES", "")
    started = autoindex.maybe_start_background_index(fake_home)
    assert started is False


def test_skips_a_repo_over_the_file_cap(repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_MAX_FILES", "0")
    started = autoindex.maybe_start_background_index(repo)
    assert started is False


def test_does_not_start_when_the_index_is_already_built(repo):
    from llm_router.semantic import indexer

    indexer.index(root=repo)
    started = autoindex.maybe_start_background_index(repo)
    assert started is False


def test_claim_lock_is_atomic_under_concurrency(repo):
    """Regression for a real race: N callers that all see an empty, unlocked
    index at the same instant must not all spawn a build. Reproduced live
    with 12 concurrent OS processes before the fix (12/12 spawned, every
    run); this drives the same code path (`_claim_lock`) with threads, which
    still race on the underlying `os.open` syscall since it releases the
    GIL."""
    import threading

    lock = autoindex._lock_path(repo)
    n = 16
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = autoindex._claim_lock(lock, autoindex._cooldown_s())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1, (
        f"{sum(1 for r in results if r)} of {n} concurrent callers claimed the "
        "lock; exactly one must win"
    )


def test_cooldown_prevents_a_second_spawn(repo):
    first = autoindex.maybe_start_background_index(repo)
    assert first is True
    second = autoindex.maybe_start_background_index(repo)
    assert second is False, "a second call inside the cooldown must not spawn again"


def test_cooldown_expires(repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_COOLDOWN_S", "0")
    first = autoindex.maybe_start_background_index(repo)
    assert first is True
    # index_is_empty may now be False if the build finished fast; force the
    # gate open again to isolate the cooldown behaviour from build speed.
    from llm_router.semantic import store as sstore
    db = sstore.index_path(repo)
    if db.exists():
        db.unlink()
    second = autoindex.maybe_start_background_index(repo)
    assert second is True


def test_env_flag_disables_autoindex_entirely(repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX", "off")
    started = autoindex.maybe_start_background_index(repo)
    assert started is False


@pytest.mark.parametrize("flag_value", ["0", "false", "no", "OFF"])
def test_env_flag_accepts_common_falsy_spellings(repo, monkeypatch, flag_value):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX", flag_value)
    assert autoindex.maybe_start_background_index(repo) is False


def test_default_is_on(repo, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_AUTOINDEX", raising=False)
    assert autoindex.maybe_start_background_index(repo) is True


# ── failure safety ───────────────────────────────────────────────────────────


def test_a_spawn_failure_is_safe_and_recorded(repo, monkeypatch):
    """`subprocess.run` (the file-count check) is itself built on `Popen`, so the
    count check must be bypassed here or patching Popen would make it fail
    first and report "over the cap" rather than exercising the spawn failure."""
    from llm_router import failopen

    monkeypatch.setattr(autoindex, "_tracked_file_count", lambda root: 1)

    def _boom(*a, **k):
        raise OSError("no fork slots")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    before = failopen.snapshot().by_code.get(autoindex._FAILOPEN_CODE, 0)
    started = autoindex.maybe_start_background_index(repo)
    assert started is False
    after = failopen.snapshot().by_code.get(autoindex._FAILOPEN_CODE, 0)
    assert after == before + 1


def test_an_unreadable_git_binary_is_safe(repo, monkeypatch):
    """`git ls-files` failing (e.g. no git on PATH) must not raise or spawn."""
    monkeypatch.setattr(autoindex, "_tracked_file_count", lambda root: None)
    started = autoindex.maybe_start_background_index(repo)
    assert started is False


def test_popen_failure_releases_the_lock(repo, monkeypatch):
    """A spawn failure must release the cooldown lock it claimed — otherwise
    a transient `Popen` failure (e.g. a momentary fork-slot shortage) causes
    a full cooldown's worth of blackout for a build that never ran."""
    monkeypatch.setattr(autoindex, "_tracked_file_count", lambda root: 1)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(
        OSError("no fork slots")
    ))
    started = autoindex.maybe_start_background_index(repo)
    assert started is False
    lock = autoindex._lock_path(repo)
    assert not lock.exists(), "lock must be released after a Popen failure"


def test_detached_build_is_niced(repo, monkeypatch):
    """The background build should run at a lower priority than the
    foreground session it shares the box with."""
    monkeypatch.setattr(autoindex, "_tracked_file_count", lambda root: 1)
    captured = {}

    class _FakeProc:
        pid = 999

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    started = autoindex.maybe_start_background_index(repo)
    assert started is True
    if sys.platform != "win32":
        assert "preexec_fn" in captured, "detached build must be niced on POSIX"


# ── the scratch-tempdir exclusion (finding 1) ───────────────────────────────


def test_scratch_prefix_guard_is_disabled_by_the_repo_fixture(repo):
    """Sanity check for the other tests in this file: without this override,
    `repo` (built from pytest's own `tmp_path`) would always be excluded."""
    assert autoindex._is_scratch_path(repo) is False


def test_real_scratch_prefixes_catch_pytests_own_tmp_path(repo, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES", raising=False)
    assert autoindex._is_scratch_path(repo) is True


def test_linked_worktree_under_tempdir_does_not_spawn(repo, monkeypatch):
    """The reported bug: a linked git worktree's `.git` is a plain FILE, so it
    satisfies `(scope / ".git").exists()` exactly like a real repo. Each
    fresh scratch worktree path (the common shape for an agent sub-session)
    must not become a never-cleaned ~5MB knowledge-store entry."""
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES", raising=False)
    worktree = repo / "scratch-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/scratch-worktree\n")
    started = autoindex.maybe_start_background_index(worktree)
    assert started is False


def test_scratch_guard_resolves_symlinked_tmp(repo, monkeypatch, tmp_path):
    """macOS's /tmp is a symlink to /private/tmp; the guard must compare
    REALPATHs so a path spelled either way is still caught."""
    real_scratch_root = tmp_path.resolve()
    monkeypatch.setenv(
        "LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES", str(real_scratch_root)
    )
    alias_dir = tmp_path / "alias_project"
    alias_dir.mkdir()
    assert autoindex._is_scratch_path(alias_dir) is True


def test_scratch_prefixes_env_var_is_pathsep_separated(tmp_path, monkeypatch):
    other = tmp_path / "other-scratch"
    other.mkdir()
    not_scratch = tmp_path / "not-scratch"
    not_scratch.mkdir()
    monkeypatch.setenv(
        "LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES",
        f"/does/not/exist{os.pathsep}{other}",
    )
    assert autoindex._is_scratch_path(other / "nested") is True
    assert autoindex._is_scratch_path(not_scratch) is False


# ── cooldown applies to a negative result too (finding 2) ──────────────────


def test_over_cap_skip_is_cached_for_the_cooldown(repo, monkeypatch):
    """The over-cap skip previously returned before claiming any lock, so
    `git ls-files` re-ran on every single call. It must now be cached."""
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_MAX_FILES", "0")
    calls = []
    real_run = subprocess.run

    def spy(cmd, *a, **k):
        if "ls-files" in cmd:
            calls.append(cmd)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(autoindex.subprocess, "run", spy)
    for _ in range(5):
        assert autoindex.maybe_start_background_index(repo) is False
    assert len(calls) == 1, (
        f"git ls-files invoked {len(calls)} times across 5 calls; expected 1 "
        "per cooldown"
    )


def test_git_failure_skip_is_also_cached_for_the_cooldown(repo, monkeypatch):
    calls = {"n": 0}

    def flaky_count(root):
        calls["n"] += 1
        return None

    monkeypatch.setattr(autoindex, "_tracked_file_count", flaky_count)
    for _ in range(5):
        assert autoindex.maybe_start_background_index(repo) is False
    assert calls["n"] == 1, (
        f"_tracked_file_count invoked {calls['n']} times across 5 calls; "
        "expected 1 per cooldown"
    )


def test_over_cap_skip_is_rechecked_after_cooldown_expires(repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_MAX_FILES", "0")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX_COOLDOWN_S", "0")
    calls = {"n": 0}
    real_count = autoindex._tracked_file_count

    def counting(root):
        calls["n"] += 1
        return real_count(root)

    monkeypatch.setattr(autoindex, "_tracked_file_count", counting)
    for _ in range(3):
        assert autoindex.maybe_start_background_index(repo) is False
    assert calls["n"] == 3, "a zero cooldown must let every call recheck"


# ── unwritable lock directory (finding 4) ───────────────────────────────────


def test_claim_lock_falls_back_to_inprocess_marker_when_dir_unwritable(
    tmp_path, monkeypatch
):
    """When the lock's directory can't even be created (e.g. a read-only
    filesystem), this process must still back off for the cooldown instead
    of reporting "never attempted" (and so re-running git) on every call."""
    lock = tmp_path / "nope" / "deeper" / "autoindex.lock"

    def bad_mkdir(self, *a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "mkdir", bad_mkdir)
    cooldown = 3600.0
    assert autoindex._claim_lock(lock, cooldown) is False
    assert autoindex._recently_attempted(lock, cooldown) is True
    assert not lock.exists()


# ── wired into retrieval (pack.build) ────────────────────────────────────────


def test_pack_build_triggers_autoindex_for_an_unindexed_project(repo):
    from llm_router.semantic import pack

    built = pack.build("what does f do", root=repo)
    assert "structural_index" in built.missing_requirements
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and autoindex.index_is_empty(repo):
        time.sleep(0.1)
    assert not autoindex.index_is_empty(repo), "pack.build() never triggered a build"


def test_pack_build_does_not_trigger_twice_in_a_row(repo):
    from llm_router.semantic import pack

    pack.build("first", root=repo)
    lock = autoindex._lock_path(repo)
    first_mtime = lock.stat().st_mtime
    pack.build("second", root=repo)
    assert lock.stat().st_mtime == pytest.approx(first_mtime)
