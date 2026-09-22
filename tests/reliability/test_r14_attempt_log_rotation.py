"""R14 — `attempt_log` rotation erased concurrent appends.

`_rotate` was `read_text()` then `write_text()` with nothing between them. Any
record another process appended in that window was gone: the write truncated the
file and rewrote it from a snapshot taken before the append existed.

Measured before the fix, 8 processes, 400 records each:

    run 1   3200 written   3183 survived   0.53% lost
    run 2   3200 written   3032 survived   5.25% lost
    run 3   3200 written   3178 survived   0.69% lost

Note the variance. A single run at 0.53% looks like a rounding artefact, which
is most of why this survived — the loss rate depends entirely on how many
appends happen to fall inside a rotation window, so the defect is invisible in
any test that does not push enough concurrent writers at it.

None of it was detectable downstream. The file stayed valid JSONL, `summary()`
returned sensible per-model numbers, and nothing logged. The only symptom was a
total that did not add up, and nothing was checking the total.

This is the same defect `file_lock` was written for (`session_store`, 1.83% at
6 processes, CHZ-AUD-C-01). The pattern existed; this module never got it.

The tests below spawn real processes on purpose. They have to actually trigger
rotation — `_rotate` returns unless the file is over 400 KB AND over 5000 lines
— so they write enough to cross both thresholds. They are NOT marked `slow`:
this suite excludes `slow` by default, and a concurrency gate that does not run
is the same as no gate. They cost about 3 seconds in total.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest

PROCS = 8
#: PROCS*PER_PROC must exceed `_MAX_LINES` (5000) — rotation needs BOTH
#: 400 KB and more than 5000 lines, and the first draft of this file satisfied
#: only the byte threshold. 3200 fat records crossed 400 KB and never rotated,
#: so all three "no loss" runs passed against code that still had the defect.
#: `test_rotation_actually_fired` is what caught that, which is the entire
#: argument for writing the check-on-the-check before trusting a green run.
PER_PROC = 900
#: And the bytes: 7200 records at ~400 B each is ~2.9 MB, comfortably over the
#: 400 KB floor with room for both thresholds to be met several times over.
_PAD = "x" * 300


def _worker(home: str, worker_id: int, barrier) -> None:
    os.environ["LLM_ROUTER_HOME"] = home
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from llm_router import attempt_log

    barrier.wait()  # every process starts writing at the same instant
    for i in range(PER_PROC):
        attempt_log.record(
            f"model-{worker_id}", attempt_log.OK, i, reason=f"{worker_id}:{i}:{_PAD}"
        )


def _run_once(home: Path) -> tuple[int, int, list[str]]:
    """Returns (written, survived, malformed_lines)."""
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(PROCS)
    procs = [
        ctx.Process(target=_worker, args=(str(home), w, barrier))
        for w in range(PROCS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=180)
        assert p.exitcode == 0, f"worker failed: exitcode={p.exitcode}"

    log = home / "attempts.jsonl"
    text = log.read_text(errors="replace") if log.exists() else ""
    survived = 0
    malformed: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            malformed.append(line[:120])
            continue
        if not isinstance(rec, dict) or "model" not in rec:
            malformed.append(line[:120])
    # Rotation KEEPS only the last 2500 lines by design, so "survived" cannot
    # be compared against everything written. What must hold is that no record
    # is lost from the window rotation promised to keep.
    survived = len([ln for ln in text.splitlines() if ln.strip()])
    return (PROCS * PER_PROC, survived, malformed)


@pytest.mark.parametrize("run", [1, 2, 3])
def test_no_record_is_lost_from_the_retained_window(tmp_path, run):
    """Three consecutive runs, zero loss inside the retained window.

    Rotation is *supposed* to drop old records — that is its job — so the
    assertion cannot be "everything written is still there". It is the stronger
    and more specific claim that survives rotation being correct: whatever the
    file ends up holding is a CONTIGUOUS SUFFIX of what was written, with no
    holes punched in the middle by a concurrent truncation.

    A hole is exactly what the unlocked version produced: records N and N+2
    present, N+1 gone, because N+1 landed between a rotator's read and its
    write.
    """
    from llm_router import attempt_log

    home = tmp_path / f"run{run}"
    home.mkdir()
    written, survived, malformed = _run_once(home)

    assert not malformed, (
        f"{len(malformed)} malformed line(s) — a concurrent write tore a record "
        f"in half: {malformed[:3]}"
    )
    assert survived > 0, "the log is empty; nothing was written at all"
    assert survived <= attempt_log._MAX_LINES + PROCS, (
        f"{survived} lines retained but rotation should cap near "
        f"{attempt_log._MAX_LINES}; rotation never ran, so this run did not "
        "exercise the defect"
    )

    # Per worker, the ids present must be a contiguous run ending at its last
    # write — no gaps. A gap is a record erased by someone else's rotation.
    log = home / "attempts.jsonl"
    seen: dict[str, list[int]] = {}
    for line in log.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        w, i, _ = rec["reason"].split(":", 2)
        seen.setdefault(w, []).append(int(i))

    holes = {}
    for w, ids in seen.items():
        ids.sort()
        expected = list(range(ids[0], ids[-1] + 1))
        if ids != expected:
            holes[w] = sorted(set(expected) - set(ids))[:10]
    assert not holes, (
        f"records erased mid-sequence by a concurrent rotation: {holes}. "
        f"(written={written}, retained={survived})"
    )


def test_rotation_actually_fired(tmp_path):
    """The check on the check.

    Every assertion above is satisfied trivially by a run in which rotation
    never triggered — and the unlocked code passes such a run too. If the
    workload stops crossing the size threshold (someone shrinks `_PAD`, or
    raises the 400 KB floor), this fails and says so instead of letting the
    suite report a green that proves nothing.
    """
    home = tmp_path / "fired"
    home.mkdir()
    written, survived, _ = _run_once(home)
    assert survived < written, (
        f"{written} records written and {survived} retained — rotation never "
        "trimmed anything, so this file's tests did not exercise rotation at "
        "all. Increase _PAD or PER_PROC."
    )


def test_rotation_is_skipped_rather_than_run_unlocked(tmp_path, monkeypatch):
    """When the lock cannot be taken, keep the append and skip the trim.

    The two halves fail in opposite directions and must not be treated alike:
    an unlocked append is atomic and safe, an unlocked rotate is the data loss.
    Refusing the append to avoid the rotation would lose the record for certain
    in order to avoid losing it by chance.
    """
    import contextlib

    from llm_router import attempt_log, failopen

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    failopen.clear()

    rotated: list[Path] = []
    monkeypatch.setattr(attempt_log, "_rotate", lambda p: rotated.append(p))

    @contextlib.contextmanager
    def _never_acquires(_lock_path, timeout=0.0):
        yield False

    monkeypatch.setattr("llm_router.file_lock.exclusive_lock", _never_acquires)
    attempt_log.record("m", attempt_log.OK, 5, reason="kept anyway")

    log = tmp_path / "attempts.jsonl"
    assert log.exists() and "kept anyway" in log.read_text(), (
        "the append was dropped because the lock was unavailable — that loses "
        "the record for certain to avoid losing it by chance"
    )
    assert not rotated, "rotation ran without the lock"

    failopen.reset_cache()
    counts = failopen.snapshot()
    assert counts.by_code.get("CHZ-FO-ATTEMPTLOG-ROTATE-UNLOCKED") == 1, (
        "a skipped rotation left no trace; 'rotation has not run for a week' "
        f"must be distinguishable from 'the log is not large yet': {counts.by_code}"
    )


def test_a_reader_never_sees_a_half_written_log(tmp_path, monkeypatch):
    """Rotation lands via os.replace, so `summary()` cannot read a torn file.

    `write_text` truncated in place. A `summary()` call landing between the
    truncate and the write read a file whose old content was gone and whose new
    content had not arrived, and reported a model with thousands of records as
    having no evidence at all — which a caller is told to treat as "no
    evidence", never as "bad", so the failure was silent AND load-bearing.
    """
    from llm_router import attempt_log

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    log = tmp_path / "attempts.jsonl"
    log.write_text(
        "".join(
            json.dumps({"ts": 0, "model": "m", "outcome": "ok",
                        "latency_ms": 1, "reason": "y" * 400}) + "\n"
            for _ in range(attempt_log._MAX_LINES + 50)
        )
    )
    before_inode = log.stat().st_ino

    observed: list[int] = []
    real_replace = os.replace

    def _watch(src, dst):
        # At the instant of the swap the destination must still be the FULL old
        # file — never truncated, never partial.
        observed.append(len(Path(dst).read_text(errors="replace").splitlines()))
        return real_replace(src, dst)

    monkeypatch.setattr(attempt_log.os, "replace", _watch)
    attempt_log._rotate(log)

    assert observed, "rotation did not run; the fixture is below the threshold"
    assert observed[0] > attempt_log._MAX_LINES, (
        f"the log was already truncated before the swap: {observed[0]} lines"
    )
    assert log.stat().st_ino != before_inode, "no atomic swap happened"
    assert len(log.read_text().splitlines()) == attempt_log._KEEP_LINES
    assert not list(tmp_path.glob("*.rot*")), "temp file left behind"
