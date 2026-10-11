"""CTX-PROJ-1 (D-57): one session id -> one project shard, whatever the writer's cwd."""

from __future__ import annotations

import hashlib
import json
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


def test_explicit_project_root_wins_when_nothing_pinned(tmp_path):
    b = _repo(tmp_path / "B")
    assert ss._session_path("sid5", str(b)).parent.name == _hash(b)  # no cwd record, no pin


def test_pin_outranks_caller_project_root(tmp_path):
    """MCP/gateway callers pass the session's LATEST cwd (worktree W): readers must still hit A."""
    a = _repo(tmp_path / "A")
    w = _repo(tmp_path / "W")
    ss.write_pointer("sid5b", cwd=str(a))
    ss.record_event("sid5b", "user_prompt", "written under A")
    assert ss._session_path("sid5b", str(w)).parent.name == _hash(a)
    assert [e["content"] for e in ss.load_events("sid5b", project_root=str(w))] == ["written under A"]
    assert "written under A" in ss.build_session_context("sid5b", project_root=str(w))


def test_env_override_beats_pin_and_caller_root(tmp_path, monkeypatch):
    a = _repo(tmp_path / "A")
    w = _repo(tmp_path / "W")
    ss.write_pointer("sid5c", cwd=str(a))
    ss._session_path("sid5c")  # pin
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "forced")
    assert ss._session_path("sid5c", str(w)).parent.name == "forced"


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
    # The pin's creation adopts the early shard (item 3), so only A's file remains live.
    assert [f.parent.name for f in _shards("sid7")] == [_hash(a)]
    assert [e["content"] for e in ss.load_events("sid7")] == ["early", "late"]
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


def _put(sid, root, text, ts, archive=False):
    d = ss._state_dir() / "projects" / _hash(root)
    if archive:
        d = d / "archive"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"session_context_{sid}.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": time.time() - 100 + ts, "role": "user", "content": text, "kind": "user_prompt"}) + "\n")


def test_pin_creation_adopts_predeploy_shards_live_and_archive(tmp_path):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    c = _repo(tmp_path / "C")
    _put("sid9", b, "b1", 1.0)
    _put("sid9", c, "c1", 3.0)
    _put("sid9", b, "b-arch", 0.5, archive=True)
    _put("sid9", c, "c-arch", 0.7, archive=True)
    ss.write_pointer("sid9", cwd=str(a))
    ss.record_event("sid9", "user_prompt", "a-new")  # first resolution creates the pin
    live = _shards("sid9")
    assert [f.parent.name for f in live] == [_hash(a)]
    contents = [e["content"] for e in ss.load_events("sid9")]
    assert contents[:2] == ["b1", "c1"] and "a-new" in contents and len(contents) == 3
    arch = ss._archive_path("sid9")
    assert arch.parent.parent.name == _hash(a)
    assert [json.loads(x)["content"] for x in arch.read_text().splitlines()] == ["b-arch", "c-arch"]
    # sources kept on disk as .premerge: nothing lost
    assert list((ss._state_dir() / "projects").glob("*/session_context_sid9.jsonl.premerge"))
    # second resolution does not re-merge or duplicate
    ss.record_event("sid9", "user_prompt", "a-newer")
    assert len(ss.load_events("sid9")) == 4


def test_pin_adoption_single_foreign_shard_no_dupes(tmp_path):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    _put("sid10", b, "only", 1.0)
    ss.write_pointer("sid10", cwd=str(a))
    assert [e["content"] for e in ss.load_events("sid10")] == ["only"]
    assert [f.parent.name for f in _shards("sid10")] == [_hash(a)]


def test_pin_write_is_atomic_and_complete(tmp_path):
    a = _repo(tmp_path / "A")
    ss.write_pointer("sid11", cwd=str(a))
    ss._session_path("sid11")
    pin = ss._state_dir() / "session_project_sid11.json"
    assert json.loads(pin.read_text())["project_root"] == str(a.resolve())
    assert not list(ss._state_dir().glob(".pin_*"))  # temp file removed


def test_pin_race_loser_reads_winner(tmp_path, monkeypatch):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    ss.write_pointer("sid12", cwd=str(a))
    pin = ss._state_dir() / "session_project_sid12.json"
    real_link = ss.os.link

    def racing_link(src, dst):
        pin.write_text(json.dumps({"project_root": str(b.resolve()), "ts": 1}))  # winner sneaks in
        return real_link(src, dst)  # raises FileExistsError

    monkeypatch.setattr(ss.os, "link", racing_link)
    assert ss._session_path("sid12").parent.name == _hash(b)


def _age(path, days):
    t = time.time() - days * 86400
    ss.os.utime(path, (t, t))


