"""T2-XL1: multi-instance budget coordination via Postgres.

Two test tiers:

1. **Unit tests** (always run) — exercise :class:`PostgresBudgetBackend`'s
   logic with a fake psycopg connection. These pin the SQL shape and the
   single-process logic without needing Docker.
2. **Integration test** (gated by ``has_docker()``) — spins up a real
   Postgres via Testcontainers and asserts the G-002 cross-instance
   acceptance: 100 concurrent ``try_reserve`` calls spread across 4
   processes against a $5 cap, $0.10 each → **exactly 50 succeed total**.

The integration test is skipped automatically when Docker is unavailable
(local dev without Docker Desktop running, or a CI runner without the
docker socket). The unit tests stay green either way so the SQL shape
regressions surface immediately.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-002 (cross-instance).
"""
from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import socket
from contextlib import suppress
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from llm_router.budget_key import SCOPE_TURN, BudgetKey

# CI may not install the optional `postgres` / `postgres-test` extras. Skip
# the whole module gracefully when psycopg is unavailable rather than
# failing on the first import that exercises PostgresBudgetBackend.__init__.
psycopg = pytest.importorskip("psycopg")


# ── Docker / Testcontainers availability ──────────────────────────────────


def _docker_socket_present() -> bool:
    """Detect a usable Docker daemon by probing the conventional socket
    paths. Cheaper than spawning a `docker version` subprocess and
    enough signal to decide whether to skip integration tests."""
    candidates = [
        Path.home() / ".docker" / "run" / "docker.sock",
        Path("/var/run/docker.sock"),
    ]
    for path in candidates:
        if path.exists():
            try:
                with socket.socket(socket.AF_UNIX) as s:
                    s.settimeout(0.2)
                    s.connect(str(path))
                    return True
            except (OSError, socket.error):
                continue
    return False


def _has_docker_binary() -> bool:
    return shutil.which("docker") is not None


def _has_testcontainers() -> bool:
    """The integration test needs both the `testcontainers` package and a
    reachable Docker daemon. Either missing → skip cleanly."""
    try:
        import testcontainers.postgres  # noqa: F401
    except ImportError:
        return False
    return True


