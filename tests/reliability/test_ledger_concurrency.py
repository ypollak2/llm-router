"""RED5-01/02/03 (P0) — the ledger must not lose events under concurrency.

Multiprocessing, not threads, and that is not a stylistic preference. The
defects are about *file* locks and the SQLite header: a thread-based test shares
one process's connections and locks and cannot reproduce either. The audit's
measurements came from real processes — 66 dropped events across 2400 concurrent
writes, and 4 of 12 `LineageStore` constructions raising on a cold start — so
the regression test has to run in the same shape or it proves nothing.

⚠ SAFETY. Every test asserts its resolved paths are inside tmp_path BEFORE
writing. During the audit a test that believed `LLM_ROUTER_HOME` sandboxed it wrote
to the operator's real `~/.llm-router/usage.db` and destroyed live data. The
`isolated_home` fixture below asserts the isolation took effect rather than
assuming it, which is the specific lesson from that incident.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(180)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect llm_router state into tmp_path and PROVE it landed there."""
    from llm_router import paths

    home = tmp_path / "llm_router-home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))

    assert paths.is_isolated(), "LLM_ROUTER_HOME did not take effect"
    resolved = paths.state_path("usage.db")
    assert str(resolved).startswith(str(tmp_path)), (
        f"state path escaped the tmpdir: {resolved} — refusing to run a "
        f"destructive concurrency test against real data"
    )
    return home


# ── workers (module level: must be picklable for spawn) ──────────────────────


def _write_events(args) -> int:
    """Append N events to the ledger. Returns how many the ledger accepted."""
    db_path, n, worker = args
    from llm_router.execution_ledger import LedgerEvent, record_event

    accepted = 0
    for i in range(n):
        ok = record_event(
            LedgerEvent(
                event_id=f"w{worker}-e{i}",
                session_id=f"s{worker}",
                route_id=f"r{worker}-{i}",
                event_type="route_started",
            ),
            path=Path(db_path),
        )
        accepted += 1 if ok else 0
    return accepted


def _construct_lineage_store(db_path) -> str:
    """Cold-start a LineageStore. Returns "ok" or the exception text."""
    from llm_router.lineage.lineage_store import LineageStore

    try:
        LineageStore(db_path=Path(db_path))
        return "ok"
    except Exception as exc:  # noqa: BLE001 — the failure IS the measurement
        return f"{type(exc).__name__}: {exc}"


# ── the measurements the audit took ──────────────────────────────────────────


def test_no_events_are_dropped_under_concurrent_cold_start(isolated_home):
    """Baseline: 66 dropped across 2400 writes, peak 29.4% in one worker.

    Cold start is the hard case — the database does not exist when the workers
    launch, so they race to create it AND to take the exclusive lock the WAL
    switch needs.
    """
    db_path = isolated_home / "ledger.db"
    assert not db_path.exists(), "this must be a COLD start to reproduce RED5-01"

    workers, per_worker = 12, 200
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers) as pool:
        accepted = pool.map(
            _write_events, [(str(db_path), per_worker, w) for w in range(workers)]
        )

    expected = workers * per_worker
    assert sum(accepted) == expected, (
        f"the ledger reported {expected - sum(accepted)} rejected writes"
    )

    conn = sqlite3.connect(str(db_path))
    try:
        persisted = conn.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0]
    finally:
        conn.close()

    assert persisted == expected, (
        f"{expected - persisted} events were reported as written but are not in "
        f"the database — the exact silent-loss shape of RED5-02"
    )


def test_lineage_store_survives_a_concurrent_cold_start(isolated_home):
    """Baseline: 4 of 12 constructions raised `database is locked`."""
    db_path = isolated_home / "lineage.db"
    assert not db_path.exists()

    ctx = mp.get_context("spawn")
    with ctx.Pool(12) as pool:
        results = pool.map(_construct_lineage_store, [str(db_path)] * 12)

    failures = [r for r in results if r != "ok"]
    assert failures == [], f"{len(failures)}/12 cold-start constructions raised: {failures}"