def test_cleanup_sweeps_orphan_pin_and_cwd_record(tmp_path):
    a = _repo(tmp_path / "A")
    sd = ss._state_dir()
    for sid in ("dead", "alive", "arch", "young"):
        ss.write_pointer(sid, cwd=str(a))
        ss._session_path(sid)  # pin
    ss.record_event("alive", "user_prompt", "x")
    ss.record_event("arch", "user_prompt", "x")
    ss.archive_session("arch")
    for sid in ("dead", "alive", "arch"):
        for f in (sd / f"session_project_{sid}.json", sd / f"session_cwd_{sid}.json"):
            _age(f, ss._TTL_DAYS + 1)
    ss.cleanup_old_sessions()
    assert not (sd / "session_project_dead.json").exists()
    assert not (sd / "session_cwd_dead.json").exists()
    for sid in ("alive", "arch", "young"):  # has a log / archive, or younger than the TTL
        assert (sd / f"session_project_{sid}.json").exists()
        assert (sd / f"session_cwd_{sid}.json").exists()


def test_cleanup_ages_out_premerge_and_merging_leftovers(tmp_path):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    _put("sid13", b, "b1", 1.0)
    _put("sid13", b, "b-arch", 0.5, archive=True)
    ss.write_pointer("sid13", cwd=str(a))
    ss.record_event("sid13", "user_prompt", "a")  # adoption leaves *.premerge in B
    projects = ss._state_dir() / "projects"
    left = list(projects.glob("*/session_context_sid13.jsonl.premerge")) \
        + list(projects.glob("*/archive/session_context_sid13.jsonl.premerge"))
    stray = projects / _hash(a) / "session_context_sid13.jsonl.merging"
    stray.write_text("{}\n")
    left.append(stray)
    assert len(left) == 3
    ss.cleanup_old_sessions()
    assert all(p.exists() for p in left)  # young: kept
    for p in left:
        _age(p, ss._TTL_DAYS + 1)
    ss.cleanup_old_sessions()
    assert not any(p.exists() for p in left)


def test_merge_skips_a_source_whose_lock_is_held(tmp_path):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    c = _repo(tmp_path / "C")
    _put("sid14", b, "b1", 1.0)
    _put("sid14", c, "c1", 2.0)
    src_b = ss._state_dir() / "projects" / _hash(b) / "session_context_sid14.jsonl"
    src_c = ss._state_dir() / "projects" / _hash(c) / "session_context_sid14.jsonl"
    target = ss._state_dir() / "projects" / _hash(a) / "session_context_sid14.jsonl"
    from llm_router.file_lock import exclusive_lock
    with exclusive_lock(ss._lock_path(src_b)) as got:  # a writer is mid-append on B
        assert got
        rep = ss.merge_session_shards("sid14", apply=True, paths=[src_b, src_c], target=target)
    assert rep["applied"]
    assert src_b.exists() and not src_b.with_suffix(".jsonl.premerge").exists()  # skipped, in place
    assert not src_c.exists() and src_c.with_suffix(".jsonl.premerge").exists()
    assert [json.loads(x)["content"] for x in target.read_text().splitlines()] == ["c1"]
    # B's data is not lost: the next merge picks it up
    ss.merge_session_shards("sid14", apply=True, paths=[src_b, target], target=target)
    assert [json.loads(x)["content"] for x in target.read_text().splitlines()] == ["b1", "c1"]


def test_stale_path_writer_retries_on_the_new_path(tmp_path, monkeypatch):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    ss.write_pointer("sid15", cwd=str(a))
    ss._session_path("sid15")  # pin to A
    stale = ss._state_dir() / "projects" / _hash(b) / "session_context_sid15.jsonl"
    real = ss._session_path
    calls = {"n": 0}

    def fake(sid, root=None):
        calls["n"] += 1
        return stale if calls["n"] == 1 else real(sid, root)  # writer resolved before the pin moved it

    monkeypatch.setattr(ss, "_session_path", fake)
    ss.record_event("sid15", "user_prompt", "late writer")
    monkeypatch.setattr(ss, "_session_path", real)
    assert not stale.exists()
    assert [f.parent.name for f in _shards("sid15")] == [_hash(a)]
    assert [e["content"] for e in ss.load_events("sid15")] == ["late writer"]


def test_merge_keeps_raw_bytes_non_ascii_and_unparseable_lines(tmp_path):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    now = time.time()
    ev = json.dumps({"ts": now - 10, "role": "user", "content": "שלום café", "kind": "user_prompt"})
    d = ss._state_dir() / "projects" / _hash(b)
    d.mkdir(parents=True)
    (d / "session_context_sid16.jsonl").write_text(ev + "\n{torn line\n")
    ss.write_pointer("sid16", cwd=str(a))
    ss.record_event("sid16", "user_prompt", "after")
    lines = ss._session_path("sid16").read_text().splitlines()
    assert lines[0] == ev            # byte-identical: ensure_ascii escapes preserved
    assert lines[1] == "{torn line"  # kept raw, in place
    assert json.loads(lines[2])["content"] == "after"
