"""D-112: owner surfaces distinguish served runtime routes from hints."""
from __future__ import annotations

import importlib.util
import io
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from llm_router.cost import CREATE_ROUTING_DECISIONS_TABLE
from llm_router.lineage import LineageStore
from llm_router.observability.summary import collect, render_markdown
from llm_router.routing_report import executed_routes, format_executed_routes
from llm_router.ui.session_summary import SessionSummaryDashboard


@pytest.fixture
def usage_db(tmp_path, monkeypatch):
    path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(path))
    con = sqlite3.connect(path)
    try:
        con.execute(CREATE_ROUTING_DECISIONS_TABLE)
        con.execute("ALTER TABLE routing_decisions ADD COLUMN provenance TEXT")
        con.execute("ALTER TABLE routing_decisions ADD COLUMN reason_code TEXT")
        rows = [
            ("-1 hours", "codex", "runtime", None),
            ("-1 hours", "codex", "runtime", None),
            ("-23 hours", "codex", "runtime", None),
            ("-1 hours", "ollama", "runtime", None),
            ("-24 hours", "ollama", "runtime", None),
            ("-3 days", "ollama", "runtime", None),
            ("-1 hours", "ollama", "test", None),
            ("-1 hours", "ollama", "unattributed", None),
            ("-1 hours", "ollama", None, None),
            ("-1 hours", "ollama", "runtime", "cache_hit"),
            ("-1 hours", "cache", "runtime", None),
            ("-1 hours", "ollama", "runtime", "error_timeout"),
            ("-1 hours", "ollama", "runtime", "breaker_open"),
        ]
        for age, provider, provenance, reason in rows:
            minutes = "-59 minutes" if age == "-23 hours" else (
                "-1 minutes" if age == "-24 hours" else "0 minutes"
            )
            con.execute(
                "INSERT INTO routing_decisions "
                "(timestamp, final_provider, provenance, reason_code) "
                "VALUES (datetime('now', ?, ?), ?, ?, ?)",
                (age, minutes, provider, provenance, reason),
            )
        con.commit()
    finally:
        con.close()
    return path


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    path = Path(__file__).resolve().parents[1] / "src/llm_router/hooks/auto-route.py"
    spec = importlib.util.spec_from_file_location("auto_route_d112", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr("llm_router.routing_report.draft_acceptance", lambda: (0, 3))
    return module


def _empty_summary():
    return collect(
        lineage_store=SimpleNamespace(recent=lambda **kwargs: []),
        session_store=SimpleNamespace(),
        since_seconds=86400,
    )


def _panel_text(result):
    console = Console(record=True, width=200, file=io.StringIO())
    panel = SessionSummaryDashboard(console=console).render_main_panel(
        decisions=[{"method": "heuristic", "count": 101}],
        executed_routes=result,
    )
    console.print(panel)
    return console.export_text()


def test_runtime_provider_counts_and_format(usage_db):
    result = executed_routes()
    assert result == {
        "window_hours": 24.0,
        "total": 4,
        "by_provider": {"codex": 3, "ollama": 1},
    }
    assert format_executed_routes(result) == (
        "routes 24h: 4 executed · codex 3 · local 1"
    )


def test_utc_window_boundary(tmp_path, monkeypatch):
    path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(path))
    con = sqlite3.connect(path)
    try:
        con.execute(CREATE_ROUTING_DECISIONS_TABLE)
        con.execute("ALTER TABLE routing_decisions ADD COLUMN provenance TEXT")
        con.execute("ALTER TABLE routing_decisions ADD COLUMN reason_code TEXT")
        con.execute(
            "INSERT INTO routing_decisions (timestamp, final_provider, provenance) "
            "VALUES (datetime('now','-23 hours','-59 minutes'), 'codex', 'runtime'), "
            "(datetime('now','-24 hours','-1 minutes'), 'ollama', 'runtime')"
        )
        con.commit()
    finally:
        con.close()
    assert executed_routes() == {
        "window_hours": 24.0, "total": 1, "by_provider": {"codex": 1},
    }
    assert format_executed_routes(executed_routes()) == (
        "routes 24h: 1 executed · codex 1 · local 0"
    )


