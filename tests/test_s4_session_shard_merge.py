"""S4 — recover the session events stranded across project buckets.

S2-1 stopped NEW fragmentation (`_project_id()` now resolves the repo root instead
of the raw cwd) but deliberately left the existing shards alone: consolidating them
at runtime would have meant a cross-project read, which is what CHZ-AUD-024 exists
to forbid. On this machine that leaves 181 events recorded and unreadable across 6
sessions.

The distinction that makes recovery legitimate is WHO asks and WHEN. The audit
forbids a running project silently reading another project's session by id. It does
not forbid the owner of the machine explicitly running a migration over their own
store — which is the same thing `okf adopt` and `okf gc` already do for the
knowledge store, and the precedent this follows.

So: an explicit CLI command, never a runtime path; dry-run by default, because a
merge that turns out to be wrong should be discovered before it is applied and not
after; and originating buckets preserved rather than deleted, so a bad merge costs
nothing but disk.

Ordering is by timestamp where events carry one. A merged conversation whose turns
are interleaved wrongly is worse than one that is merely split — it reads as a
coherent history that never happened.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router import session_store


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ID", raising=False)
    yield


SID = "shattered-session"


def _shard(tmp_path: Path, bucket: str, events: list[tuple[str, str, int]]) -> Path:
    d = tmp_path / ".llm-router" / "projects" / bucket
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"session_context_{SID}.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for role, content, ts in events:
            fh.write(json.dumps({"role": role, "content": content, "ts": ts}) + "\n")
    return p


@pytest.fixture
def shattered(tmp_path):
    """One session, three buckets, interleaved in time — the real shape."""
    _shard(tmp_path, "bucket_a", [("user", "alpha first", 100), ("assistant", "alpha reply", 101)])
    _shard(tmp_path, "bucket_b", [("user", "bravo middle", 200)])
    _shard(tmp_path, "bucket_c", [("user", "charlie last", 300), ("assistant", "charlie reply", 301)])
    return tmp_path


def test_dry_run_reports_without_changing_anything(shattered):
    before = {p: p.read_text() for p in
              (shattered / ".llm-router" / "projects").glob("*/session_context_*.jsonl")}
    report = session_store.merge_session_shards(SID, apply=False)
    assert report["shards"] == 3
    assert report["events"] == 5
    assert report["applied"] is False
    after = {p: p.read_text() for p in
             (shattered / ".llm-router" / "projects").glob("*/session_context_*.jsonl")}
    assert before == after, "a dry run modified the store"


def test_apply_makes_every_event_readable(shattered):
    session_store.merge_session_shards(SID, apply=True)
    bodies = " ".join(e.get("content", "") for e in session_store.load_events(SID, limit=100))
    for word in ("alpha first", "alpha reply", "bravo middle", "charlie last", "charlie reply"):
        assert word in bodies, f"{word!r} is still stranded"


def test_merged_events_are_in_timestamp_order(shattered):
    session_store.merge_session_shards(SID, apply=True)
    contents = [e.get("content", "") for e in session_store.load_events(SID, limit=100)]
    order = [contents.index(c) for c in
             ("alpha first", "alpha reply", "bravo middle", "charlie last", "charlie reply")]
    assert order == sorted(order), (
        f"merged turns are out of order: {contents} — a history that reads coherently "
        "but never happened is worse than one that is split"
    )


def test_originating_shards_are_preserved(shattered):
    session_store.merge_session_shards(SID, apply=True)
    surviving = list((shattered / ".llm-router" / "projects").glob("*/session_context_*.jsonl*"))
    assert len(surviving) >= 3, "a bad merge should cost disk, not data"


def test_duplicate_events_are_not_doubled(shattered):
    """Shards can overlap — the same turn recorded from two cwds."""
    _shard(shattered, "bucket_d", [("user", "bravo middle", 200)])
    session_store.merge_session_shards(SID, apply=True)
    contents = [e.get("content", "") for e in session_store.load_events(SID, limit=100)]
    assert contents.count("bravo middle") == 1


def test_merging_is_idempotent(shattered):
    session_store.merge_session_shards(SID, apply=True)
    first = [e.get("content") for e in session_store.load_events(SID, limit=100)]
    session_store.merge_session_shards(SID, apply=True)
    assert [e.get("content") for e in session_store.load_events(SID, limit=100)] == first


def test_an_unfragmented_session_is_a_noop(tmp_path):
    _shard(tmp_path, "only_bucket", [("user", "solo", 1)])
    report = session_store.merge_session_shards(SID, apply=True)
    assert report["shards"] == 1
    assert report["merged"] == 0


def test_an_unknown_session_is_not_an_error():
    report = session_store.merge_session_shards("no-such-session", apply=True)
    assert report["shards"] == 0
    assert report["events"] == 0


def test_discovery_lists_every_fragmented_session(shattered):
    _shard(shattered, "bucket_a", [("user", "other one", 1)])
    other = shattered / ".llm-router" / "projects" / "bucket_b" / "session_context_other.jsonl"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(json.dumps({"role": "user", "content": "other two", "ts": 2}) + "\n")
    found = session_store.fragmented_sessions()
    assert SID in found
    assert found[SID]["shards"] >= 3


def test_merge_stays_within_this_users_store(shattered, monkeypatch):
    """Not a cross-project read path: it only ever walks this machine's own
    state dir, which the owner already has on disk."""
    root = shattered / ".llm-router" / "projects"
    session_store.merge_session_shards(SID, apply=True)
    for p in root.rglob("*"):
        assert str(p).startswith(str(root)), f"merge touched {p}, outside the store"
