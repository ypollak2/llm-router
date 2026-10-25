"""W0-4c -- a sqlite READER never creates usage.db.

W0-4b review N2: demo, last, replay, share, verify, gain, invoice, dashboard_data, tui,
quota_savings and ui/status_premium opened usage.db with a bare ``sqlite3.connect``. When the
state directory existed but the file did not, that created an empty 0644 usage.db. They now use
``state_files.connect_sqlite(..., create=False)``. ``gain`` also reached ``cost._get_db`` through
``get_compression_stats`` (found while testing; fixed by a guard).

CLI cases run in a subprocess on a state dir that EXISTS but holds no usage.db (a bare HOME with no
directory proves nothing: the connect fails on the missing directory before it can create a file).
Expected exit codes are the ones ``origin/main`` returns for the same HOME (measured 2026-10-25).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_CASES = [  # (argv, exit code on main)
    (["demo"], 0),
    (["last"], 1),
    (["replay"], 1),
    (["share"], 0),
    (["verify"], 1),
    (["gain"], 0),
    (["status"], 0),
]


@pytest.mark.parametrize("argv,rc", _CASES, ids=[" ".join(a) for a, _ in _CASES])
def test_cli_reader_on_fresh_state_dir_creates_no_db(tmp_path, argv, rc):
    home = tmp_path / "home"
    state = home / ".llm-router"
    state.mkdir(parents=True, mode=0o700)
    (state / "marker.txt").write_text("the directory exists; usage.db does not")
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_HOME"}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    code = "import sys; sys.argv = ['llm-router'] + sys.argv[1:]; from llm_router.cli import main; main()"
    r = subprocess.run([sys.executable, "-c", code, *argv], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120)
    assert "Traceback" not in r.stderr + r.stdout, r.stderr[-800:]
    assert r.returncode == rc, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    leftovers = sorted(p.name for p in state.iterdir() if p.name.startswith("usage.db"))
    assert not leftovers, f"`{' '.join(argv)}` created {leftovers}"
    assert (state / "marker.txt").exists(), "the scan looked at the wrong directory"


@pytest.fixture
def state(tmp_path, monkeypatch):
    s = tmp_path / "state"
    s.mkdir()
    (s / "marker.txt").write_text("x")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(s))
    return s


def _no_db(state: Path):
    assert sorted(p.name for p in state.iterdir()) == ["marker.txt"], "a reader created a file"


def test_invoice_tally_creates_nothing(state):
    from llm_router.commands import invoice
    assert invoice._llm_router_tally("openai", "2026-10") == (0.0, 0)
    _no_db(state)


def test_demo_history_creates_nothing(state):
    from llm_router.commands import demo
    assert demo._load_real_routing_history(str(state / "usage.db")) == []
    _no_db(state)


def test_quota_savings_readers_create_nothing(state):
    from datetime import datetime, timezone
    from llm_router import quota_savings
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db = state / "usage.db"
    assert quota_savings._sum_saved_usd_since(db, since) is None
    assert quota_savings._sum_cost_usd_since_for_provider(db, since, "openai") == 0.0
    _no_db(state)


def test_dashboard_data_readers_create_nothing(state):
    from llm_router import dashboard_data as dd
    db = state / "usage.db"
    dd.query_daily(db_path=db)
    dd.summary("today", db_path=str(db))
    _no_db(state)


def test_tui_fetch_creates_nothing(state, monkeypatch):
    pytest.importorskip("textual")  # optional extra; the ratchet covers tui.py's source either way
    from llm_router.dashboard import tui
    monkeypatch.setattr(tui, "_db_path", lambda: state / "usage.db")
    tui._fetch_data()
    _no_db(state)


def test_status_premium_creates_nothing(state):
    pytest.importorskip("rich")
    from llm_router.ui.status_premium import PremiumStatusCommand
    cmd = PremiumStatusCommand()
    cmd.render_routing_savings()
    _no_db(state)


def test_reader_connect_helper_raises_when_missing(state):
    """The contract every converted site relies on: missing file -> OperationalError, no file."""
    import sqlite3
    from llm_router.state_files import connect_sqlite
    with pytest.raises(sqlite3.OperationalError):
        connect_sqlite(state / "usage.db", create=False)
    _no_db(state)
