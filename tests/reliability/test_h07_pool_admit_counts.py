"""H-07 — `Pool.admit` lost duplicate counts under concurrency.

Measured by the 2026-09-21 audit: **19 recorded where 21 occurred**. Reproduced
here under seven threads:

    pre-fix    recorded   9  expected  22   (13 lost)
    post-fix   recorded  22  expected  22

The mechanism is a read-modify-append with nothing serialising it.
`duplicate_count += 1` mutates a `Candidate` loaded into memory when the `Pool`
was constructed, appends the whole row, and `load()` takes last-wins per
`task_id`. Two admits of the same duplicate both read 1, both write 2, and one
increment is gone.

It matters because `accumulate.py` constructs a fresh `Pool()` on every call, so
the production path hits this on every admit rather than only in an unusual
interleaving.

The fix is `file_lock.exclusive_lock`, which already existed in this repository —
written for `session_store.record_event`, whose append-then-compact critical
section lost 22 of 1200 writes under six-process load. Same shape, same
primitive, never adopted here. Two details carried across: the lock is on a
SIBLING file (so the JSONL inode stays swappable), and the state is **re-read
inside the lock**, because locking around a stale in-memory copy would serialise
the writes while still losing the count.
"""

from __future__ import annotations

import hashlib
import pathlib
import threading

import pytest

PROMPT = "how do I list files in a directory"
SHA = hashlib.sha256(PROMPT.encode()).hexdigest()


def _pool_module():
    import importlib.util
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "scripts" / "groundtruth" / "pool.py"
    name = "_gt_pool_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def P(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    return _pool_module()


def _candidate(P, tid):
    return P.Candidate(
        task_id=tid,
        prompt_sha256=SHA,
        envelope={"prompt": PROMPT},
        eligibility={"ground_truth_candidate": True},
    )


def _unserialised_admit(self, candidate, prompt, *, threshold=0.9):
    """The pre-fix body verbatim: no lock, no re-read."""
    dup_id, reason = self.find_duplicate(prompt, threshold=threshold)
    if dup_id:
        existing = self._index[dup_id]
        existing.duplicate_count += 1
        self._append(existing)
        self._reject(reason, {"task_id": candidate.task_id, "duplicate_of": dup_id})
        return False, reason
    return True, "admitted"


def _hammer(P, tmp_path, admit_fn, threads=7, each=3) -> tuple[int, int]:
    path = tmp_path / "pool.jsonl"
    funnel = tmp_path / "funnel.jsonl"
    P.Pool(path=path, funnel=funnel).admit(_candidate(P, "seed"), PROMPT)

    def worker(i):
        for j in range(each):
            pool = P.Pool(path=path, funnel=funnel)   # fresh, as accumulate.py does
            admit_fn(pool, _candidate(P, f"t{i}_{j}"), PROMPT)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    final = P.Pool(path=path, funnel=funnel)
    return final._index["seed"].duplicate_count, 1 + threads * each


def test_concurrent_duplicate_admits_lose_nothing(P, tmp_path):
    got, expected = _hammer(P, tmp_path, P.Pool.admit)
    assert got == expected, f"{expected - got} duplicate increments lost ({got}/{expected})"


def test_the_unserialised_version_really_does_lose(P, tmp_path):
    """Anti-vacuity, and the reason the lock is not cargo cult.

    If the old body could not be made to lose on this machine, the test above
    would pass with or without the fix and prove nothing.
    """
    got, expected = _hammer(P, tmp_path, _unserialised_admit)
    assert got < expected, (
        f"the unserialised admit recorded {got}/{expected} with no loss, so the "
        f"serialised test cannot demonstrate an improvement"
    )


def test_a_single_threaded_admit_still_counts_normally(P, tmp_path):
    """Anti-over-correction: the lock must not suppress legitimate increments."""
    path, funnel = tmp_path / "p.jsonl", tmp_path / "f.jsonl"
    P.Pool(path=path, funnel=funnel).admit(_candidate(P, "seed"), PROMPT)
    for i in range(4):
        P.Pool(path=path, funnel=funnel).admit(_candidate(P, f"d{i}"), PROMPT)
    assert P.Pool(path=path, funnel=funnel)._index["seed"].duplicate_count == 5


def test_lock_failure_degrades_rather_than_blocking(P, tmp_path, monkeypatch):
    """Accumulation must survive a locking problem, per the primitive's contract.

    `exclusive_lock` never raises for acquisition failures; `_lock` must not
    reintroduce a hard failure on top of it.
    """
    import llm_router.file_lock as fl

    def _boom(*a, **k):
        raise OSError("no locking on this filesystem")

    monkeypatch.setattr(fl, "exclusive_lock", _boom)

    path, funnel = tmp_path / "p.jsonl", tmp_path / "f.jsonl"
    pool = P.Pool(path=path, funnel=funnel)
    admitted, _ = pool.admit(_candidate(P, "seed"), PROMPT)
    assert admitted, "a locking failure prevented admission entirely"


def test_the_fix_uses_the_existing_primitive(P):
    """Pin the mechanism. A hand-rolled lock here would drift from session_store's."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "scripts" / "groundtruth" / "pool.py").read_text(encoding="utf-8")
    assert "exclusive_lock" in src, "pool.py no longer uses file_lock.exclusive_lock"
    body = src.split("def admit(")[1].split("\n    def ")[0]
    assert "self.load()" in body, (
        "admit() locks but does not re-read; incrementing a stale in-memory copy "
        "loses the count whether or not the write is serialised"
    )
