"""D-112: owner surfaces distinguish served runtime routes from hints."""
from __future__ import annotations

import importlib.util
import io
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from llm_router.cost import CREATE_ROUTING_DECISIONS_TABLE
from llm_router.lineage import LineageStore
from llm_router.observability.summary import collect, render, render_markdown
from llm_router.routing_report import executed_routes, format_executed_routes
from llm_router.ui.session_summary import SessionSummaryDashboard


@pytest.fixture
def usage_db(tmp_path, monkeypatch):
    path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
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


def test_runtime_provider_counts_and_format(usage_db, non_utc_timezone):
    result = executed_routes()
    assert result == {
        "window_hours": 24.0,
        "total": 4,
        "by_provider": {"codex": 3, "ollama": 1},
    }
    assert format_executed_routes(result) == (
        "routes 24h: 4 executed · codex 3 · local 1"
    )


def test_utc_window_boundary(tmp_path, monkeypatch, non_utc_timezone):
    path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
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


@pytest.fixture
def non_utc_timezone():
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is unavailable")
    original = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_summary_respects_execution_window(usage_db, non_utc_timezone):
    with sqlite3.connect(usage_db) as con:
        con.execute("DELETE FROM routing_decisions")
        con.execute(
            "INSERT INTO routing_decisions (timestamp, final_provider, provenance) "
            "VALUES (datetime('now','-1 hours'), 'codex', 'runtime'), "
            "(datetime('now','-3 hours'), 'ollama', 'runtime')"
        )
    for hours, total in ((2, 1), (24, 2)):
        data = collect(
            lineage_store=SimpleNamespace(recent=lambda **kwargs: []),
            session_store=SimpleNamespace(),
            since_seconds=hours * 3600,
        )
        assert data.executed["total"] == total
        assert f"routes {hours}h:" in render_markdown(data)


def test_missing_execution_data_keeps_summary_window(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "missing.db"))
    data = collect(
        lineage_store=SimpleNamespace(recent=lambda **kwargs: []),
        session_store=SimpleNamespace(),
        since_seconds=2 * 3600,
    )
    assert data.executed is None
    assert "routes 2h: no execution data" in render_markdown(data)
    console = Console(record=True, width=200, file=io.StringIO())
    render(data, console=console)
    assert "routes 2h: no execution data" in console.export_text()


def test_tracking_summary_labels_hints_and_executions(usage_db, monkeypatch):
    from llm_router import model_tracking

    monkeypatch.setattr(model_tracking, "get_model_usage_stats", lambda hours: {"local": 101})
    monkeypatch.setattr(model_tracking, "get_task_type_distribution", lambda hours: {})
    monkeypatch.setattr(model_tracking, "get_classification_method_stats", lambda hours: {})
    text = model_tracking.display_tracking_summary(2)
    assert "Classifier hints (Last 2h)" in text
    assert "prompts classified by the hook, not executed routes" in text
    assert "Top Models Suggested (hints)" in text
    assert "Executed: routes 2h: 3 executed · codex 2 · local 1" in text


def test_explainer_distinguishes_executions_and_hints(usage_db):
    from llm_router.commands.explain_dashboard import _routing_panel_block

    text = "\n".join(_routing_panel_block())
    assert "usage.db routing_decisions (executed header)" in text
    assert "provenance='runtime', real decisions" in text
    assert "routing_report.executed_routes" in text
    assert "last 24h (UTC)" in text
    assert "routes 24h: 4 executed" in text
    assert "Hints today: classifier log model_tracking.jsonl" in text
    assert "session-end.py:_query_routing_logic" in text
    assert "today (start-of-day local)" in text


def test_stop_main_passes_executed_routes_to_dashboard(usage_db, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_STOP_HOOK", "full")
    monkeypatch.delenv("LLM_ROUTER_ROLLING_SUMMARY", raising=False)
    path = Path(__file__).resolve().parents[1] / "src/llm_router/hooks/session-end.py"
    spec = importlib.util.spec_from_file_location("session_end_d112", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name": "Stop"}'))
    monkeypatch.setattr(module, "HAS_RICH_DASHBOARD", True)
    monkeypatch.setattr(module, "_spawn_background_stop_work", lambda: None)
    monkeypatch.setattr(module, "_read_session_start", lambda: 0)
    monkeypatch.setattr(module, "_query_session_data", lambda start: ([], [], []))
    monkeypatch.setattr(module, "_get_cc_usage", lambda: ({}, {}, False))
    monkeypatch.setattr(module, "_sync_import_savings_log", lambda: None)
    monkeypatch.setattr(module, "_query_cumulative_savings", lambda: [])
    monkeypatch.setattr(module, "_collect_report_data", lambda *args: {})
    monkeypatch.setattr(module, "_query_session_metrics", lambda start: {})
    monkeypatch.setattr(module, "_query_daily_cache_trend", lambda: [])
    monkeypatch.setattr(module, "_read_session_spend", lambda: {})
    monkeypatch.setattr(module, "_pop_stop_notes", lambda: [])
    monkeypatch.setattr(module, "_read_session_id", lambda: None)
    monkeypatch.setattr("llm_router.verify_queue.spawn_worker_if_needed", lambda: None)
    monkeypatch.setattr("llm_router.quota_samples.append_session_sample", lambda *args: None)
    monkeypatch.setattr("llm_router.monitoring.periodic.load_session_snapshots", lambda: [])
    monkeypatch.setattr("llm_router.hooks.lineage_integration.format_routing_section", lambda: "")
    monkeypatch.setattr("llm_router.quality_breaker.stop_line_summary", lambda: "")
    captured = []

    def capture_dashboard(self, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(SessionSummaryDashboard, "print_dashboard", capture_dashboard)
    module.main()
    assert len(captured) == 1
    assert captured[0]["executed_routes"] == {
        "window_hours": 24.0, "total": 4, "by_provider": {"codex": 3, "ollama": 1},
    }