def test_banner_labels_hints_and_survives_empty_log(usage_db, hook, monkeypatch):
    monkeypatch.setattr(
        LineageStore, "recent",
        lambda self, **kwargs: [{"model_tier": "local", "task_type": "code"}] * 3,
    )
    banner = hook._build_mini_summary()
    assert "routes 24h: 4 executed" in banner
    assert "codex 3" in banner
    assert "local 1" in banner
    assert "(executed, usage.db)" in banner
    assert "hints" in banner
    monkeypatch.setattr(LineageStore, "recent", lambda self, **kwargs: [])
    banner = hook._build_mini_summary()
    assert "routes 24h: 4 executed" in banner
    assert "hints" not in banner


def test_banner_survives_execution_reporting_error(hook, monkeypatch):
    failure = RuntimeError("execution report unavailable")
    records = []

    def raise_execution_error(*args, **kwargs):
        raise failure

    monkeypatch.setattr(
        "llm_router.routing_report.executed_routes", raise_execution_error,
    )
    monkeypatch.setattr(
        "llm_router.failopen.record", lambda code, exc: records.append((code, exc)),
    )
    monkeypatch.setattr(LineageStore, "recent", lambda self, **kwargs: [])
    banner = hook._build_mini_summary()
    assert banner is not None
    assert "no execution data" in banner
    assert "session check" in banner
    assert records == [("CHZ-FO-HOOK-EXECUTED-ROUTES", failure)]


def test_stop_dashboard_labels_execution_and_classifier_counts(usage_db):
    lines = [
        " ".join(line.split())
        for line in _panel_text(executed_routes(24.0)).splitlines()
    ]
    assert any("ROUTING 24h 4 executed" in line for line in lines)
    assert any("codex 3" in line for line in lines)
    assert any("local 1" in line for line in lines)
    assert any("hints today: 101 classified" in line for line in lines)
    assert not any("today 101 decisions" in line for line in lines)


def test_summary_reports_execution_without_hint_rows(usage_db):
    data = _empty_summary()
    assert data.total_decisions == 0
    text = render_markdown(data)
    assert "Executed routes:" in text
    assert "routes 24h: 4 executed · codex 3 · local 1" in text
    assert "Classified prompts (hints):** 0" in text


def test_missing_db_surfaces_remain_visible(tmp_path, monkeypatch, hook):
    path = tmp_path / "missing.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(path))
    monkeypatch.setattr(LineageStore, "recent", lambda self, **kwargs: [])
    assert executed_routes() is None
    expected = "routes 24h: no execution data (usage.db missing or unreadable)"
    assert format_executed_routes(None) == expected
    assert expected in hook._build_mini_summary()
    lines = [
        " ".join(line.split())
        for line in _panel_text(None).splitlines()
    ]
    assert any("ROUTING 24h no execution data" in line for line in lines)
    assert "no execution data" in render_markdown(_empty_summary())
    assert not path.exists()


def test_legacy_schema_and_explicit_path_precedence(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "missing.db"))
    con = sqlite3.connect(path)
    try:
        con.execute(CREATE_ROUTING_DECISIONS_TABLE)
        con.execute(
            "INSERT INTO routing_decisions (timestamp, final_provider) "
            "VALUES (datetime('now','-1 hours'), '')"
        )
        con.commit()
    finally:
        con.close()
    assert executed_routes(2.5, db_path=path) == {
        "window_hours": 2.5, "total": 1, "by_provider": {"unknown": 1},
    }
    assert format_executed_routes(executed_routes(2.5, db_path=path)) == (
        "routes 2.5h: 1 executed · unknown 1 · local 0"
    )


def test_unreadable_db_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a SQLite database")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(path))
    assert executed_routes() is None
