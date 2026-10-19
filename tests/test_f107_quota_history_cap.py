"""F107: quota_history.jsonl is bounded and quota_rate reads only the tail it needs."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from llm_router import quota_rate, statusline_tick as tick

NOW = 1_791_234_000.0
DAY = 86400.0


def _row(ts: float, wk: float, source: str = "measured") -> dict:
    return {"ts": ts, "five_hour_pct": 1.0, "weekly_pct": wk, "updated_at": ts,
            "five_hour_resets_at": None, "source": source}


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def _rows(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _series(days: int, step: float = 300.0) -> list[dict]:
    n = int(days * DAY / step)
    return [_row(NOW - (n - i) * step, (i * 0.05) % 100.0) for i in range(n)]


def test_prune_keeps_window_and_drops_older(tmp_path):
    hist = tmp_path / tick.HISTORY_NAME
    rows = _series(60)
    _write(hist, rows)
    assert tick.prune_history(str(tmp_path), NOW) is True
    kept = _rows(hist)
    expect = [r for r in rows if r["ts"] >= NOW - 30 * DAY]
    assert kept == expect and 0 < len(kept) < len(rows)
    assert oct(hist.stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob("*.tmp"))


def test_prune_row_cap_keeps_newest(tmp_path):
    hist = tmp_path / tick.HISTORY_NAME
    rows = [_row(NOW - 100 + i, 1.0) for i in range(50)]
    _write(hist, rows)
    assert tick.prune_history(str(tmp_path), NOW, max_rows=10) is True
    assert _rows(hist) == rows[-10:]


def test_prune_once_a_day_and_nothing_to_drop_does_not_rewrite(tmp_path):
    hist = tmp_path / tick.HISTORY_NAME
    _write(hist, _series(2))
    before = hist.stat().st_ino
    assert tick.prune_history(str(tmp_path), NOW) is False        # nothing older than 30 d
    assert hist.stat().st_ino == before
    _write(hist, _series(60))
    assert tick.prune_history(str(tmp_path), NOW + 3600) is False  # stamped less than a day ago
    assert len(_rows(hist)) > 8640
    assert tick.prune_history(str(tmp_path), NOW + DAY + 1) is True


def test_prune_skips_while_a_writer_holds_the_slot_lock(tmp_path):
    hist = tmp_path / tick.HISTORY_NAME
    _write(hist, _series(60))
    n = len(_rows(hist))
    fd = os.open(tmp_path / tick.HISTORY_STAMP_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert tick.prune_history(str(tmp_path), NOW) is False
        assert len(_rows(hist)) == n
    finally:
        os.close(fd)
    assert tick.prune_history(str(tmp_path), NOW) is True          # not stamped: retried


def test_append_during_prune_is_not_lost(tmp_path, monkeypatch):
    """A writer that arrives while the prune holds the lock skips its slot (stamp not advanced);
    the next tick writes it. After both, the new row is in the pruned file."""
    hist = tmp_path / tick.HISTORY_NAME
    _write(hist, _series(60))
    usage = {"session_pct": 5.0, "weekly_pct": 7.0, "updated_at": NOW}
    during: list[bool] = []
    real_replace = os.replace

    def replace(src, dst):
        during.append(tick.maybe_append_history(str(tmp_path), usage, NOW))   # lock is held here
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    assert tick.prune_history(str(tmp_path), NOW) is True
    monkeypatch.undo()
    assert during == [False]                                        # skipped, not interleaved
    assert tick.maybe_append_history(str(tmp_path), usage, NOW + 1) is True
    last = _rows(hist)[-1]
    assert last["ts"] == NOW + 1 and last["weekly_pct"] == 7.0


def test_quota_rate_tail_read_equals_full_scan(tmp_path):
    hist = tmp_path / tick.HISTORY_NAME
    rows = []
    wk = 0.0
    for i in range(int(60 * DAY / 300)):
        wk = wk + 0.2 if i % 3 else wk
        if i % 2016 == 0:
            wk = 0.0
        rows.append(_row(NOW - (int(60 * DAY / 300) - i) * 300.0, wk))
    _write(hist, rows)
    with hist.open("a") as fh:
        fh.write("not json\n")
    # reference: the old whole-file semantics
    ref = [r for r in rows if r["ts"] >= NOW - 14 * DAY]
    got, bad = quota_rate._read(hist, NOW - 14 * DAY)
    assert got == ref and bad == 1
    assert hist.stat().st_size > 3 * quota_rate._TAIL_CHUNK        # the tail path really ran
    full = quota_rate.median_pp_per_active_hour(now=NOW, path=hist)
    _write(tmp_path / "ref.jsonl", ref)
    want = quota_rate.median_pp_per_active_hour(now=NOW, path=tmp_path / "ref.jsonl")
    assert (full.value, full.n) == (want.value, want.n) and full.value is not None
    assert full.bad_rows == 1 and want.bad_rows == 0


def test_quota_rate_tail_read_small_and_odd_files(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text("")
    assert quota_rate._read(p, 0.0) == ([], 0)
    p.write_text(json.dumps(_row(NOW, 1.0)))                       # no trailing newline
    assert quota_rate._read(p, 0.0)[0] == [_row(NOW, 1.0)]
    assert quota_rate._read(p, NOW + 1)[0] == []
