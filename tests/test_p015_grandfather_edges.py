"""P0.15-2: first-claim inventory and hook-only first-run history."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import config, discover, failopen
from llm_router import model_discovery as md


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".llm-router"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.delenv("LLM_ROUTER_PROMOTE_GATE", raising=False)
    monkeypatch.setattr(md, "_grandfather_cache", None)
    monkeypatch.setattr(
        config, "get_config",
        lambda: SimpleNamespace(all_openai_compat_models=lambda: []),
    )
    return home


def claimed_ids(state):
    return json.loads((state / "grandfathered_models.json").read_text())["ids"]


def write_upgrade_cache(state):
    (state / "discovery.json").write_text(json.dumps({
        "cached_at": time.time() - 3600,
        "models": {"ollama/a:7b": {"provider": "ollama"}},
    }))


def write_usage(state, provider):
    # Use the hook's actual schema builder, without executing the hook entrypoint.
    path = Path(__file__).resolve().parents[1] / "hooks" / "cc-usage-track.py"
    spec = importlib.util.spec_from_file_location("p015_cc_usage_track", path)
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    with sqlite3.connect(state / "usage.db") as db:
        hook._ensure_table(db)
        db.execute(
            "INSERT INTO usage (model, provider, reason) VALUES (?, ?, ?)",
            ("a:7b", provider,
             "claude_code_subscription" if provider == "cc" else "routed"),
        )


@pytest.mark.parametrize("compat_id", ["lmstudio-x", "openai_compat/lmstudio-x"])
def test_discover_first_claim_includes_compat(state, monkeypatch, compat_id):
    write_upgrade_cache(state)
    monkeypatch.setattr(
        config, "get_config",
        lambda: SimpleNamespace(all_openai_compat_models=lambda: [compat_id]),
    )
    discover._update_discovery_cache([{"name": "a:7b"}])
    assert claimed_ids(state) == ["ollama/a:7b", "openai_compat/lmstudio-x"]


def test_cc_only_usage_does_not_grandfather_a_later_pull(state):
    write_usage(state, "cc")
    assert not md._usage_db_has_history(state / "usage.db")
    assert md.order_for_chain([], "query", critical=False) == ([], [])
    md.ensure_grandfather(["ollama/justpulled:7b"])
    assert claimed_ids(state) == []


def test_empty_first_chain_writes_claim_marker(state):
    assert md.order_for_chain([], "query", critical=False) == ([], [])
    assert (state / "grandfathered_models.init").is_file()
    assert claimed_ids(state) == []


@pytest.mark.parametrize("provider", ["ollama", "openai"])
def test_real_usage_still_establishes_upgrade(state, provider):
    write_usage(state, provider)
    assert md._usage_db_has_history(state / "usage.db")
    md.ensure_grandfather(["ollama/a:7b"])
    assert claimed_ids(state) == ["ollama/a:7b"]


@pytest.mark.parametrize("reason,history", [
    ("direct", False), ("routed", True), (None, True),
])
def test_routing_decision_history_excludes_direct_hook(state, reason, history):
    with sqlite3.connect(state / "usage.db") as db:
        db.execute("CREATE TABLE routing_decisions (reason_code TEXT)")
        db.execute("INSERT INTO routing_decisions VALUES (?)", (reason,))
    assert md._usage_db_has_history(state / "usage.db") is history
    md.ensure_grandfather(["ollama/a:7b"])
    assert claimed_ids(state) == (["ollama/a:7b"] if history else [])


def test_compat_lookup_failure_records_and_preserves_ollama_claim(state, monkeypatch):
    write_upgrade_cache(state)
    records = []

    def broken_config():
        raise RuntimeError("compat config unavailable")

    monkeypatch.setattr(config, "get_config", broken_config)
    monkeypatch.setattr(failopen, "record", lambda code, exc: records.append((code, exc)))
    md.ensure_grandfather(["ollama/a:7b"])
    assert claimed_ids(state) == ["ollama/a:7b"]
    assert records[0][0] == "CHZ-FO-PROMOTE-GATE-GRANDFATHER-COMPAT"
