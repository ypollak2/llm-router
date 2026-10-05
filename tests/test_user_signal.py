"""user_signal ledger: one row per press, private, locked, capped, no free text."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap

import pytest

from llm_router import user_signal as us


def test_row_holds_ts_key_signal_surface_and_nothing_else():
    row = us.record("msg_01ABC", "kept", "terminal", now=1000.0)
    assert row == {"ts": 1000.0, "key": "msg_01ABC", "signal": "kept", "surface": "terminal"}
    lines = us.ledger_path().read_text().splitlines()
    assert [json.loads(x) for x in lines] == [row]
    assert stat.S_IMODE(os.stat(us.ledger_path()).st_mode) == 0o600


@pytest.mark.parametrize("key,signal,surface", [
    ("has space", "kept", "terminal"),
    ("write a poem about my secrets", "kept", "terminal"),
    ("k" * 129, "kept", "terminal"),
    ("", "kept", "terminal"),
    ("msg_1", "loved", "terminal"),
    ("msg_1", "used", "terminal"),        # a press can never say "used"
    ("msg_1", "kept", "Terminal Window"),
    ("msg_1", "kept", ""),
])
def test_free_text_cannot_ride_in(key, signal, surface):
    with pytest.raises(ValueError):
        us.record(key, signal, surface)
    assert not us.ledger_path().exists()


def test_last_press_per_key_wins_and_repeats_count_once():
    us.record("a", "kept", "terminal", now=10.0)
    us.record("a", "kept", "terminal", now=11.0)
    us.record("b", "kept", "desktop", now=12.0)
    us.record("b", "redone", "desktop", now=13.0)
    s = us.summarize(1.0, now=20.0)
    assert s == {"kept": 1, "redone": 1, "presses": 4, "newest_ts": 13.0}


def test_window_excludes_old_rows():
    us.record("old", "redone", "terminal", now=1.0)
    us.record("new", "redone", "terminal", now=86400.0 * 10)
    assert us.summarize(7, now=86400.0 * 10)["redone"] == 1


def test_torn_and_foreign_lines_are_skipped():
    p = us.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"ts": 5, "key": "a", "signal": "kept", "surface": "t"}\n{"ts": 6, "key": "b", "sig\n'
                 '{"ts": 7, "key": "c", "signal": "used", "surface": "t"}\n[1,2]\n')
    assert [r["key"] for r in us.read_rows()] == ["a"]


def test_cap_rotates(monkeypatch):
    monkeypatch.setattr(us, "MAX_BYTES", 400)
    for i in range(20):
        us.record(f"k{i}", "kept", "terminal", now=100.0 + i)
    p = us.ledger_path()
    assert p.with_name(p.name + ".1").exists()
    assert p.stat().st_size <= 400 + 120
    assert len(us.read_rows()) < 20  # older than two generations is dropped: the cap


_WRITER = textwrap.dedent("""
    import sys
    from llm_router import user_signal as us
    worker, n = sys.argv[1], int(sys.argv[2])
    for i in range(n):
        us.record(f"w{worker}-{i}", "kept" if i % 2 else "redone", "terminal")
""")


def test_six_concurrent_writers_no_torn_lines_no_duplicates():
    n = 200
    env = {**os.environ}
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(w), str(n)], env=env) for w in range(6)]
    assert [p.wait(timeout=120) for p in procs] == [0] * 6
    lines = us.ledger_path().read_text().splitlines()
    assert len(lines) == 6 * n
    rows = [json.loads(line) for line in lines]  # a torn line would not parse
    keys = [r["key"] for r in rows]
    assert len(set(keys)) == 6 * n, "no duplicates"
    assert {f"w{w}-{i}" for w in range(6) for i in range(n)} == set(keys), "and none lost"


def test_lock_timeout_is_a_failure_not_an_unlocked_write(monkeypatch):
    import contextlib

    from llm_router import file_lock

    @contextlib.contextmanager
    def never(*_a, **_k):
        yield False

    monkeypatch.setattr(file_lock, "exclusive_lock", never)
    with pytest.raises(OSError):
        us.record("k1", "kept", "terminal")
    assert not us.ledger_path().exists()


def test_write_lock_file_is_0600():
    us.record("msg_1", "kept", "terminal")
    lock = us.ledger_path().with_name(us.LEDGER_FILENAME + ".write.lock")
    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600