_DOCKER_AVAILABLE = (
    _has_docker_binary() and _docker_socket_present() and _has_testcontainers()
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _k(user: str = "alice") -> BudgetKey:
    return BudgetKey(
        tenant_id="t1", org_id="o1", user_id=user, agent_id=None, scope=SCOPE_TURN
    )


class _FakeCursor:
    """Just enough psycopg.Cursor surface to verify SQL + rowcount semantics."""

    def __init__(self, store: "_FakeStore") -> None:
        self._store = store
        self.rowcount = -1
        self._result: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._store.executed.append((sql, params))
        self._result = []
        self.rowcount = -1
        sql_norm = " ".join(sql.split())
        if sql_norm.startswith("CREATE"):
            return
        if "INSERT INTO llm_router_envelopes" in sql_norm:
            key, cap, soft, parents = params
            self._store.rows[key] = {
                "cap_usd": cap,
                "soft_cap_usd": soft,
                "parents_json": parents,
                "consumed_usd": 0.0,
                "pending_usd": 0.0,
                "soft_breached": False,
            }
            self.rowcount = 1
            return
        if "FROM llm_router_envelopes WHERE key_blob =" in sql_norm and "SELECT cap_usd" in sql_norm:
            key = params[0]
            row = self._store.rows.get(key)
            self._result = [(
                row["cap_usd"],
                row["soft_cap_usd"],
                row["parents_json"],
                row["consumed_usd"],
                row["pending_usd"],
                row["soft_breached"],
            )] if row else []
            return
        if "SELECT parents_json FROM llm_router_envelopes" in sql_norm and "FOR UPDATE" in sql_norm:
            key = params[0]
            row = self._store.rows.get(key)
            self._result = [(row["parents_json"],)] if row else []
            return
        if "SELECT 1 FROM llm_router_envelopes" in sql_norm and "FOR UPDATE" in sql_norm:
            key = params[0]
            self._result = [(1,)] if key in self._store.rows else []
            return
        if "UPDATE llm_router_envelopes SET pending_usd = pending_usd +" in sql_norm and "<= cap_usd" in sql_norm:
            cost, key, cost2 = params
            row = self._store.rows.get(key)
            if row and row["consumed_usd"] + row["pending_usd"] + cost <= row["cap_usd"]:
                row["pending_usd"] += cost
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if "SET pending_usd = GREATEST(0.0, pending_usd -" in sql_norm:
            cost, key = params
            row = self._store.rows.get(key)
            if row:
                row["pending_usd"] = max(0.0, row["pending_usd"] - cost)
                self.rowcount = 1
            return
        if "SET consumed_usd = consumed_usd +" in sql_norm:
            cost, cost2, key = params
            row = self._store.rows.get(key)
            if row:
                row["consumed_usd"] += cost
                row["pending_usd"] = max(0.0, row["pending_usd"] - cost)
                self.rowcount = 1
            return
        if "UPDATE llm_router_envelopes SET soft_breached =" in sql_norm:
            flag, key = params
            row = self._store.rows.get(key)
            if row:
                row["soft_breached"] = flag
                self.rowcount = 1
            return

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.executed: list[tuple[str, tuple]] = []


class _FakeConn:
    def __init__(self) -> None:
        self._store = _FakeStore()
        self.committed = 0
        self.rolled_back = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._store)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        pass


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch):
    """PostgresBudgetBackend talking to an in-memory fake connection."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_POSTGRES_DSN", "postgresql://fake")
    from llm_router import budget_backend_postgres as mod

    fake_conn = _FakeConn()
    monkeypatch.setattr(mod, "_SCHEMA", "")
    mock_psycopg = MagicMock()
    mock_psycopg.connect.return_value = fake_conn
    monkeypatch.setitem(__import__("sys").modules, "psycopg", mock_psycopg)
    backend = mod.PostgresBudgetBackend()
    yield backend, fake_conn
    backend.close()


# ── 1. Unit tests — single-process logic via fake psycopg ─────────────────


def test_init_requires_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_ROUTER_BUDGET_POSTGRES_DSN", raising=False)
    from llm_router.budget_backend_postgres import PostgresBudgetBackend
    with pytest.raises(RuntimeError, match="DSN"):
        PostgresBudgetBackend()


def test_register_persists_envelope(fake_backend) -> None:
    backend, conn = fake_backend
    key = _k()
    env = backend.register(key, cap_usd=1.0)
    assert env.cap_usd == pytest.approx(1.0)
    assert backend.get(key) is not None
    assert backend.consumed(key) == pytest.approx(0.0)
    assert backend.remaining(key) == pytest.approx(1.0)


def test_register_rejects_non_positive_cap(fake_backend) -> None:
    backend, _ = fake_backend
    with pytest.raises(ValueError, match="cap_usd must be positive"):
        backend.register(_k(), cap_usd=0.0)


def test_register_validates_soft_cap(fake_backend) -> None:
    backend, _ = fake_backend
    with pytest.raises(ValueError, match="strictly less"):
        backend.register(_k(), cap_usd=1.0, soft_cap_usd=1.0)


@pytest.mark.asyncio
async def test_try_reserve_succeeds_under_cap(fake_backend) -> None:
    backend, _ = fake_backend
    key = _k()
    backend.register(key, cap_usd=1.0)
    assert await backend.try_reserve(key, 0.4) is True
    assert backend.pending(key) == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_try_reserve_refuses_over_cap(fake_backend) -> None:
    backend, conn = fake_backend
    key = _k()
    backend.register(key, cap_usd=1.0)
    assert await backend.try_reserve(key, 0.6) is True
    assert await backend.try_reserve(key, 0.6) is False
    assert backend.pending(key) == pytest.approx(0.6)
    assert conn.rolled_back >= 1


@pytest.mark.asyncio
async def test_commit_moves_pending_to_consumed(fake_backend) -> None:
    backend, _ = fake_backend
    key = _k()
    backend.register(key, cap_usd=1.0)
    assert await backend.try_reserve(key, 0.4) is True
    await backend.commit(key, 0.4)
    assert backend.consumed(key) == pytest.approx(0.4)
    assert backend.pending(key) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_release_reverts_reservation(fake_backend) -> None:
    backend, _ = fake_backend
    key = _k()
    backend.register(key, cap_usd=1.0)
    assert await backend.try_reserve(key, 0.4) is True
    await backend.release(key, 0.4)
    assert backend.pending(key) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_unregistered_key_is_unenforced(fake_backend) -> None:
    backend, _ = fake_backend
    assert await backend.try_reserve(_k(user="ghost"), 999.0) is True
    assert backend.remaining(_k(user="ghost")) == float("inf")


def test_sql_contains_cap_check_clause(fake_backend) -> None:
    """The atomic check-then-charge contract: the WHERE clause itself
    enforces ``consumed + pending + cost <= cap`` — no application-side
    pre-read. If a refactor ever splits this into SELECT-then-UPDATE,
    multi-process atomicity is lost. Pin the SQL shape."""
    backend, conn = fake_backend
    key = _k()
    backend.register(key, cap_usd=1.0)
    import asyncio as _aio
    _aio.run(backend.try_reserve(key, 0.4))
    statements = [stmt for stmt, _ in conn._store.executed]
    update_stmt = next(
        s for s in statements if "UPDATE llm_router_envelopes" in s and "pending_usd + " in s
    )
    norm = " ".join(update_stmt.split())
    assert "consumed_usd + pending_usd +" in norm
    assert "<= cap_usd" in norm


# ── 2. Factory — postgres recognised, falls back when missing dep/DSN ─────


def test_factory_recognises_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting LLM_ROUTER_BUDGET_BACKEND=postgres without a DSN must
    fail-open back to SQLite — never break boot."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_BACKEND", "postgres")
    monkeypatch.delenv("LLM_ROUTER_BUDGET_POSTGRES_DSN", raising=False)
    from llm_router.budget_backend import (
        SqliteBudgetBackend,
        get_budget_backend,
        reset_budget_backend_for_tests,
    )
    reset_budget_backend_for_tests()
    backend = get_budget_backend()
    assert isinstance(backend, SqliteBudgetBackend)
    reset_budget_backend_for_tests()


# ── 3. Integration test — multi-process G-002 acceptance ──────────────────


def _attempt_in_subprocess(args: tuple[str, str, float, int]) -> int:
    """Run ``count`` try_reserve calls in a fresh process. Returns
    the number that succeeded. Module-level entry-point so spawn-mode
    child processes can import it."""
    dsn, key_user, cost, count = args
    import asyncio as _aio
    import os as _os

    _os.environ["LLM_ROUTER_BUDGET_POSTGRES_DSN"] = dsn
    from llm_router.budget_backend_postgres import PostgresBudgetBackend
    from llm_router.budget_key import SCOPE_TURN as _SCOPE, BudgetKey as _BK

    backend = PostgresBudgetBackend()
    key = _BK(
        tenant_id="t1",
        org_id="o1",
        user_id=key_user,
        agent_id=None,
        scope=_SCOPE,
    )

    async def _run() -> int:
        results = await _aio.gather(
            *(backend.try_reserve(key, cost) for _ in range(count))
        )
        return sum(1 for r in results if r)

    succeeded = _aio.run(_run())
    backend.close()
    return succeeded


@pytest.mark.skipif(
    not _DOCKER_AVAILABLE,
    reason="Docker daemon not reachable — Postgres integration test skipped",
)
def test_tst003_multi_process_concurrency_acceptance() -> None:
    """**G-002 multi-instance acceptance** (TST-003 variant):

    100 concurrent ``try_reserve`` calls spread across 4 processes
    against a $5 cap, $0.10 each → **exactly 50 succeed total**.

    The single-process SQLite acceptance from T2-L1 used the same shape
    in one process; this is the cross-process generalisation that
    Phase 3b deployments need.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url(driver=None)
        if dsn.startswith("postgresql+psycopg2://"):
            dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)

        os.environ["LLM_ROUTER_BUDGET_POSTGRES_DSN"] = dsn

        from llm_router.budget_backend_postgres import PostgresBudgetBackend
        bootstrap = PostgresBudgetBackend()
        key = _k(user="multi-proc-acceptance")
        bootstrap.register(key, cap_usd=5.0)
        bootstrap.close()

        per_process = 25
        process_count = 4
        with mp.get_context("spawn").Pool(process_count) as pool:
            wins = pool.map(
                _attempt_in_subprocess,
                [
                    (dsn, "multi-proc-acceptance", 0.10, per_process)
                    for _ in range(process_count)
                ],
            )
        total = sum(wins)
        assert total == 50, (
            f"Expected exactly 50 successful reservations across "
            f"{process_count} processes; got {total} (per-process: {wins})"
        )

        verify = PostgresBudgetBackend()
        assert verify.pending(key) == pytest.approx(5.0)
        verify.close()
        with suppress(KeyError):
            del os.environ["LLM_ROUTER_BUDGET_POSTGRES_DSN"]


