"""CTX-PROJ-1 (D-57): one session id -> one project shard, whatever the writer's cwd."""

from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

from llm_router import session_store as ss


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    for v in ("LLM_ROUTER_PROJECT_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
              "LLM_ROUTER_SESSION_CONTEXT"):
        monkeypatch.delenv(v, raising=False)
    return home


def _repo(path):
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def _hash(p):
    return hashlib.sha1(str(p.resolve()).encode(), usedforsecurity=False).hexdigest()[:16]


def _shards(sid):
    return sorted((ss._state_dir() / "projects").glob(f"*/session_context_{sid}.jsonl"))


def test_events_from_other_cwds_land_in_one_file(tmp_path, monkeypatch):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    wt = tmp_path / "A-wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../A/.git/worktrees/wt\n")  # worktree: .git is a file
    ss.write_pointer("sid1", cwd=str(a))
    (b / "sub").mkdir()
    for i, cwd in enumerate((a, b, wt, b / "sub")):
        monkeypatch.chdir(cwd)
        ss.record_event("sid1", "user_prompt", f"event {i}")
    files = _shards("sid1")
    assert [f.parent.name for f in files] == [_hash(a)]
    assert len(ss.load_events("sid1")) == 4
    monkeypatch.chdir(b)  # a reader from another cwd finds the same file
    assert ss._session_path("sid1") == files[0]


def test_pin_survives_session_cwd_moving(tmp_path, monkeypatch):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    ss.write_pointer("sid2", cwd=str(a))
    ss.record_event("sid2", "user_prompt", "first")
    ss.write_pointer("sid2", cwd=str(b))  # session cd'd to another repo
    ss.record_event("sid2", "user_prompt", "second")
    assert [f.parent.name for f in _shards("sid2")] == [_hash(a)]


def test_pin_ignores_session_cwd_age(tmp_path):
    a = _repo(tmp_path / "A")
    ss.write_pointer("sid3", cwd=str(a))
    ss.record_event("sid3", "user_prompt", "x")
    p = ss._session_cwd_path("sid3")
    p.write_text(json.dumps({"cwd": str(a), "ts": time.time() - 3 * 86400}))
    assert ss._session_path("sid3").parent.name == _hash(a)


def test_env_override_wins(tmp_path, monkeypatch):
    a = _repo(tmp_path / "A")
    ss.write_pointer("sid4", cwd=str(a))
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "forced")
    ss.record_event("sid4", "user_prompt", "x")
    assert [f.parent.name for f in _shards("sid4")] == ["forced"]


def test_explicit_project_root_wins(tmp_path):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    ss.write_pointer("sid5", cwd=str(a))
    assert ss._session_path("sid5", str(b)).parent.name == _hash(b)


def test_no_session_cwd_record_is_old_behaviour(tmp_path, monkeypatch):
    b = _repo(tmp_path / "B")
    monkeypatch.chdir(b)
    ss.record_event("sid6", "user_prompt", "x")
    assert [f.parent.name for f in _shards("sid6")] == [_hash(b)]
    assert ss._project_id() == _hash(b)  # no session id: unchanged
    assert not list(ss._state_dir().glob("session_project_*"))  # nothing pinned


def test_first_write_race_is_deterministic(tmp_path, monkeypatch):
    """Before the hook's record exists the writer's cwd is used and nothing is pinned;
    after it exists, every later event goes to the session's project."""
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    monkeypatch.chdir(b)
    ss.record_event("sid7", "user_prompt", "early")
    ss.write_pointer("sid7", cwd=str(a))
    ss.record_event("sid7", "user_prompt", "late")
    assert sorted(f.parent.name for f in _shards("sid7")) == sorted([_hash(a), _hash(b)])
    assert ss._session_path("sid7").parent.name == _hash(a)


def test_archive_and_restore_use_same_project(tmp_path, monkeypatch):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    ss.write_pointer("sid8", cwd=str(a))
    monkeypatch.chdir(b)
    ss.record_event("sid8", "user_prompt", "keep me")
    ss.archive_session("sid8")
    assert ss._archive_path("sid8").parent.parent.name == _hash(a)
    assert ss._archive_path("sid8").exists() and not _shards("sid8")
    assert ss.restore_session("sid8")
    assert [f.parent.name for f in _shards("sid8")] == [_hash(a)]
    assert ss.load_events("sid8")[0]["content"] == "keep me"
