"""H-06 — the quota file was rewritten non-atomically while ten hooks read it.

`_write_to_disk` used `Path.write_text`, which truncates the file and then
writes. Every reader that opens during that window sees a partial or empty file.
Measured under concurrency by the 2026-09-21 audit: **32-38% read failure**.

That is not a telemetry inconvenience. Ten hooks consume `usage.json` on the
routing hot path, and a failed read degrades to a conservative 50% quota
assumption — so roughly a third of the time, routing decisions were being made
from a torn file, and nothing recorded that it had happened.

The correct pattern already existed in this repository three times over:
`install_hooks.py`, `file_lock.py`, and `budget_backend.py` — which the audit
named as the reference for cross-process correctness. It had simply never been
applied here. That is the shape of most findings in this audit: not "write the
fix", but "finish adopting the fix that already exists".

`os.replace` is atomic on POSIX, so a reader sees either the whole old file or
the whole new one. `fsync` before the rename matters too: the rename being atomic
says nothing about the *content* being durable, and a crash between write and
rename would otherwise publish a valid-looking name over empty bytes.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time

import pytest

_PAYLOAD = json.dumps({
    "session_pct": 0.4, "weekly_pct": 0.2, "sonnet_pct": 0.1,
    "updated_at": time.time(), "is_fresh": True,
})


def _hammer_reads(target: pathlib.Path, writer, n: int = 2000) -> float:
    """Fraction of reads that could not parse, while *writer* rewrites in a loop."""
    target.write_text(_PAYLOAD, encoding="utf-8")
    stop = threading.Event()
    t = threading.Thread(target=writer, args=(target, stop), daemon=True)
    t.start()
    try:
        bad = 0
        for _ in range(n):
            try:
                json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                bad += 1
        return bad / n
    finally:
        stop.set()
        t.join(timeout=2)


def _non_atomic_writer(target: pathlib.Path, stop: threading.Event) -> None:
    while not stop.is_set():
        target.write_text(_PAYLOAD, encoding="utf-8")


def _atomic_writer(target: pathlib.Path, stop: threading.Event) -> None:
    while not stop.is_set():
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(_PAYLOAD)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)


def test_the_old_pattern_really_does_tear(tmp_path):
    """Anti-vacuity, and the reason this test exists at all.

    If the non-atomic writer could not be made to fail here, the atomic test
    below would be proving nothing about anything.
    """
    rate = _hammer_reads(tmp_path / "usage.json", _non_atomic_writer)
    assert rate > 0.0, (
        "the non-atomic writer produced zero torn reads on this machine, so the "
        "atomic assertion below cannot demonstrate an improvement"
    )


def test_atomic_rename_eliminates_torn_reads(tmp_path):
    rate = _hammer_reads(tmp_path / "usage.json", _atomic_writer)
    assert rate == 0.0, f"{rate:.1%} of reads were torn despite the atomic rename"


async def test_quota_tracker_writes_atomically(tmp_path, monkeypatch):
    """The production path, not a reimplementation of it."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.quota_tracker import QuotaSnapshot, QuotaTracker

    tracker = QuotaTracker()
    snap = QuotaSnapshot(
        claude_session_pct=0.3, claude_weekly_pct=0.2, claude_sonnet_pct=0.1,
        openai_spent_usd=0.0, gemini_spent_usd=0.0, ollama_available=False,
        cache_age_seconds=0.0, is_fresh=True, refreshed_at=time.time(),
    )
    await tracker._write_to_disk(snap)

    target = pathlib.Path(QuotaTracker.USAGE_JSON)
    assert target.exists(), "nothing was written — this test would be vacuous"
    assert json.loads(target.read_text())["session_pct"] == pytest.approx(0.3)

    leftovers = list(target.parent.glob("usage.json.*.tmp"))
    assert not leftovers, f"temp files left behind: {leftovers}"


async def test_repeated_writes_leave_no_temp_litter(tmp_path, monkeypatch):
    """A temp file per write must not accumulate for `gc` to puzzle over."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.quota_tracker import QuotaSnapshot, QuotaTracker

    tracker = QuotaTracker()
    for i in range(20):
        await tracker._write_to_disk(QuotaSnapshot(
            claude_session_pct=i / 100, claude_weekly_pct=0.0, claude_sonnet_pct=0.0,
            openai_spent_usd=0.0, gemini_spent_usd=0.0, ollama_available=False,
            cache_age_seconds=0.0, is_fresh=True, refreshed_at=time.time(),
        ))

    target = pathlib.Path(QuotaTracker.USAGE_JSON)
    assert json.loads(target.read_text())["session_pct"] == pytest.approx(0.19)
    assert not list(target.parent.glob("*.tmp"))


def test_the_write_path_uses_the_established_idiom():
    """Pin the mechanism, not just the outcome.

    A future refactor back to `write_text` would pass a single-threaded test and
    reintroduce a defect that only appears under concurrent readers.
    """
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src" / "llm_router" / "quota_tracker.py").read_text(encoding="utf-8")
    body = src.split("async def _write_to_disk")[1].split("\n    async def ")[0]
    assert "os.replace" in body, "_write_to_disk no longer renames atomically"
    assert "fsync" in body, "content is renamed into place without being made durable"
    assert ".write_text(" not in body, "_write_to_disk is truncating in place again"
