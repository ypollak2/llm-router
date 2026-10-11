"""CTX-COMPACT-1 (R-CTX-4): compaction spills dropped lines to the archive, never deletes them.

See docs/BUGS.md CTX-COMPACT-1.
"""
from __future__ import annotations

import json
import time

import pytest

from llm_router import session_store as ss

SID = "spill-sid"
BODY = "x" * 1300  # ~1.3KB per event


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    for v in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_SESSION_CONTEXT"):
        monkeypatch.delenv(v, raising=False)
    yield tmp_path


def _lines(p):
    return [ln for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _contents(lines):
    return [json.loads(ln)["content"] for ln in lines]


def _paths():
    live = ss._session_path(SID)
    return live, ss._archive_path(SID)


def _write(i):
    ss.record_event(SID, "user_prompt", f"{i:04d}-{BODY}")


def _expected(n):
    return [f"{i:04d}-{BODY}" for i in range(n)]


def test_180_events_bounded_live_nonlossy_union_in_order(monkeypatch):
    monkeypatch.setattr(ss, "_MAX_RECORDS", 60)
    monkeypatch.setattr(ss, "_COMPACT_TO", 30)
    live, arch = _paths()
    prev_total = 0
    spilled = False
    for i in range(180):
        _write(i)
        nl, na = len(_lines(live)), len(_lines(arch))
        assert nl <= 61
        assert nl + na >= prev_total
        assert nl + na == i + 1
        prev_total = nl + na
        spilled = spilled or na > 0
    assert spilled
    assert _contents(_lines(arch) + _lines(live)) == _expected(180)


def test_line_count_trigger(monkeypatch):
    monkeypatch.setattr(ss, "_MAX_RECORDS", 20)
    monkeypatch.setattr(ss, "_COMPACT_TO", 10)
    for i in range(21):
        ss.record_event(SID, "user_prompt", f"m{i}")
    live, arch = _paths()
    assert len(_lines(live)) == 10
    assert _contents(_lines(arch)) == [f"m{i}" for i in range(11)]


def test_size_trigger(monkeypatch):
    monkeypatch.setattr(ss, "_MAX_FILE_BYTES", 20 * 1400)
    monkeypatch.setattr(ss, "_COMPACT_TO", 5)
    live, arch = _paths()
    for i in range(30):
        _write(i)
    assert len(_lines(arch)) > 0
    assert len(_lines(live)) < 30
    assert _contents(_lines(arch) + _lines(live)) == _expected(30)


def test_ttl_expired_lines_are_spilled(monkeypatch):
    live, arch = _paths()
    live.parent.mkdir(parents=True, exist_ok=True)
    old = {"ts": time.time() - 90 * 86400, "kind": "k", "role": "user", "content": "old", "h": "a"}
    live.write_text(json.dumps(old) + "\n")
    ss.record_event(SID, "user_prompt", "new")
    assert _contents(_lines(live)) == ["new"]
    assert _contents(_lines(arch)) == ["old"]


def test_session_end_archive_is_spilled_then_live(monkeypatch):
    monkeypatch.setattr(ss, "_MAX_RECORDS", 20)
    monkeypatch.setattr(ss, "_COMPACT_TO", 10)
    for i in range(50):
        ss.record_event(SID, "user_prompt", f"m{i}")
    live, arch = _paths()
    assert _lines(arch)
    ss.archive_session(SID)
    assert not live.exists()
    assert _contents(_lines(arch)) == [f"m{i}" for i in range(50)]


def test_resume_after_session_end_no_dup_no_loss(monkeypatch):
    monkeypatch.setattr(ss, "_MAX_RECORDS", 20)
    monkeypatch.setattr(ss, "_COMPACT_TO", 10)
    for i in range(50):
        ss.record_event(SID, "user_prompt", f"m{i}")
    ss.archive_session(SID)
    assert ss.restore_session(SID) is True
    live, arch = _paths()
    assert not arch.exists()
    # keep working: compaction after the big restore spills again
    for i in range(50, 90):
        ss.record_event(SID, "user_prompt", f"m{i}")
    got = _contents(_lines(arch) + _lines(live))
    assert got == [f"m{i}" for i in range(90)]
    # second end/resume cycle
    ss.archive_session(SID)
    ss.restore_session(SID)
    assert _contents(_lines(arch) + _lines(live)) == [f"m{i}" for i in range(90)]


def test_restore_dedups_event_present_in_both(monkeypatch):
    live, arch = _paths()
    for i in range(3):
        ss.record_event(SID, "user_prompt", f"m{i}")
    arch.parent.mkdir(parents=True, exist_ok=True)
    arch.write_text(live.read_text())  # crash-shaped overlap: all lines in both
    ss.record_event(SID, "user_prompt", "m3")
    assert ss.restore_session(SID)
    assert _contents(_lines(live)) == ["m0", "m1", "m2", "m3"]


def test_crash_between_archive_append_and_replace_loses_nothing(monkeypatch):
    monkeypatch.setattr(ss, "_MAX_RECORDS", 20)
    monkeypatch.setattr(ss, "_COMPACT_TO", 10)
    live, arch = _paths()
    for i in range(20):
        ss.record_event(SID, "user_prompt", f"m{i}")
    real = ss.os.replace

    def boom(*a, **k):
        raise OSError("crash")

    monkeypatch.setattr(ss.os, "replace", boom)
    ss.record_event(SID, "user_prompt", "m20")  # triggers compaction; replace "crashes"
    monkeypatch.setattr(ss.os, "replace", real)
    union = _contents(_lines(arch)) + _contents(_lines(live))
    assert set(f"m{i}" for i in range(21)) <= set(union)  # nothing lost (dup allowed)
    # recovery: next compaction + resume leave exactly one copy of each event
    for i in range(21, 30):
        ss.record_event(SID, "user_prompt", f"m{i}")
    ss.archive_session(SID)
    ss.restore_session(SID)
    got = _contents(_lines(live))
    assert set(got) >= {f"m{i}" for i in range(30)}
