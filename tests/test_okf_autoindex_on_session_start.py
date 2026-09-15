"""The OKF index refreshes in the background at session start.

Item 2 of the 2026-09-15 grounding work. Note what this does and does NOT fix.

It does NOT fix false rejections. Both grounding gates consult the disk — paths
always did, symbols do since the same day — so a stale index never causes a valid
citation to be called a hallucination. And it could not fix them anyway: the gap
is WITHIN a session (write a function at 14:20, ask at 14:21), and `okf index`
covers TRACKED files only, so an uncommitted file is absent however often it runs.

What a stale index costs is RETRIEVAL. `find_relevant` cannot return a document it
has never seen, so a prompt about a new module gets no injected context, the OKF
RESCUE arm cannot fire, and the turn falls through to Claude for want of material
rather than for want of capability.

Constraint: the first prompt must not be delayed. A full index of this repo is
1,184 documents.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/session-start.py"

spec = importlib.util.spec_from_file_location("_sshook", HOOK)
sshook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sshook)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    monkeypatch.setattr(sshook, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("LLM_ROUTER_OKF_AUTOINDEX", raising=False)
    return tmp_path


def _spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(sshook.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)) or _FakeProc())
    return calls


class _FakeProc:
    pid = 1234


def test_it_indexes_when_nothing_has_been_indexed_yet(repo, monkeypatch):
    calls = _spawned(monkeypatch)
    sshook._maybe_reindex_okf_bg(str(repo))
    assert calls, "no index was started on a repo with no stamp"
    argv = calls[0][0][0]
    assert "index_project" in argv[-1]
    assert "Path(" in argv[-1], (
        "index_project takes a Path; passing a str raised AttributeError in the "
        "detached child and, with stderr discarded, failed in total silence"
    )


def test_the_child_can_report_its_own_failure(repo, monkeypatch):
    calls = _spawned(monkeypatch)
    sshook._maybe_reindex_okf_bg(str(repo))
    kwargs = calls[0][1]
    assert kwargs.get("stderr") is not sshook.subprocess.DEVNULL, (
        "a background task that cannot report its own failure is worse than none"
    )


def test_it_detaches_so_the_first_prompt_is_not_delayed(repo, monkeypatch):
    calls = _spawned(monkeypatch)
    sshook._maybe_reindex_okf_bg(str(repo))
    assert calls[0][1].get("start_new_session") is True


def test_a_non_repo_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(sshook, "STATE_DIR", str(tmp_path / "state"))
    calls = _spawned(monkeypatch)
    sshook._maybe_reindex_okf_bg(str(tmp_path))
    assert not calls, "indexed a directory that is not a project"


def test_it_can_be_turned_off(repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OKF_AUTOINDEX", "0")
    calls = _spawned(monkeypatch)
    sshook._maybe_reindex_okf_bg(str(repo))
    assert not calls


def test_an_unchanged_repo_inside_the_ttl_is_skipped(repo, monkeypatch):
    calls = _spawned(monkeypatch)
    sshook._maybe_reindex_okf_bg(str(repo))          # first: indexes, writes stamp
    assert len(calls) == 1
    monkeypatch.setattr(sshook, "_repo_changed_since", lambda root, since: False)
    sshook._maybe_reindex_okf_bg(str(repo))          # second: nothing changed
    assert len(calls) == 1, "re-indexed an unchanged repo; this runs every session"


def test_a_changed_repo_is_reindexed_even_inside_the_ttl(repo, monkeypatch):
    calls = _spawned(monkeypatch)
    sshook._maybe_reindex_okf_bg(str(repo))
    monkeypatch.setattr(sshook, "_repo_changed_since", lambda root, since: True)
    sshook._maybe_reindex_okf_bg(str(repo))
    assert len(calls) == 2, "a repo with new files was not re-indexed"


def test_each_project_has_its_own_stamp(repo, tmp_path, monkeypatch):
    import subprocess
    other = tmp_path / "other"; other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=False)
    calls = _spawned(monkeypatch)
    sshook._maybe_reindex_okf_bg(str(repo))
    sshook._maybe_reindex_okf_bg(str(other))
    assert len(calls) == 2, "switching repos reused the other project's stamp"


def test_an_unknown_change_state_indexes_rather_than_skipping(repo, monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert sshook._repo_changed_since(str(repo), 0) is True, (
        "unknown must mean index; a wasted index is cheap, a missed one is not"
    )
