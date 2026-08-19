"""Iteration 2 acceptance — PostgresControlPlaneStore.

Two layers:
1. Fake-psycopg unit tests that RUN LOCALLY (inject a mock psycopg so no real
   psycopg/libpq is needed) — verify the store issues the expected SQL in the
   right order (execute-before-fetch, commits) and maps rows to the shared
   dataclasses. Catches the class of bug a weak model produced (fetch without
   execute, wrong columns, redefined dataclasses).
2. An optional real-Postgres integration test via testcontainers, guarded by
   Docker — skips cleanly in CI/laptops without Docker.
"""
from __future__ import annotations

import sys
import types

import pytest


# ── 1. Fake psycopg so the store's SQL flow runs without real psycopg ─────────


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params: tuple = ()):  # noqa: D401
        self._conn.calls.append((" ".join(sql.split()), params))
        low = sql.lower()
        # Serve the reads the store performs, from the fake's in-memory state.
        if "coalesce(max(version)" in low:
            self._result = [(self._conn.max_version,)]
        elif "from llm_router_cp_tenants where tenant_id" in low:
            self._result = [self._conn.tenant_row] if self._conn.tenant_row else []
        elif "from llm_router_cp_policy_versions where tenant_id" in low and "version" in low:
            self._result = [self._conn.version_row] if self._conn.version_row else []
        elif "join llm_router_cp_policy_versions" in low:
            self._result = [self._conn.version_row] if self._conn.active else []
        elif "from llm_router_cp_instances where tenant_id" in low:
            self._result = list(self._conn.instance_rows)
        elif "from llm_router_cp_instances where instance_id" in low:
            self._result = [self._conn.instance_rows[0]] if self._conn.instance_rows else []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.committed = 0
        self.max_version = 0
        self.tenant_row = ("t1", "o1", 123.0)
        self.version_row = ("t1", 1, "a: 1", '{"a":1}', "admin", "n", 123.0)
        self.instance_rows = [("i1", "t1", 1, "d", "control_plane", "", None, 123.0)]
        self.active = True

    def cursor(self):
        return _FakeCursor(self)

    def transaction(self):
        return _FakeTxn()

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def fake_pg(monkeypatch):
    fake_conn = _FakeConn()
    mod = types.ModuleType("psycopg")
    mod.connect = lambda *a, **k: fake_conn  # type: ignore[attr-defined]
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()  # type: ignore[attr-defined]
    mod.rows = rows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setenv("LLM_ROUTER_CP_POSTGRES_DSN", "postgresql://fake/db")
    return fake_conn


def _store():
    from llm_router.control_plane.store_postgres import PostgresControlPlaneStore
    return PostgresControlPlaneStore()


def test_requires_dsn(monkeypatch, fake_pg):
    monkeypatch.delenv("LLM_ROUTER_CP_POSTGRES_DSN", raising=False)
    from llm_router.control_plane.store_postgres import PostgresControlPlaneStore
    with pytest.raises(RuntimeError):
        PostgresControlPlaneStore(dsn="")


def test_append_uses_max_version_then_insert(fake_pg):
    fake_pg.max_version = 2  # existing v1,v2 -> store must compute next = 3
    rec = _store().append_policy_version(
        "t1", yaml_text="a: 1", normalized_json='{"a":1}', actor="admin"
    )
    assert rec is not None  # a record is returned
    sqls = [c[0].lower() for c in fake_pg.calls]
    # The store INSERTs the next version (max+1 = 3), and does the MAX select
    # BEFORE the insert (execute-before-fetch — the bug a weak model had).
    max_idx = next(i for i, s in enumerate(sqls) if "coalesce(max(version)" in s)
    ins_idx = next(i for i, s in enumerate(sqls) if "insert into llm_router_cp_policy_versions" in s)
    assert max_idx < ins_idx
    # Confirm the computed version (3) is what got inserted (params of the insert).
    ins_params = next(p for s, p in fake_pg.calls if "insert into llm_router_cp_policy_versions" in s.lower())
    assert ins_params[1] == 3  # (tenant_id, version, ...) -> version param is 3


def test_get_current_policy_maps_row(fake_pg):
    rec = _store().get_current_policy("t1")
    assert rec is not None and rec.tenant_id == "t1" and rec.version == 1


def test_record_heartbeat_upserts_instance_and_history(fake_pg):
    rec = _store().record_heartbeat(
        instance_id="i1", tenant_id="t1", effective_version=1,
        effective_digest="d", source="control_plane",
    )
    assert rec.instance_id == "i1" and rec.effective_version == 1
    sqls = " || ".join(c[0].lower() for c in fake_pg.calls)
    assert "insert into llm_router_cp_instances" in sqls
    assert "insert into llm_router_cp_heartbeats" in sqls  # history appended too


def test_list_instances_maps_rows(fake_pg):
    out = _store().list_instances("t1")
    assert [i.instance_id for i in out] == ["i1"]
    assert out[0].last_seen_at == 123.0


# ── 2. Optional real-Postgres integration (skips without Docker) ──────────────

try:
    import shutil
    _DOCKER = shutil.which("docker") is not None
    import testcontainers.postgres  # noqa: F401
    _HAVE_TC = True
except Exception:
    _HAVE_TC = False
    _DOCKER = False


@pytest.mark.skipif(not (_DOCKER and _HAVE_TC), reason="Docker/testcontainers unavailable")
def test_real_postgres_roundtrip():
    from testcontainers.postgres import PostgresContainer
    from llm_router.control_plane.store_postgres import PostgresControlPlaneStore

    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url(driver=None)
        if dsn.startswith("postgresql+psycopg2://"):
            dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
        s = PostgresControlPlaneStore(dsn=dsn)
        s.ensure_tenant("t1", org_id="o1")
        assert s.append_policy_version("t1", yaml_text="a: 1", normalized_json='{"a":1}', actor="admin").version == 1
        assert s.append_policy_version("t1", yaml_text="a: 2", normalized_json='{"a":2}', actor="admin").version == 2
        assert s.get_current_policy("t1") is None
        s.set_active_policy("t1", 2)
        assert s.get_current_policy("t1").version == 2
        s.record_heartbeat(instance_id="i1", tenant_id="t1", effective_version=2, effective_digest="d", source="control_plane")
        assert [i.instance_id for i in s.list_instances("t1")] == ["i1"]
        s.close()
