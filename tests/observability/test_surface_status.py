"""Tests for llm_router.observability.surface_status.

Offline and fail-soft: everything runs against a temp state dir with synthetic
savings_log.jsonl records — no network, no rich, no real ~/.llm-router.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from llm_router.observability import surface_status as ss


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the module at an isolated temp state dir and force a known now()."""
    monkeypatch.setenv("LLM_ROUTER_STATE_DIR", str(tmp_path))
    # No provider keys / NO_COLOR so renderers are deterministic.
    for k in ss._PROVIDER_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    return tmp_path


def _write_log(state_dir, records):
    (state_dir / ss._SAVINGS_LOG).write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


NOW = 1_700_000_000.0  # fixed epoch for deterministic "age"/"today" math


def test_no_log_is_down_and_fail_soft(state_dir):
    """No savings log at all → a valid 'down/no route' status, never raises."""
    status = ss.compute_status("claude_code", now=NOW)
    assert status.last_model is None
    assert status.routed_count_session == 0
    assert status.health == ss.HEALTH_DOWN  # no key, no local route
    assert status.active is False


def test_last_route_and_today_aggregates(state_dir):
    _write_log(state_dir, [
        {"timestamp": _iso(NOW - 100), "host": "claude_code",
         "model": "ollama/hermes3:8b", "task_type": "code", "complexity": "moderate",
         "estimated_saved": 0.03, "input_tokens": 400, "output_tokens": 540},
        {"timestamp": _iso(NOW - 50), "host": "claude_code",
         "model": "ollama/qwen2.5-coder:7b", "task_type": "query", "complexity": "simple",
         "estimated_saved": 0.01, "input_tokens": 100, "output_tokens": 47},
    ])
    st = ss.compute_status("claude_code", now=NOW)
    # last route = the most recent record
    assert st.last_model == "ollama/qwen2.5-coder:7b"
    assert st.last_task == "query/simple"
    assert st.short_model() == "qwen2.5-coder:7b"
    assert st.last_tokens == 147
    # today aggregates
    assert st.routed_count_session == 2
    assert st.saved_session == pytest.approx(0.04)
    assert st.tokens_session == 400 + 540 + 100 + 47
    # a recent ollama route makes it reachable → not down; active within window
    assert st.active is True
    assert st.health != ss.HEALTH_DOWN  # reachable (ok/degraded depends on usage freshness)


def test_host_alias_matching(state_dir):
    """A record stamped 'claude-code' matches the 'claude_code' canonical host."""
    _write_log(state_dir, [
        {"timestamp": _iso(NOW - 10), "host": "claude-code",
         "model": "ollama/hermes3:8b", "task_type": "code", "complexity": "moderate",
         "estimated_saved": 0.02, "input_tokens": 10, "output_tokens": 10},
    ])
    st = ss.compute_status("claude_code", now=NOW)
    assert st.last_model == "ollama/hermes3:8b"
    assert st.routed_count_session == 1


def test_compact_line_contains_route_and_glyph(state_dir):
    _write_log(state_dir, [
        {"timestamp": _iso(NOW - 5), "host": "claude_code",
         "model": "ollama/hermes3:8b", "task_type": "code", "complexity": "moderate",
         "estimated_saved": 0.03, "input_tokens": 400, "output_tokens": 600},
    ])
    st = ss.compute_status("claude_code", now=NOW)
    line = ss.compact_line(st, color=False)
    assert "llm-router" in line
    assert "hermes3:8b" in line
    assert "code/moderate" in line
    assert "$0.03 saved" in line
    assert st.health_glyph() in line
    # never leaks the chuzom brand
    assert "chuzom" not in line.lower()


def test_compact_line_no_route_is_honest(state_dir):
    st = ss.compute_status("claude_code", now=NOW)
    line = ss.compact_line(st, color=False)
    assert "no route yet" in line


def test_terminal_title_is_osc_escape(state_dir):
    _write_log(state_dir, [
        {"timestamp": _iso(NOW - 5), "host": "claude_code",
         "model": "ollama/hermes3:8b", "task_type": "code", "complexity": "moderate",
         "estimated_saved": 0.03, "input_tokens": 1200, "output_tokens": 300},
    ])
    st = ss.compute_status("claude_code", now=NOW)
    title = ss.terminal_title(st)
    assert title.startswith("\033]2;")
    assert title.endswith("\007")
    assert "hermes3:8b" in title
    assert "1.5k tok" in title  # 1500 tokens compacted


def test_malformed_log_degrades_not_raises(state_dir):
    """Garbage / partial lines must be skipped, never raise."""
    (state_dir / ss._SAVINGS_LOG).write_text(
        "not json\n"
        "{ half a record \n"
        + json.dumps({"timestamp": _iso(NOW - 5), "host": "claude_code",
                      "model": "ollama/hermes3:8b", "task_type": "code",
                      "complexity": "moderate", "estimated_saved": 0.02,
                      "input_tokens": 5, "output_tokens": 5}) + "\n"
        "[]\n"  # valid json but not a dict → skipped
        "12345\n",
        encoding="utf-8",
    )
    st = ss.compute_status("claude_code", now=NOW)  # must not raise
    assert st.last_model == "ollama/hermes3:8b"
    assert st.routed_count_session == 1


def test_health_down_without_provider(state_dir):
    """Old log (outside active window) + no key/local → down."""
    _write_log(state_dir, [
        {"timestamp": _iso(NOW - 100_000), "host": "claude_code",
         "model": "openai/gpt-4o", "task_type": "code", "complexity": "complex",
         "estimated_saved": 0.5, "input_tokens": 100, "output_tokens": 100},
    ])
    st = ss.compute_status("claude_code", now=NOW)
    assert st.health == ss.HEALTH_DOWN
    assert st.active is False
    # all-time saved still counts the old route
    assert st.saved_total == pytest.approx(0.5)


def test_health_ok_with_api_key(state_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # touch usage.json so it isn't stale
    (state_dir / "usage.json").write_text("{}")
    import os
    os.utime(state_dir / "usage.json", (NOW, NOW))
    st = ss.compute_status("claude_code", now=NOW)
    assert st.health == ss.HEALTH_OK


def test_notification_rate_limited(state_dir):
    _write_log(state_dir, [
        {"timestamp": _iso(NOW - 5), "host": "claude_code",
         "model": "ollama/hermes3:8b", "task_type": "code", "complexity": "moderate",
         "estimated_saved": 0.03, "input_tokens": 10, "output_tokens": 10},
    ])
    st = ss.compute_status("claude_code", now=NOW)
    first = ss.notification(st, "route", now=NOW)
    assert first is not None and "llm-router" in first["title"]
    # second call within the interval is suppressed
    second = ss.notification(st, "route", now=NOW + 10)
    assert second is None
