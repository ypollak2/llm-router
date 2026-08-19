"""Tests for llm_router.observability.surface_status — the cross-surface "is LLM Router working" core."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_router.observability import surface_status as ss


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("LLM_ROUTER_STATE_DIR", str(tmp_path))
    # Clear provider keys by default so health is deterministic per-test.
    for k in ss._PROVIDER_KEYS:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _write_log(state_dir: Path, records: list[dict]) -> None:
    (state_dir / ss._SAVINGS_LOG).write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )


def _rec(host="codex", model="ollama/hermes3:8b", task="code", cx="moderate",
         saved=0.01, ts=None, now=None, in_tok=0, out_tok=0) -> dict:
    if ts is None:
        ts = now if now is not None else time.time()
    return {
        "timestamp": _iso(ts),
        "session_id": "s1",
        "task_type": task,
        "complexity": cx,
        "estimated_saved": saved,
        "model": model,
        "host": host,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }


# ── compute_status ───────────────────────────────────────────────────────────
def test_no_log_yields_idle_down(state_dir):
    s = ss.compute_status("codex", now=1_000_000.0)
    assert s.active is False
    assert s.last_model is None
    assert s.routed_count_session == 0
    assert s.saved_session == 0.0
    # No providers configured and no routes → down.
    assert s.health == ss.HEALTH_DOWN


def test_last_route_and_active(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(now=now, ts=now - 60)])
    s = ss.compute_status("codex", now=now)
    assert s.active is True
    assert s.last_model == "ollama/hermes3:8b"
    assert s.last_task == "code/moderate"
    assert s.short_model() == "hermes3:8b"
    assert s.last_age_s == pytest.approx(60, abs=1)


def test_stale_route_is_inactive(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(now=now, ts=now - ss.ACTIVE_WINDOW_S - 100)])
    s = ss.compute_status("codex", now=now)
    assert s.active is False


def test_host_filtering(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [
        _rec(host="codex", model="ollama/a", ts=now - 30),
        _rec(host="gemini_cli", model="ollama/b", ts=now - 10),
    ])
    assert ss.compute_status("codex", now=now).short_model() == "a"
    assert ss.compute_status("gemini_cli", now=now).short_model() == "b"


def test_today_aggregates_vs_total(state_dir):
    now = 1_000_000.0  # day_start = now - now%86400
    day_start = now - (now % 86400)
    _write_log(state_dir, [
        _rec(host="codex", saved=0.02, ts=day_start + 100),   # today
        _rec(host="codex", saved=0.03, ts=day_start + 200),   # today
        _rec(host="codex", saved=0.05, ts=day_start - 5000),  # yesterday
    ])
    s = ss.compute_status("codex", now=now)
    assert s.routed_count_session == 2
    assert s.saved_session == pytest.approx(0.05)
    assert s.saved_total == pytest.approx(0.10)


# ── health axis ──────────────────────────────────────────────────────────────
def test_health_ok_with_api_key(state_dir, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # Fresh usage.json so the degraded-on-stale path doesn't trip.
    (state_dir / "usage.json").write_text("{}")
    import os
    os.utime(state_dir / "usage.json", (now, now))
    _write_log(state_dir, [_rec(now=now, ts=now - 30)])
    s = ss.compute_status("codex", now=now)
    assert s.health == ss.HEALTH_OK


def test_health_down_when_no_provider(state_dir):
    now = 1_000_000.0
    # An old ollama route (outside the active window) does not prove reachability.
    _write_log(state_dir, [_rec(model="ollama/x", ts=now - ss.ACTIVE_WINDOW_S - 1)])
    s = ss.compute_status("codex", now=now)
    assert s.health == ss.HEALTH_DOWN


def test_recent_local_model_counts_as_provider(state_dir):
    now = 1_000_000.0
    (state_dir / "usage.json").write_text("{}")
    import os
    os.utime(state_dir / "usage.json", (now, now))
    _write_log(state_dir, [_rec(model="ollama/x", ts=now - 30)])
    s = ss.compute_status("codex", now=now)
    assert s.health == ss.HEALTH_OK


def test_health_degraded_on_stale_usage(state_dir, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # usage.json missing → stale → degraded (provider available so not down).
    _write_log(state_dir, [_rec(now=now, ts=now - 30)])
    s = ss.compute_status("codex", now=now)
    assert s.health == ss.HEALTH_DEGRADED


def test_health_snapshot_override(state_dir):
    now = 1_000_000.0
    (state_dir / ss._HEALTH_SNAPSHOT).write_text(
        json.dumps({"status": "degraded", "reason": "ollama down", "checked_at": now - 10})
    )
    _write_log(state_dir, [_rec(now=now, ts=now - 30)])
    s = ss.compute_status("codex", now=now)
    assert s.health == ss.HEALTH_DEGRADED
    assert s.health_reason == "ollama down"


def test_stale_health_snapshot_ignored(state_dir, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    (state_dir / "usage.json").write_text("{}")
    import os
    os.utime(state_dir / "usage.json", (now, now))
    (state_dir / ss._HEALTH_SNAPSHOT).write_text(
        json.dumps({"status": "down", "reason": "old", "checked_at": now - 9999})
    )
    _write_log(state_dir, [_rec(now=now, ts=now - 30)])
    s = ss.compute_status("codex", now=now)
    assert s.health == ss.HEALTH_OK  # snapshot too old to count


# ── renderers ────────────────────────────────────────────────────────────────
def test_compact_line_with_route(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(saved=0.03, ts=now - 30)])
    s = ss.compute_status("codex", now=now)
    line = ss.compact_line(s, color=False)
    assert "⚡ llm_router" in line
    assert "hermes3:8b" in line
    assert "code/moderate" in line
    assert "$0.03 saved" in line


def test_compact_line_no_route_is_honest(state_dir):
    s = ss.compute_status("codex", now=1_000_000.0)
    line = ss.compact_line(s, color=False)
    assert "no route yet" in line
    assert "🎯" not in line  # must not imply a route happened


def test_compact_line_no_color_has_no_escapes(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(ts=now - 30)])
    line = ss.compact_line(ss.compute_status("codex", now=now), color=False)
    assert "\033" not in line


def test_terminal_title_is_osc(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(saved=0.03, ts=now - 30)])
    title = ss.terminal_title(ss.compute_status("codex", now=now))
    assert title.startswith("\033]2;")
    assert title.endswith("\007")
    assert "hermes3:8b" in title


# ── notifications (rate-limit + dedup) ───────────────────────────────────────
def test_route_notification_then_throttled(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(saved=0.03, ts=now - 30)])
    s = ss.compute_status("codex", now=now)
    first = ss.notification(s, "route", now=now)
    assert first is not None
    assert "hermes3:8b" in first["message"]
    # Immediately again → throttled.
    second = ss.notification(s, "route", now=now + 5)
    assert second is None
    # After the interval → fires again.
    third = ss.notification(s, "route", now=now + ss.ACTIVE_WINDOW_S + 1)
    assert third is not None


def test_health_notification_only_on_transition(state_dir):
    now = 1_000_000.0
    # No provider → down.
    _write_log(state_dir, [_rec(model="ollama/x", ts=now - ss.ACTIVE_WINDOW_S - 1)])
    s = ss.compute_status("codex", now=now)
    assert s.health == ss.HEALTH_DOWN
    first = ss.notification(s, "health", now=now)
    assert first is not None
    assert first["urgency"] == "critical"
    # Same state again → no repeat.
    second = ss.notification(s, "health", now=now + 10)
    assert second is None


def test_no_notification_when_healthy(state_dir, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    (state_dir / "usage.json").write_text("{}")
    import os
    os.utime(state_dir / "usage.json", (now, now))
    _write_log(state_dir, [_rec(ts=now - 30)])
    s = ss.compute_status("codex", now=now)
    assert ss.notification(s, "health", now=now) is None


# ── host aliasing ────────────────────────────────────────────────────────────
def test_host_alias_hyphen_underscore(state_dir):
    now = 1_000_000.0
    # Record stamped with the hyphen variant; query the canonical underscore key.
    _write_log(state_dir, [_rec(host="gemini-cli", model="ollama/g", ts=now - 30)])
    s = ss.compute_status("gemini_cli", now=now)
    assert s.short_model() == "g"
    assert s.active is True


def test_claude_code_absorbs_subagent(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(host="claude_code_subagent", model="ollama/s", ts=now - 30)])
    s = ss.compute_status("claude_code", now=now)
    assert s.short_model() == "s"


# ── emit_indicator (channel writes, no real notifications) ───────────────────
def test_emit_indicator_writes_line_and_title(state_dir):
    import io
    now = 1_000_000.0
    _write_log(state_dir, [_rec(saved=0.03, ts=now - 30)])
    buf = io.StringIO()
    s = ss.emit_indicator("codex", now=now, notify=False, stream=buf)
    out = buf.getvalue()
    assert "\033]2;" in out          # terminal title OSC
    assert "hermes3:8b" in out       # inline line
    assert s.last_model == "ollama/hermes3:8b"


def test_emit_indicator_respects_off_switch(state_dir, monkeypatch):
    import io
    now = 1_000_000.0
    monkeypatch.setenv("LLM_ROUTER_INDICATOR", "off")
    _write_log(state_dir, [_rec(ts=now - 30)])
    buf = io.StringIO()
    ss.emit_indicator("codex", now=now, notify=False, stream=buf)
    assert buf.getvalue() == ""      # disabled → nothing written


# ── token amounts (#token-indicators) ────────────────────────────────────────
def test_fmt_tokens():
    assert ss.fmt_tokens(0) == ""
    assert ss.fmt_tokens(None) == ""
    assert ss.fmt_tokens(940) == "940 tok"
    assert ss.fmt_tokens(1250) == "1.2k tok"


def test_compute_status_last_and_session_tokens(state_dir):
    now = 1_000_000.0
    day_start = now - (now % 86400)
    _write_log(state_dir, [
        _rec(in_tok=100, out_tok=50, ts=day_start + 10),
        _rec(in_tok=300, out_tok=200, ts=day_start + 20),   # last route
    ])
    s = ss.compute_status("codex", now=now)
    assert s.last_tokens == 500          # 300 + 200, the most recent route
    assert s.tokens_session == 650       # (100+50) + (300+200) today


def test_compact_line_shows_tokens(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(in_tok=900, out_tok=350, ts=now - 30)])
    line = ss.compact_line(ss.compute_status("codex", now=now), color=False)
    assert "1.2k tok" in line


def test_compact_line_omits_tokens_when_zero(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(in_tok=0, out_tok=0, ts=now - 30)])
    line = ss.compact_line(ss.compute_status("codex", now=now), color=False)
    assert "tok" not in line


def test_terminal_title_shows_tokens(state_dir):
    now = 1_000_000.0
    _write_log(state_dir, [_rec(in_tok=500, out_tok=500, ts=now - 30)])
    title = ss.terminal_title(ss.compute_status("codex", now=now))
    assert "1.0k tok" in title
