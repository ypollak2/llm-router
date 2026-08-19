"""External/gateway traffic must show in the host-tagged savings pipeline.

Part A: route_payload appends a host-tagged savings_log record (so gateway /
LoopHole traffic reaches savings_stats + the indicators) without touching the
current session's session_spend ledger.
Part B: surface_status reads the DURABLE savings_stats table, so the indicators
survive savings_log.jsonl truncation and capture gateway traffic.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from llm_router.observability import surface_status as ss


# ── Part A ───────────────────────────────────────────────────────────────────
def _fake_resp():
    return SimpleNamespace(content="ok", model="ollama/hermes3:8b", provider="ollama",
                           cost_usd=0.0, input_tokens=100, output_tokens=50, complexity="simple")


def test_route_payload_writes_host_tagged_savings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".llm-router").mkdir(parents=True)

    import llm_router.router as R

    async def _fake(task_type, prompt, **kw):
        return _fake_resp()

    monkeypatch.setattr(R, "route_and_call", _fake)

    from llm_router import route_server
    out = route_server.route_payload({"prompt": "hi", "task_type": "query", "complexity": "simple"})
    assert out["model"] == "ollama/hermes3:8b"

    lines = (tmp_path / ".llm-router" / "savings_log.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["host"] == "gateway"           # default host tag
    assert rec["input_tokens"] == 100 and rec["output_tokens"] == 50
    assert rec["estimated_saved"] >= 0.0
    assert rec["model"] == "ollama/hermes3:8b"


def test_route_payload_honors_host_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".llm-router").mkdir(parents=True)
    import llm_router.router as R
    monkeypatch.setattr(R, "route_and_call", lambda *a, **k: _fake_resp())

    async def _fake(task_type, prompt, **kw):
        return _fake_resp()
    monkeypatch.setattr(R, "route_and_call", _fake)

    from llm_router import route_server
    route_server.route_payload({"prompt": "hi", "host": "loophole"})
    rec = json.loads((tmp_path / ".llm-router" / "savings_log.jsonl").read_text().strip())
    assert rec["host"] == "loophole"


def test_route_payload_treats_auto_model_as_no_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".llm-router").mkdir(parents=True)
    import llm_router.router as R

    seen = {}

    async def _fake(task_type, prompt, **kw):
        seen.update(kw)
        return _fake_resp()

    monkeypatch.setattr(R, "route_and_call", _fake)

    from llm_router import route_server
    route_server.route_payload({"prompt": "hi", "model": "auto"})
    assert seen["model_override"] is None


def test_route_payload_does_not_touch_session_spend(tmp_path, monkeypatch):
    # External traffic must NOT pollute the current session's session_spend.json.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".llm-router").mkdir(parents=True)
    import llm_router.router as R

    async def _fake(task_type, prompt, **kw):
        return _fake_resp()
    monkeypatch.setattr(R, "route_and_call", _fake)

    from llm_router import route_server
    route_server.route_payload({"prompt": "hi"})
    assert not (tmp_path / ".llm-router" / "session_spend.json").exists()


# ── Part B ───────────────────────────────────────────────────────────────────
def _seed_stats(state_dir, ts_iso, host, model, task, saved, in_tok, out_tok):
    conn = sqlite3.connect(state_dir / "usage.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS savings_stats ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, session_id TEXT, "
        "task_type TEXT, estimated_claude_cost_saved REAL, external_cost REAL, "
        "model_used TEXT, host TEXT, input_tokens INTEGER, output_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO savings_stats (timestamp, host, model_used, task_type, "
        "estimated_claude_cost_saved, input_tokens, output_tokens) VALUES (?,?,?,?,?,?,?)",
        (ts_iso, host, model, task, saved, in_tok, out_tok),
    )
    conn.commit()
    conn.close()


def test_surface_status_reads_durable_savings_stats(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_STATE_DIR", str(tmp_path))
    for k in ss._PROVIDER_KEYS:
        monkeypatch.delenv(k, raising=False)
    now = 1_000_000.0
    ts = datetime.fromtimestamp(now - 30, tz=timezone.utc).isoformat()
    # savings_stats has a gateway route; NO savings_log.jsonl at all.
    _seed_stats(tmp_path, ts, "gateway", "ollama/hermes3:8b", "query", 0.02, 300, 200)

    s = ss.compute_status("gateway", now=now)
    assert s.last_model == "ollama/hermes3:8b"   # durable read worked
    assert s.last_tokens == 500
    assert s.active is True
    assert s.saved_total == pytest.approx(0.02)


def test_stats_and_log_combine_by_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_STATE_DIR", str(tmp_path))
    for k in ss._PROVIDER_KEYS:
        monkeypatch.delenv(k, raising=False)
    now = 1_000_000.0
    # older route in durable stats, newer route in the live jsonl buffer
    _seed_stats(tmp_path, datetime.fromtimestamp(now - 300, tz=timezone.utc).isoformat(),
                "gateway", "ollama/old", "query", 0.01, 100, 50)
    (tmp_path / ss._SAVINGS_LOG).write_text(json.dumps({
        "timestamp": datetime.fromtimestamp(now - 10, tz=timezone.utc).isoformat(),
        "host": "gateway", "model": "ollama/new", "task_type": "query",
        "complexity": "simple", "estimated_saved": 0.03, "input_tokens": 200, "output_tokens": 100,
    }) + "\n")
    s = ss.compute_status("gateway", now=now)
    assert s.short_model() == "new"              # newest across BOTH sources
    assert s.routed_count_session == 2           # both counted