# ── the signal nobody was reading ────────────────────────────────────────────


def test_a_failed_write_is_counted_not_silent(isolated_home, monkeypatch):
    """A forced failure must raise or increment a visible counter — never both quiet.

    `record_event` is fail-open on purpose: a ledger problem must not break
    routing. Fail-open is only defensible when the failure is counted, and it
    was not — the boolean was returned to seven call sites that all discarded
    it, so the loss had no error, no log and no metric.
    """
    import llm_router.execution_ledger as el

    el.reset_dropped_event_count()
    assert el.dropped_event_count() == 0

    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(el, "_connect", _boom)

    ok = el.record_event(el.LedgerEvent(event_id="x", event_type="route_started"))

    assert ok is False
    assert el.dropped_event_count() == 1, "a dropped event left no trace"


def test_no_record_event_call_site_discards_the_boolean():
    """The habit, not just the instance. Enforced structurally.

    Every call site binding the result is what stops the next one from being
    written the old way; the counter inside record_event is the backstop, not
    the fix.

    Scoped to the LEDGER's ``record_event`` — the one returning bool.
    ``session_store.record_event`` is a different function that returns None and
    reports failure through its own counter; demanding a binding there would be
    cargo-culting the shape instead of the property.
    """
    src = Path(__file__).resolve().parents[2] / "src"
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        if path.name == "execution_ledger.py":
            continue  # the definition itself
        text = path.read_text(encoding="utf-8")
        if "from llm_router.execution_ledger import" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("record_event("):
                offenders.append(f"{path.relative_to(src)}:{lineno}")
    assert offenders == [], (
        "ledger record_event() result discarded at: " + ", ".join(offenders)
    )


def test_lock_timeout_declines_rather_than_running_unlocked(tmp_path, monkeypatch):
    """RED5-03: `with exclusive_lock(...)` discarded the yielded boolean.

    exclusive_lock degrades to "unlocked" on timeout rather than raising — a
    reasonable default for best-effort callers, and the wrong one for a
    hash-chain read-modify-write. Proceeding unlocked is what produced
    `broken_chain_at_*` for writers doing nothing wrong.
    """
    import contextlib

    import llm_router.session_store as ss

    @contextlib.contextmanager
    def _always_times_out(_lock_path, timeout=30.0):
        yield False

    monkeypatch.setattr(ss, "exclusive_lock", _always_times_out)
    before = ss.lock_timeout_count()

    ss.record_event("s1", "prompt", "hello", role="user")

    assert ss.lock_timeout_count() > before, (
        "a lock timeout was absorbed silently; the write either happened "
        "unlocked or vanished, and neither was recorded"
    )


def test_concurrent_legitimate_writers_do_not_break_the_chain(isolated_home):
    """A broken chain must mean tampering, not contention.

    If honest concurrent writers can produce `broken_chain_at_*`, the integrity
    signal is noise and stops being read — which is worse than not having it.
    """
    db_path = isolated_home / "chain.db"
    ctx = mp.get_context("spawn")
    with ctx.Pool(8) as pool:
        pool.map(_write_events, [(str(db_path), 50, w) for w in range(8)])

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT event_id FROM execution_events").fetchall()
    finally:
        conn.close()

    broken = [r[0] for r in rows if str(r[0]).startswith("broken_chain_at_")]
    assert broken == [], f"contention forged tamper evidence: {broken}"
    assert len(rows) == 8 * 50


def test_the_db_path_honours_llm_router_home(isolated_home):
    """RED2-07 directly: the gap that destroyed real data during the audit."""
    from llm_router.config import RouterConfig

    resolved = RouterConfig().llm_router_db_path
    assert str(resolved).startswith(str(isolated_home)), (
        f"LLM_ROUTER_HOME did not redirect the usage DB (got {resolved}). This is "
        f"the exact failure that wrote to a live database during the audit."
    )
    assert os.environ["LLM_ROUTER_HOME"] in str(resolved)
