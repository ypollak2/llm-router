"""Non-functional pillar — does LLM Router degrade gracefully under stress?

Every public-API method must have at least one negative test exercising
the realistic failure modes: corrupt input, missing parent directories,
file-system errors, terminal-state mutations, budget breaches,
incompatible data shapes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router.agents import (
    AgentRegistry,
    BudgetEnvelope,
    BudgetExceeded,
    SessionStore,
)
from llm_router.agents.registry import AgentNotFound
from llm_router.agents.session import SessionNotFound, TerminalStateViolation
from llm_router.lineage import LineageStore, make_record
from llm_router.signals.pii import PiiSignal

from tests.qa.conftest import HostSpec, load_adapter


# ────────────────────────────────────────────────────────────────────────
# Adapter resilience: corrupt configs, missing parents
# ────────────────────────────────────────────────────────────────────────

def test_adapter_recovers_from_corrupt_json(host: HostSpec, tmp_path: Path):
    cls = load_adapter(host)
    cfg = tmp_path / "broken.json"
    cfg.write_text("not valid json {{{{ ((( ")
    adapter = cls(config_path=cfg)
    # Install must overwrite — not raise
    adapter.install(server_command=["llm-router"])
    data = json.loads(cfg.read_text())
    assert "llm_router" in data["mcpServers"]


def test_adapter_creates_missing_parent_dir(host: HostSpec, tmp_path: Path):
    cls = load_adapter(host)
    deep = tmp_path / "nested" / "deeper" / "config.json"
    adapter = cls(config_path=deep)
    adapter.install(server_command=["llm-router"])
    assert deep.exists()


def test_adapter_uninstall_on_missing_config_is_noop(host: HostSpec, tmp_path: Path):
    cls = load_adapter(host)
    adapter = cls(config_path=tmp_path / "never_existed.json")
    result = adapter.uninstall()
    assert result is None  # documented contract


def test_adapter_is_installed_on_empty_file_returns_false(host: HostSpec, tmp_path: Path):
    cls = load_adapter(host)
    cfg = tmp_path / "empty.json"
    cfg.write_text("")
    adapter = cls(config_path=cfg)
    assert not adapter.is_installed()


def test_adapter_handles_pre_existing_llm_router_entry(host: HostSpec, tmp_path: Path):
    """Install over an existing llm_router entry must overwrite cleanly."""
    cls = load_adapter(host)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "llm_router": {"command": "old-command", "args": ["--legacy"]},
        }
    }))
    adapter = cls(config_path=cfg)
    adapter.install(server_command=["llm-router", "--new"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["llm_router"]["command"] == "llm-router"
    assert data["mcpServers"]["llm_router"]["args"] == ["--new"]


# ────────────────────────────────────────────────────────────────────────
# Lineage store: invalid input + recovery
# ────────────────────────────────────────────────────────────────────────

def test_lineage_recent_on_empty_db(tmp_path: Path):
    store = LineageStore(db_path=tmp_path / "lineage.db")
    assert store.recent() == []


def test_lineage_inversions_on_empty_db(tmp_path: Path):
    store = LineageStore(db_path=tmp_path / "lineage.db")
    assert store.inversions() == []


def test_lineage_summary_on_empty_db(tmp_path: Path):
    store = LineageStore(db_path=tmp_path / "lineage.db")
    summary = store.summary()
    assert summary["total_decisions"] == 0
    assert summary["inversion_rate"] == 0.0


def test_lineage_handles_concurrent_open_via_separate_processes(tmp_path: Path):
    """SQLite supports multiple connections to the same DB. Open + use."""
    db = tmp_path / "lineage.db"
    s1 = LineageStore(db_path=db)
    s2 = LineageStore(db_path=db)
    rec = make_record(
        host="test", prompt_fingerprint="f", task_type="query",
        complexity="simple", classifier_method="heuristic",
        signal_scores={}, fired_decisions=(), chain_attempted=("m",),
        model_chosen="ollama/qwen3.5:latest", outcome="success",
        latency_ms=10, cost_usd=0.0,
    )
    s1.record(rec)
    # second store sees the row
    assert len(s2.recent()) == 1


# ────────────────────────────────────────────────────────────────────────
# Session store: terminal-state violations + recovery
# ────────────────────────────────────────────────────────────────────────

def test_session_store_get_unknown_raises_specific_exception(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    with pytest.raises(SessionNotFound):
        store.get("nonexistent")


def test_session_store_record_step_unknown_raises(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    with pytest.raises(SessionNotFound):
        store.record_step("nonexistent", cost_usd=0.01)


def test_session_store_complete_unknown_raises(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    with pytest.raises(SessionNotFound):
        store.complete("nonexistent")


def test_session_store_record_after_error_rejected(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    s = store.create(agent_id="x", budget_usd=1.0)
    store.error(s.session_id)
    with pytest.raises(TerminalStateViolation):
        store.record_step(s.session_id, cost_usd=0.1)


def test_session_store_complete_after_budget_exceeded_rejected(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    s = store.create(agent_id="x", budget_usd=0.10)
    with pytest.raises(BudgetExceeded):
        store.record_step(s.session_id, cost_usd=0.20)
    # Session is now BUDGET_EXCEEDED — complete must reject
    with pytest.raises(TerminalStateViolation):
        store.complete(s.session_id)


def test_session_store_error_on_already_errored_is_idempotent(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    s = store.create(agent_id="x", budget_usd=1.0)
    store.error(s.session_id)
    # second error must NOT raise — idempotent
    store.error(s.session_id)


def test_session_envelope_reflects_current_consumed(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    s = store.create(agent_id="x", budget_usd=1.0)
    store.record_step(s.session_id, cost_usd=0.30)
    env = store.envelope(s.session_id)
    assert env.consumed_usd == pytest.approx(0.30)
    assert env.remaining_usd == pytest.approx(0.70)


# ────────────────────────────────────────────────────────────────────────
# Budget envelope: edge cases
# ────────────────────────────────────────────────────────────────────────

def test_envelope_consume_negative_raises():
    env = BudgetEnvelope(cap_usd=1.0)
    with pytest.raises(ValueError):
        env.consume(-0.1)


def test_envelope_consume_zero_is_noop():
    env = BudgetEnvelope(cap_usd=1.0, consumed_usd=0.3)
    env2 = env.consume(0.0)
    assert env2.consumed_usd == 0.3


def test_envelope_would_exceed_at_exact_cap():
    """Spending exactly to the cap is OK; one cent over is not."""
    env = BudgetEnvelope(cap_usd=1.0, consumed_usd=0.5)
    assert not env.would_exceed(0.5)  # exactly at cap
    assert env.would_exceed(0.51)


def test_budget_exceeded_carries_context():
    exc = BudgetExceeded("sid", cap_usd=1.0, consumed_usd=0.9, proposed_usd=0.5)
    msg = str(exc)
    assert "sid" in msg
    assert "1.00" in msg or "1.0000" in msg
    assert "0.5" in msg


# ────────────────────────────────────────────────────────────────────────
# Registry: malformed YAML, duplicate IDs, missing keys
# ────────────────────────────────────────────────────────────────────────

def test_registry_get_unknown_raises_specific_exception():
    reg = AgentRegistry.from_profiles([])
    with pytest.raises(AgentNotFound):
        reg.get("nonexistent")


def test_registry_yaml_missing_id_raises(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("agents:\n  - description: nameless\n")
    with pytest.raises(ValueError, match="missing required keys"):
        AgentRegistry.from_yaml(bad)


def test_registry_yaml_missing_description_raises(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("agents:\n  - id: anon\n")
    with pytest.raises(ValueError, match="missing required keys"):
        AgentRegistry.from_yaml(bad)


def test_registry_yaml_missing_agents_key_raises(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("other_root: []\n")
    with pytest.raises(ValueError, match="agents"):
        AgentRegistry.from_yaml(bad)


# ────────────────────────────────────────────────────────────────────────
# PII signal: never crashes on adversarial input
# ────────────────────────────────────────────────────────────────────────

def test_pii_signal_handles_empty_string():
    signal = PiiSignal()
    score = signal.evaluate("")
    assert score.score == 0.0


def test_pii_signal_handles_very_long_input():
    signal = PiiSignal()
    score = signal.evaluate("a" * 100_000)
    # Must not crash; result depends on whether anything matched
    assert 0.0 <= score.score <= 1.0


def test_pii_signal_handles_unicode_input():
    signal = PiiSignal()
    score = signal.evaluate("こんにちは 🔑 пароль")
    # Unicode prompt should be handled without crash
    assert 0.0 <= score.score <= 1.0


def test_pii_signal_handles_null_bytes():
    signal = PiiSignal()
    score = signal.evaluate("hello\x00world")
    assert 0.0 <= score.score <= 1.0
