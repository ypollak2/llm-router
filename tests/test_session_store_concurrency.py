"""Real multi-process concurrency regression test for CHZ-AUD-C-01.

Root cause: ``session_store.record_event()``'s append-then-maybe-compact
critical section had no cross-process coordination. A concurrent process's
compaction could read a stale snapshot of the JSONL file (taken before this
process's append landed) and then ``os.replace()`` the file with that stale
snapshot, silently orphaning this process's just-written record even though
the write itself succeeded on disk moments earlier.

Methodology (ported from the original audit's ad hoc reproduction,
``Docs/audit/C-packaging-concurrency-soak.md``): N real OS processes each call
``record_event()`` repeatedly against the *same* session_id, with a unique
marker per write. Immediately after each write, the writing process re-scans
the file's raw bytes for its own marker. This is deliberately NOT "does the
final file contain all N*iterations records" — production compaction
(``_MAX_RECORDS`` / ``_COMPACT_TO``) is *designed* to prune older records
once the log grows past its cap, and that pruning is not data loss. What
must never happen is a record's immediate self-readback (right after its
own successful write, well before compaction could plausibly cycle through
150+ newer records) coming up empty.

Uses real ``multiprocessing`` processes (fork on POSIX, spawn as the only
option on Windows) — concurrency is NOT mocked or simulated with threads,
per the hardening task's hard constraints.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

# GH#84: the project-wide pytest-timeout default (`timeout = 30` in
# pyproject.toml, mirrored by CI's `--timeout=30`) is sized for ordinary unit
# tests, not this one. Six real forked processes doing 200 lock-serialized
# writes apiece across 3 rounds routinely take single-digit seconds when the
# host is idle, but pytest's own per-test SETUP already includes every
# autouse fixture, and both setup and this test's own `_run_round` slow down
# sharply under CPU pressure from whatever else happens to be running at the
# time — which is exactly what pytest-randomly's shuffled ordering changes
# from run to run. Confirmed by instrumentation: saturating the host with
# background CPU load reproduces
# `Failed: Timeout (>30.0s) from pytest-timeout` deterministically, with
# `_run_round`'s own `p.join(timeout=JOIN_TIMEOUT_SECONDS)` still in flight —
# i.e. pytest-timeout's watchdog fires and unwinds the test WHILE worker
# processes are still writing, orphaning them and (via the enclosing
# `tempfile.TemporaryDirectory` in the caller) deleting the directory out
# from under them. That is indistinguishable from "writes were lost" and is
# the actual GH#84 order-dependent flake — not corrupted/leaked
# session_store state (instrumented and ruled out: no module-level cache or
# singleton in session_store.py/paths.py depends on call order, and repeated
# runs under heavy synthetic thread/CPU load with real accumulated
# background state from a full suite pass never produced a single genuinely
# lost marker once the timeout was removed from the equation).
#
# `tests/reliability/test_ledger_concurrency.py` — the other real-multiprocess
# regression test in this suite — already carries the identical
# `pytest.mark.timeout(180)` override for the same reason; this mirrors that
# established precedent rather than inventing a parallel mechanism.
pytestmark = pytest.mark.timeout(180)

N_WORKERS = 6
ITERATIONS_PER_WORKER = 200
N_ROUNDS = 3
JOIN_TIMEOUT_SECONDS = 120


def _mp_context():
    """Prefer fork (fast process startup) where available; spawn elsewhere."""
    try:
        return multiprocessing.get_context("fork")
    except ValueError:
        return multiprocessing.get_context("spawn")


def _worker(
    home: str,
    project_id: str,
    session_id: str,
    worker_id: int,
    iterations: int,
    result_path: str,
) -> None:
    """Write *iterations* uniquely-marked events, self-verifying each one.

    Runs in a separate OS process. Sets HOME/LLM_ROUTER_PROJECT_ID explicitly
    (rather than relying on inheritance) so the test is correct under both
    fork and spawn start methods.
    """
    os.environ["HOME"] = home
    os.environ["LLM_ROUTER_PROJECT_ID"] = project_id
    os.environ.pop("LLM_ROUTER_SESSION_CONTEXT", None)
    os.environ.pop("CLAUDE_SESSION_ID", None)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    from llm_router import session_store as ss

    lost: list[str] = []
    path = ss._session_path(session_id)
    for i in range(iterations):
        marker = f"MARK-w{worker_id}-i{i}-pid{os.getpid()}"
        ss.record_event(session_id, "user_prompt", f"payload {marker} payload")
        try:
            raw = path.read_bytes()
        except OSError:
            raw = b""
        if marker.encode("utf-8") not in raw:
            lost.append(marker)

    Path(result_path).write_text(
        json.dumps({"worker_id": worker_id, "lost": lost, "wrote": iterations}),
        encoding="utf-8",
    )


def _run_round(tmp_path: Path, round_idx: int) -> list[str]:
    home = str(tmp_path)
    project_id = "concurrency-test-project"
    session_id = f"concurrency-round-{round_idx}"
    ctx = _mp_context()

    result_paths = [
        str(tmp_path / f"result_r{round_idx}_w{i}.json") for i in range(N_WORKERS)
    ]
    procs = []
    for i in range(N_WORKERS):
        p = ctx.Process(
            target=_worker,
            args=(home, project_id, session_id, i, ITERATIONS_PER_WORKER, result_paths[i]),
        )
        p.start()
        procs.append(p)

    for p in procs:
        p.join(timeout=JOIN_TIMEOUT_SECONDS)
        assert not p.is_alive(), "worker process hung past join timeout"
        assert p.exitcode == 0, f"worker process exited with code {p.exitcode}"

    all_lost: list[str] = []
    total_written = 0
    for rp in result_paths:
        data = json.loads(Path(rp).read_text(encoding="utf-8"))
        all_lost.extend(data["lost"])
        total_written += data["wrote"]
    assert total_written == N_WORKERS * ITERATIONS_PER_WORKER
    return all_lost


def test_session_store_concurrency_zero_lost_writes():
    """>=6 processes x 200 events x 3 rounds against the same session_id.

    Each round exercises production compaction thresholds
    (``_MAX_RECORDS=300`` / ``_COMPACT_TO=150`` are left at their real
    defaults, guaranteeing multiple organic compaction cycles mid-run).
    Asserts zero lost writes in every round (repeated for confidence, per
    the hardening task's mandatory-test spec).
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="llm_router-c01-") as tmp:
        tmp_path = Path(tmp)
        for round_idx in range(N_ROUNDS):
            lost = _run_round(tmp_path, round_idx)
            assert lost == [], (
                f"round {round_idx}: {len(lost)} writes were lost "
                f"(first few: {lost[:10]})"
            )
