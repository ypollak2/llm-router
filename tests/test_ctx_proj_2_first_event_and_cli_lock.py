"""CTX-PROJ-2: first-event race strands events; CLI merge did not lock its target."""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from llm_router import session_store as ss
from llm_router.file_lock import exclusive_lock


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    for v in ("LLM_ROUTER_PROJECT_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
              "LLM_ROUTER_SESSION_CONTEXT"):
        monkeypatch.delenv(v, raising=False)


def _repo(path):
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def _hash(p):
    return hashlib.sha1(str(p.resolve()).encode(), usedforsecurity=False).hexdigest()[:16]


def _contents(path):
    return [json.loads(x)["content"] for x in path.read_text().splitlines() if x.strip()]


def test_first_event_race_events_reach_pinned_shard(tmp_path, monkeypatch):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    monkeypatch.chdir(b)
    ss.record_event("sidr1", "user_prompt", "early")  # old shard under hash(B)
    old = ss._state_dir() / "projects" / _hash(b) / "session_context_sidr1.jsonl"
    assert old.exists()
    real = ss._session_path
    calls = {"n": 0}

    def fake(sid, root=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # first resolve: no pin yet; the hook's cwd record appears right after it
            p = real(sid, root)
            ss.write_pointer(sid, cwd=str(a))
            return p
        return real(sid, root)

    monkeypatch.setattr(ss, "_session_path", fake)
    ss.record_event("sidr1", "user_prompt", "racing")
    monkeypatch.setattr(ss, "_session_path", real)

    pinned = ss._state_dir() / "projects" / _hash(a) / "session_context_sidr1.jsonl"
    assert _contents(pinned) == ["early", "racing"]  # no `llm-router sessions merge` needed
    assert not old.exists() and old.with_suffix(".jsonl.premerge").exists()


def test_cli_merge_holds_target_lock_and_loses_no_append(tmp_path):
    a = _repo(tmp_path / "A")
    b = _repo(tmp_path / "B")
    ss.write_pointer("sidr2", cwd=str(a))
    target = ss._session_path("sidr2")  # pins to A
    src = ss._state_dir() / "projects" / _hash(b) / "session_context_sidr2.jsonl"
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps({"ts": 1.0, "role": "user", "content": "from-b"}) + "\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"ts": 2.0, "role": "user", "content": "in-a"}) + "\n")

    done = threading.Event()
    result: dict = {}

    def _cli():
        result["rep"] = ss.merge_session_shards("sidr2", apply=True, paths=[src, target])
        done.set()

    with exclusive_lock(ss._lock_path(target)) as got:  # a concurrent writer is mid-append
        assert got
        t = threading.Thread(target=_cli)
        t.start()
        time.sleep(0.4)
        assert not done.is_set(), "CLI merge must wait for the target lock"
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": 3.0, "role": "user", "content": "appended"}) + "\n")
    t.join(10)
    assert done.is_set() and result["rep"]["applied"]
    assert _contents(target) == ["from-b", "in-a", "appended"]