@pytest.mark.skipif(
    not _DOCKER_AVAILABLE,
    reason="Docker daemon not reachable — Postgres integration test skipped",
)
def test_tst003_stress_extreme_oversubscription() -> None:
    """**G-002 stress variant — beyond the minimum acceptance.**

    The minimum acceptance (:func:`test_tst003_multi_process_concurrency_acceptance`)
    races 100 attempts / 4 processes against a cap that admits 50 — a 2×
    oversubscription. This variant turns the contention up hard: **160
    concurrent ``try_reserve`` calls spread across 8 processes fight over a
    cap that admits only 3** (~53× oversubscription, every racer targeting
    the *same* key). It exists to catch a lost-update / double-spend bug
    that the gentle 2× case can miss — the exactly-N invariant must hold no
    matter how many racers pile onto the atomic check-then-charge.

    Asserts (a) **exactly 3** reservations win across all processes (no
    overshoot, no starvation), and (b) the persisted ``pending`` equals
    exactly ``3 × cost`` — i.e. not one cent was double-charged under the
    stampede.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url(driver=None)
        if dsn.startswith("postgresql+psycopg2://"):
            dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)

        os.environ["LLM_ROUTER_BUDGET_POSTGRES_DSN"] = dsn

        from llm_router.budget_backend_postgres import PostgresBudgetBackend

        cost = 1.0
        winners_allowed = 3
        cap = cost * winners_allowed  # $3.00 → exactly 3 of the racers win

        bootstrap = PostgresBudgetBackend()
        key = _k(user="stress-oversubscription")
        bootstrap.register(key, cap_usd=cap)
        bootstrap.close()

        per_process = 20
        process_count = 8  # 8 × 20 = 160 attempts, only 3 slots
        with mp.get_context("spawn").Pool(process_count) as pool:
            wins = pool.map(
                _attempt_in_subprocess,
                [
                    (dsn, "stress-oversubscription", cost, per_process)
                    for _ in range(process_count)
                ],
            )
        total = sum(wins)
        assert total == winners_allowed, (
            f"Expected exactly {winners_allowed} successful reservations under "
            f"{process_count * per_process}-way contention across {process_count} "
            f"processes; got {total} (per-process: {wins}). A total > "
            f"{winners_allowed} means the atomic check-then-charge lost an update "
            f"(double-spend); < {winners_allowed} means spurious starvation."
        )

        verify = PostgresBudgetBackend()
        assert verify.pending(key) == pytest.approx(cap), (
            "Persisted pending must equal exactly cap (3 × cost) — any drift "
            "means a reservation was charged without being counted, or vice versa."
        )
        verify.close()
        with suppress(KeyError):
            del os.environ["LLM_ROUTER_BUDGET_POSTGRES_DSN"]


def _load_worker(args: tuple[str, str, float, int]) -> dict[str, object]:
    """Run realistic sequential reserve->commit spend cycles in a fresh
    spawn-safe process and return successes plus per-op latencies."""
    dsn, key_user, cost, ops = args
    import asyncio
    import os
    import time

    os.environ["LLM_ROUTER_BUDGET_POSTGRES_DSN"] = dsn
    from llm_router.budget_backend_postgres import PostgresBudgetBackend
    from llm_router.budget_key import SCOPE_TURN as _SCOPE, BudgetKey as _BK

    backend = PostgresBudgetBackend()
    key = _BK(
        tenant_id="t1",
        org_id="o1",
        user_id=key_user,
        agent_id=None,
        scope=_SCOPE,
    )

    async def _run() -> dict[str, object]:
        ok = 0
        latencies_ms: list[float] = []
        for _ in range(ops):
            started = time.perf_counter()
            reserved = await backend.try_reserve(key, cost)
            if reserved:
                await backend.commit(key, cost)
                ok += 1
            latencies_ms.append((time.perf_counter() - started) * 1000.0)
        return {"ok": ok, "attempts": ops, "latencies_ms": latencies_ms}

    try:
        return asyncio.run(_run())
    finally:
        backend.close()


@pytest.mark.skipif(
    not _DOCKER_AVAILABLE,
    reason="Docker daemon not reachable — Postgres integration test skipped",
)
def test_load_realistic_concurrency() -> None:
    """Realistic-load throughput/latency companion to the exactly-N contention
    test.

    This drives the normal reserve->commit spend path under real
    multi-process concurrency with enough headroom that nearly every
    reservation should succeed. The assertions guard correctness under
    load and basic forward progress, not a hardware-specific SLA.
    """
    import math
    import time

    from testcontainers.postgres import PostgresContainer

    workers = 6
    ops_per_worker = 200
    total_ops = workers * ops_per_worker
    cost = 0.001
    cap = total_ops * cost * 10.0

    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url(driver=None)
        if dsn.startswith("postgresql+psycopg2://"):
            dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)

        os.environ["LLM_ROUTER_BUDGET_POSTGRES_DSN"] = dsn

        from llm_router.budget_backend_postgres import PostgresBudgetBackend

        key = _k(user="realistic-load")
        bootstrap = PostgresBudgetBackend()
        bootstrap.register(key, cap_usd=cap)
        bootstrap.close()

        started = time.perf_counter()
        with mp.get_context("spawn").Pool(workers) as pool:
            results = pool.map(
                _load_worker,
                [(dsn, "realistic-load", cost, ops_per_worker) for _ in range(workers)],
            )
        elapsed = time.perf_counter() - started

        total_attempts = sum(int(result["attempts"]) for result in results)
        total_successes = sum(int(result["ok"]) for result in results)
        latencies_ms = sorted(
            latency
            for result in results
            for latency in result["latencies_ms"]  # type: ignore[index]
        )
        throughput = total_attempts / elapsed

        def _percentile(sorted_values: list[float], pct: float) -> float:
            if not sorted_values:
                return float("nan")
            index = min(
                len(sorted_values) - 1,
                max(0, int(math.ceil((pct / 100.0) * len(sorted_values)) - 1)),
            )
            return sorted_values[index]

        p50 = _percentile(latencies_ms, 50)
        p95 = _percentile(latencies_ms, 95)
        p99 = _percentile(latencies_ms, 99)

        print(
            f"realistic-load throughput={throughput:.2f} ops/sec "
            f"p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms"
        )

        assert total_successes == total_ops, (
            f"Expected every realistic-load op to succeed with large cap; "
            f"got {total_successes}/{total_ops} successes"
        )
        assert total_attempts == total_ops

        verify = PostgresBudgetBackend()
        assert verify.consumed(key) == pytest.approx(total_successes * cost)
        assert verify.pending(key) == pytest.approx(0.0)
        verify.close()

        assert throughput > 1.0, (
            f"Realistic-load test made insufficient progress: throughput="
            f"{throughput:.4f} ops/sec over {elapsed:.2f}s for {total_attempts} ops"
        )
        assert math.isfinite(p99) and p99 > 0.0

        with suppress(KeyError):
            del os.environ["LLM_ROUTER_BUDGET_POSTGRES_DSN"]
