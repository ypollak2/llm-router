"""Regression tests for DIRECT routing savings persistence.

Bug background (pre-v9.4.0):
    auto-route.py routes prompts to cheap models (Ollama/Gemini/OpenAI) via
    direct_executor.execute_chain. When a DIRECT call succeeded, the model
    answered the prompt for ~free, but no savings record was persisted.
    Result: session-end.py's savings query showed $0.00 saved for any session
    that relied entirely on DIRECT routing (the common case once Ollama is
    configured).

Fix:
    A new module llm_router.hooks.savings_logger appends a JSONL record to
    ~/.llm-router/savings_log.jsonl after each successful DIRECT result.
    session-end.py's existing _sync_import_savings_log() then flushes those
    records into the savings_stats table on session end.
"""

from __future__ import annotations

import json

import pytest

from llm_router.hooks.direct_executor import DirectResult, ModelSpec


@pytest.fixture
def savings_log_path(temp_router_dir):
    return temp_router_dir / "savings_log.jsonl"


def _ollama_result(input_tokens: int = 100, output_tokens: int = 50) -> DirectResult:
    return DirectResult(
        text="some answer",
        model=ModelSpec(provider="ollama", model="qwen3.5:latest"),
        latency_ms=6500,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def test_log_direct_savings_creates_jsonl(savings_log_path):
    """A successful DIRECT call must create savings_log.jsonl with one record."""
    from llm_router.hooks.savings_logger import log_direct_savings

    log_direct_savings(
        result=_ollama_result(),
        task_type="query",
        complexity="simple",
        session_id="test-session-001",
    )

    assert savings_log_path.exists(), "savings_log.jsonl must be created"
    lines = savings_log_path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_record_schema_matches_session_end_flush(savings_log_path):
    """Record keys must match what session-end._sync_import_savings_log expects."""
    from llm_router.hooks.savings_logger import log_direct_savings

    log_direct_savings(
        result=_ollama_result(),
        task_type="code",
        complexity="moderate",
        session_id="abc",
    )

    record = json.loads(savings_log_path.read_text().strip())
    required_keys = {
        "timestamp",
        "session_id",
        "task_type",
        "estimated_saved",
        "external_cost",
        "model",
        "host",
    }
    assert required_keys.issubset(record.keys()), (
        f"missing keys {required_keys - set(record.keys())}"
    )


def test_ollama_has_zero_external_cost_and_positive_savings(savings_log_path):
    """Ollama is free → external_cost=0.0 and estimated_saved > 0 vs Claude baseline."""
    from llm_router.hooks.savings_logger import log_direct_savings

    log_direct_savings(
        result=_ollama_result(input_tokens=1000, output_tokens=500),
        task_type="query",
        complexity="moderate",
        session_id="s",
    )

    record = json.loads(savings_log_path.read_text().strip())
    assert record["external_cost"] == 0.0
    assert record["estimated_saved"] > 0.0
    assert record["model"] == "ollama/qwen3.5:latest"
    assert record["host"] == "claude_code"


def test_multiple_calls_append_not_overwrite(savings_log_path):
    """Repeated calls must append, not truncate, the JSONL file."""
    from llm_router.hooks.savings_logger import log_direct_savings

    for i in range(3):
        log_direct_savings(
            result=_ollama_result(),
            task_type="query",
            complexity="simple",
            session_id=f"sess-{i}",
        )

    lines = savings_log_path.read_text().strip().splitlines()
    assert len(lines) == 3
    session_ids = {json.loads(line)["session_id"] for line in lines}
    assert session_ids == {"sess-0", "sess-1", "sess-2"}


def test_paid_provider_subtracts_external_cost_from_savings(savings_log_path):
    """For a paid provider (Gemini Flash), estimated_saved = baseline − external_cost."""
    from llm_router.hooks.savings_logger import log_direct_savings

    gemini = DirectResult(
        text="x",
        model=ModelSpec(provider="gemini", model="gemini-2.5-flash"),
        latency_ms=500,
        input_tokens=1000,
        output_tokens=500,
    )
    log_direct_savings(
        result=gemini,
        task_type="generate",
        complexity="moderate",
        session_id="s",
    )

    record = json.loads(savings_log_path.read_text().strip())
    assert record["external_cost"] > 0.0, "Gemini Flash is metered, not free"
    assert record["estimated_saved"] > 0.0, "still cheaper than Sonnet baseline"


def test_failure_is_silent(temp_router_dir):
    """Filesystem errors must not propagate and break the calling hook."""
    from llm_router.hooks.savings_logger import log_direct_savings

    # Make the savings_log path a directory so open(..., 'a') will fail
    bad = temp_router_dir / "savings_log.jsonl"
    bad.mkdir()

    # Must not raise
    log_direct_savings(
        result=_ollama_result(),
        task_type="query",
        complexity="simple",
        session_id="abc",
    )


def test_unknown_provider_returns_safe_record(savings_log_path):
    """An unfamiliar provider/model must still produce a record (don't crash)."""
    from llm_router.hooks.savings_logger import log_direct_savings

    weird = DirectResult(
        text="x",
        model=ModelSpec(provider="custom-host", model="some-weird-model"),
        latency_ms=100,
        input_tokens=100,
        output_tokens=50,
    )
    log_direct_savings(
        result=weird,
        task_type="query",
        complexity="simple",
        session_id="s",
    )

    record = json.loads(savings_log_path.read_text().strip())
    assert record["model"] == "custom-host/some-weird-model"
    assert "external_cost" in record  # numeric, defaulted
    assert "estimated_saved" in record


def test_records_use_opus_baseline_for_all_complexities(savings_log_path):
    """2026-07-12 (user decision): savings are opus-equivalent ONLY — the
    baseline is claude-opus-4-8 regardless of complexity, so identical token
    counts must yield identical estimated_saved across all tiers. (The old
    tiered haiku/sonnet/opus 'honest' baseline was dropped.)"""
    from llm_router.hooks.savings_logger import log_direct_savings

    for complexity in ("simple", "moderate", "complex"):
        log_direct_savings(
            result=_ollama_result(input_tokens=1000, output_tokens=500),
            task_type="query",
            complexity=complexity,
            session_id=complexity,
        )

    lines = savings_log_path.read_text().strip().splitlines()
    records = {json.loads(line)["session_id"]: json.loads(line) for line in lines}
    assert (
        records["simple"]["estimated_saved"]
        == records["moderate"]["estimated_saved"]
        == records["complex"]["estimated_saved"]
    )
    # opus-4-8 @ $5/$25 per 1M: 1000 in + 500 out on a free local model
    # → baseline (and savings) = 0.005 + 0.0125 = $0.0175
    assert abs(records["simple"]["estimated_saved"] - 0.0175) < 1e-9


# ── DIRECT → usage / routing_decisions table persistence ─────────────────────
# Bug: the DIRECT (hook) path only appended to savings_log.jsonl, so the
# `usage` and `routing_decisions` tables — which the routing view / summary
# read from — stayed frozen whenever the hook answered prompts inline. The
# fix wires log_direct_to_db() into the DIRECT success handler so DIRECT-routed
# turns are visible everywhere the MCP-tool path is.


def test_log_direct_to_db_writes_usage_table(temp_db):
    """A successful DIRECT call must insert one row into the usage table."""
    import sqlite3

    from llm_router.hooks.savings_logger import log_direct_to_db

    log_direct_to_db(
        _ollama_result(input_tokens=512, output_tokens=88),
        prompt="check the backfill log",
        task_type="research",
        complexity="moderate",
        classifier_type="heuristic",
    )

    rows = sqlite3.connect(str(temp_db)).execute(
        "SELECT model, provider, task_type, input_tokens, output_tokens, cost_usd FROM usage"
    ).fetchall()
    assert len(rows) == 1
    model, provider, task_type, in_tok, out_tok, cost = rows[0]
    assert provider == "ollama"
    assert task_type == "research"
    assert (in_tok, out_tok) == (512, 88)
    assert cost == 0.0  # local provider is free


def test_log_direct_to_db_writes_routing_decisions_table(temp_db):
    """A successful DIRECT call must insert one row into routing_decisions,
    tagged reason_code='direct' so it's distinguishable from MCP-path rows."""
    import sqlite3

    from llm_router.hooks.savings_logger import log_direct_to_db

    log_direct_to_db(
        _ollama_result(),
        prompt="hello",
        task_type="query",
        complexity="simple",
        classifier_type="heuristic",
    )

    rows = sqlite3.connect(str(temp_db)).execute(
        "SELECT task_type, final_provider, final_model, reason_code FROM routing_decisions"
    ).fetchall()
    assert len(rows) == 1
    task_type, provider, model, reason = rows[0]
    assert task_type == "query"
    assert provider == "ollama"
    assert reason == "direct"


def test_log_direct_to_db_never_raises_on_bad_task_type(temp_db):
    """Unknown task_type / profile strings must fall back, not crash the hook."""
    from llm_router.hooks.savings_logger import log_direct_to_db

    # Should not raise even with a nonsense task_type.
    log_direct_to_db(
        _ollama_result(),
        prompt="x",
        task_type="not-a-real-task-type",
        complexity="moderate",
        profile="not-a-real-profile",
    )
