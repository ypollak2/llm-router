"""Tests for the migration framework.

Covers the four states ``status`` distinguishes (applied, pending,
missing_down, drifted), the up/down round-trip on a real SQLite file,
and the safety check that refuses to roll back migrations without
a down() function.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from llm_router.migrations import (
    apply,
    discover,
    rollback,
    status,
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_migration(tmp_path: Path, *, version: str, name: str,
                    up_sql: str, down_sql: str | None = None) -> Path:
    """Write a migration file into ``tmp_path`` and return its path."""
    body = [
        f'version = "{version}"',
        f'name = "{name}"',
        "import sqlite3",
        "",
        "def up(c: sqlite3.Connection) -> None:",
    ]
    body.extend(f"    c.execute({line!r})" for line in textwrap.dedent(up_sql).strip().splitlines())
    if down_sql is not None:
        body.append("")
        body.append("def down(c: sqlite3.Connection) -> None:")
        body.extend(f"    c.execute({line!r})" for line in textwrap.dedent(down_sql).strip().splitlines())
    path = tmp_path / f"{version}_{name.replace('-', '_')}.py"
    path.write_text("\n".join(body))
    return path


# ── Discovery ───────────────────────────────────────────────────────────


def test_discover_orders_by_version_string(tmp_path):
    _make_migration(tmp_path, version="002", name="b", up_sql="SELECT 2")
    _make_migration(tmp_path, version="001", name="a", up_sql="SELECT 1")
    _make_migration(tmp_path, version="010", name="c", up_sql="SELECT 10")
    mods = discover(tmp_path)
    assert [m.version for m in mods] == ["001", "002", "010"]


def test_discover_skips_underscored_and_non_compliant_files(tmp_path):
    _make_migration(tmp_path, version="001", name="ok", up_sql="SELECT 1")
    # File starting with underscore — should be skipped
    (tmp_path / "_helpers.py").write_text("def helper(): pass\n")
    # File without version / name attributes
    (tmp_path / "999_invalid.py").write_text("print('not a migration')\n")
    mods = discover(tmp_path)
    assert [m.version for m in mods] == ["001"]


# ── Apply / rollback round-trip ─────────────────────────────────────────


def test_apply_creates_ledger_and_table(tmp_path):
    _make_migration(
        tmp_path,
        version="001",
        name="create-widgets",
        up_sql="CREATE TABLE widgets (id INTEGER PRIMARY KEY)",
        down_sql="DROP TABLE widgets",
    )

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    try:
        applied = apply(conn, tmp_path)
        assert len(applied) == 1 and applied[0].version == "001"
        # Migration ran — widgets table exists
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='widgets'"
        ).fetchone() is not None
        # Ledger ran — llm_router_migrations table populated
        rows = conn.execute("SELECT version, name FROM llm_router_migrations").fetchall()
        assert rows == [("001", "create-widgets")]
    finally:
        conn.close()


def test_apply_is_idempotent(tmp_path):
    _make_migration(
        tmp_path, version="001", name="a",
        up_sql="CREATE TABLE w (id INTEGER PRIMARY KEY)",
        down_sql="DROP TABLE w",
    )
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    try:
        apply(conn, tmp_path)
        second = apply(conn, tmp_path)
        assert second == [], "second apply should be a no-op"
    finally:
        conn.close()


def test_rollback_reverses_a_migration(tmp_path):
    _make_migration(
        tmp_path, version="001", name="add-table",
        up_sql="CREATE TABLE w (id INTEGER PRIMARY KEY)",
        down_sql="DROP TABLE w",
    )
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    try:
        apply(conn, tmp_path)
        reversed_ = rollback(conn, tmp_path, steps=1)
        assert reversed_ == ["001"]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='w'"
        ).fetchone() is None
        # Ledger reflects the rollback
        assert conn.execute(
            "SELECT COUNT(*) FROM llm_router_migrations"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_rollback_refuses_when_down_missing(tmp_path):
    _make_migration(
        tmp_path, version="001", name="no-down",
        up_sql="CREATE TABLE w (id INTEGER PRIMARY KEY)",
        down_sql=None,  # explicitly omit
    )
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    try:
        apply(conn, tmp_path)
        with pytest.raises(RuntimeError, match="no down"):
            rollback(conn, tmp_path, steps=1)
    finally:
        conn.close()


# ── Status report ───────────────────────────────────────────────────────


def test_status_reports_pending_and_applied(tmp_path):
    _make_migration(
        tmp_path, version="001", name="a",
        up_sql="CREATE TABLE w (id INTEGER PRIMARY KEY)",
        down_sql="DROP TABLE w",
    )
    _make_migration(
        tmp_path, version="002", name="b",
        up_sql="CREATE TABLE x (id INTEGER PRIMARY KEY)",
        down_sql="DROP TABLE x",
    )
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    try:
        apply(conn, tmp_path, target="001")
        report = status(conn, tmp_path)
        assert [r.version for r in report.applied] == ["001"]
        assert report.pending == ["002"]
        assert report.missing_down == []
        assert report.drifted == []
    finally:
        conn.close()


def test_status_detects_missing_down(tmp_path):
    _make_migration(
        tmp_path, version="001", name="no-down",
        up_sql="CREATE TABLE w (id INTEGER PRIMARY KEY)",
        down_sql=None,
    )
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    try:
        apply(conn, tmp_path)
        report = status(conn, tmp_path)
        assert report.missing_down == ["001"]
    finally:
        conn.close()


def test_status_detects_drift_after_edit(tmp_path):
    """If a migration's source changes after apply, status flags it."""
    path = _make_migration(
        tmp_path, version="001", name="a",
        up_sql="CREATE TABLE w (id INTEGER)",
        down_sql="DROP TABLE w",
    )
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    try:
        apply(conn, tmp_path)
        # Simulate post-apply edit — touch the file content.
        path.write_text(path.read_text() + "\n# trailing edit\n")
        report = status(conn, tmp_path)
        assert "001" in report.drifted, "edit should be detected as drift"
    finally:
        conn.close()


# ── Real first migration round-trip ─────────────────────────────────────


def test_shipped_001_creates_and_rolls_back_llm_router_health(tmp_path):
    """The actual versions/001 must up/down cleanly on a fresh DB."""

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    try:
        applied = apply(conn)
        versions = [r.version for r in applied]
        assert "001" in versions
        # Table now exists
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='llm_router_health'"
        ).fetchone() is not None

        rollback(conn, steps=1)
        # Table removed
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='llm_router_health'"
        ).fetchone() is None
    finally:
        conn.close()
