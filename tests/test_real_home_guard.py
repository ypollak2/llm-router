"""The real-home guard must refuse, record, and not over-block.

INCIDENT 2026-10-06: 294 rows with the constants of `tests/test_quality_guard.py` reached
the operator's live ~/.llm-router/usage.db in one day (222 naming openai/gpt-4o). See
tests/_real_home_guard.py. These tests aim the guard at a fake "real" directory under
tmp_path -- never at the operator's actual files -- by swapping the protected-roots lists.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from tests import _real_home_guard as g


@pytest.fixture
def fake_real(tmp_path, monkeypatch):
    llm = tmp_path / "realhome" / ".llm-router"
    claude = tmp_path / "realhome" / ".claude"
    llm.mkdir(parents=True)
    claude.mkdir(parents=True)
    monkeypatch.setattr(g, "PROTECTED_WRITE", [llm, claude])
    monkeypatch.setattr(g, "PROTECTED_SQLITE", [llm])
    before = len(g.VIOLATIONS)
    yield llm, claude
    # The refusals below are the point of these tests; consume them so the autouse
    # `_real_home_untouched` fixture does not fail the test for doing its job.
    del g.VIOLATIONS[before:]


def test_sqlite_connect_to_protected_dir_is_refused_and_recorded(fake_real):
    llm, _ = fake_real
    target = llm / "usage.db"
    with pytest.raises(PermissionError):
        sqlite3.connect(str(target))
    assert not target.exists(), "the refusal must happen BEFORE the file is created"
    assert g.VIOLATIONS[-1][0] == "sqlite3.connect"


def test_open_for_write_is_refused_but_read_is_allowed(fake_real):
    llm, claude = fake_real
    with pytest.raises(PermissionError):
        open(claude / "settings.json", "w")
    with pytest.raises(PermissionError):
        os.open(llm / "x", os.O_WRONLY | os.O_CREAT)
    seed = llm / "readme"
    seed.parent.mkdir(exist_ok=True)
    # create it legitimately by lifting the guard for one call
    g.PROTECTED_WRITE.clear()
    seed.write_text("x")
    g.PROTECTED_WRITE.extend([llm, claude])
    assert seed.read_text() == "x"  # reading is not blocked


def test_swallowed_refusal_is_still_recorded(fake_real):
    """A fail-open `except Exception: pass` must not hide the attempt."""
    llm, _ = fake_real
    try:
        sqlite3.connect(str(llm / "usage.db"))
    except Exception:  # noqa: BLE001 -- the pattern this codebase is full of
        pass
    assert any(a == "sqlite3.connect" for a, _p in g.VIOLATIONS)


def test_path_outside_protected_dirs_is_untouched(fake_real, tmp_path):
    ok = tmp_path / "elsewhere.db"
    sqlite3.connect(str(ok)).close()
    assert ok.exists()
    assert sqlite3.connect(":memory:")  # in-memory never resolves to a path


def test_symlink_into_protected_dir_is_resolved_for_databases(fake_real, tmp_path):
    llm, _ = fake_real
    link = tmp_path / "innocent"
    link.symlink_to(llm)
    with pytest.raises(PermissionError):
        sqlite3.connect(str(link / "usage.db"))


def test_suite_runs_with_sandboxed_home():
    """HOME is not the operator's, and the guard's real home is."""
    assert Path.home() != g.TRUE_HOME
    assert Path.home().exists()
    assert str(g.TRUE_HOME / ".llm-router") in map(str, g.PROTECTED_SQLITE)
    # the guard is live in this very process
    assert g._installed is True
