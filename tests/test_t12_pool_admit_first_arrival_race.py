"""`Pool.admit` must admit one canonical row per prompt — T-12.

The H-07 fix locked ONLY the duplicate-increment branch. `find_duplicate` still
ran outside the lock, and so did both append branches — so N threads admitting
the SAME NEW prompt each saw no duplicate, each fell through, and each appended
a canonical row. Measured during the audit: 20 threads, one prompt, **2
canonical rows where 1 was correct**.

Locking only the increment fixes the second arrival and leaves the first wide
open, which is the subtler half. A duplicate miscount is a wrong number; two
canonical rows are two different tasks claiming to be the same one, and
everything downstream — propose, mutation validation, human approval — then
happens twice for one piece of work.
"""

from __future__ import annotations

import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth import pool as poolmod   # noqa: E402

PROMPT = "What is the capital of Portugal? Answer with just the city name."
THREADS = 20


def _candidate(n: int, prompt: str = PROMPT, *, eligible: bool = True):
    """A candidate shaped exactly as `pool.make_candidate` builds one.

    `content_sha` and `envelope["prompt"]` are LOAD-BEARING and were missing
    from the first version of this fixture: `find_duplicate` matches exactly on
    `content_sha` and near-matches on `_dedup_sets`, which `load()` rebuilds
    from `envelope["prompt"]`. Without them a freshly constructed Pool cannot
    dedup at all, and this file reported a race that was really a broken
    fixture — worth stating, because a concurrency test that fails for the
    wrong reason is indistinguishable from one that found something.
    """
    return poolmod.Candidate(
        task_id=f"gtc-race-{n:04d}",
        prompt_sha256=f"sha{n:04d}",
        task_type="query",
        complexity="simple",
        eligibility={
            "ground_truth_candidate": eligible,
            "ineligibility_reasons": [] if eligible else ["no-reliable-verifier"],
        },
        envelope={"prompt": prompt},
        content_sha=poolmod.exact_key(prompt),
    )


def _admit_concurrently(path, prompt, n=THREADS):
    """Each thread gets its OWN Pool, as `accumulate.py` does in production."""
    barrier = threading.Barrier(n)
    results: list[tuple[bool, str]] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        p = poolmod.Pool(path)
        barrier.wait()          # maximise the overlap
        out = p.admit(_candidate(i, prompt), prompt)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _canonical_rows(path):
    """Distinct task_ids that reached a non-rejected state."""
    p = poolmod.Pool(path)
    p.load()
    return [c for c in p.all() if c.state not in poolmod.TERMINAL_FAILURES]


# ── the race ─────────────────────────────────────────────────────────────────

def test_twenty_threads_one_new_prompt_produce_one_canonical_row(tmp_path):
    """The gate, exactly as the audit measured it."""
    path = tmp_path / "pool.jsonl"
    results = _admit_concurrently(path, PROMPT)

    admitted = [r for r in results if r[0]]
    rows = _canonical_rows(path)

    assert len(admitted) == 1, (
        f"{len(admitted)} of {THREADS} threads were told they admitted a new "
        f"candidate for the same prompt"
    )
    assert len(rows) == 1, (
        f"{len(rows)} canonical rows for one prompt: "
        f"{[c.task_id for c in rows]}"
    )


def test_the_duplicates_are_counted_not_lost(tmp_path):
    """H-07's half must keep working: every arrival is counted exactly once.

    `duplicate_count` counts OCCURRENCES, not duplicates — a fresh candidate
    starts at 1 for its own arrival — so 20 threads must leave 20, not 19. The
    H-07 defect showed up here as a count BELOW the arrival count (measured
    then: 19 recorded where 21 occurred).
    """
    path = tmp_path / "pool.jsonl"
    _admit_concurrently(path, PROMPT)

    rows = _canonical_rows(path)
    assert len(rows) == 1
    assert rows[0].duplicate_count == THREADS, (
        f"{rows[0].duplicate_count} occurrences recorded for {THREADS} arrivals — "
        "increments were lost under concurrency (the H-07 defect)"
    )


# ── anti-vacuity ─────────────────────────────────────────────────────────────

def test_genuinely_different_prompts_still_each_get_a_row(tmp_path):
    """A lock that serialises everything into one row is not a fix.

    Without this, `admit` could satisfy every test above by rejecting all but
    the first candidate regardless of content.
    """
    path = tmp_path / "pool.jsonl"
    prompts = [
        "What is the capital of Portugal?",
        "Implement a Redis-backed rate limiter with a sliding window.",
        "Explain why my asyncio gather call deadlocks on a shared semaphore.",
    ]
    p = poolmod.Pool(path)
    for i, prompt in enumerate(prompts):
        admitted, reason = p.admit(_candidate(100 + i, prompt), prompt)
        assert admitted, f"distinct prompt {i} was rejected: {reason}"

    assert len(_canonical_rows(path)) == len(prompts)


def test_sequential_admits_are_unchanged(tmp_path):
    """The restructure must not change single-threaded behaviour."""
    path = tmp_path / "pool.jsonl"
    p = poolmod.Pool(path)
    first, _ = p.admit(_candidate(1), PROMPT)
    second, reason = p.admit(_candidate(2), PROMPT)
    assert first is True
    assert second is False and reason

    rows = _canonical_rows(path)
    assert len(rows) == 1
    assert rows[0].duplicate_count == 2, "two arrivals, two occurrences"


def test_an_ineligible_candidate_is_still_rejected(tmp_path):
    """The eligibility branch moved inside the lock; it must still fire."""
    path = tmp_path / "pool.jsonl"
    other = "some other prompt entirely, quite different from the first"
    cand = _candidate(9, other, eligible=False)
    admitted, reason = poolmod.Pool(path).admit(cand, other)
    assert admitted is False
    assert reason == "no-reliable-verifier"


# ── the call site ────────────────────────────────────────────────────────────

def test_the_duplicate_search_happens_inside_the_lock():
    """Rule B. The defect was WHERE the lock was, not whether one existed."""
    import inspect

    src = inspect.getsource(poolmod.Pool.admit)
    lock_at = src.index("with self._lock():")
    find_at = src.index("self.find_duplicate(")
    append_at = src.rindex("self._append(candidate)")
    assert lock_at < find_at, "find_duplicate still runs before the lock is taken"
    assert lock_at < append_at, "the canonical append still runs outside the lock"
