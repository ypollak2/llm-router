"""RETROSPECTIVE B-2 (retro correction) — subagent routing IS credited.

The retro claimed subagents "can't be routed, only gated." That is false: the
DIRECT/CLI subagent paths route work onto cheap models and log savings via
``savings_logger.log_direct_savings(..., host=...)``. This test proves a routed
subagent produces a credited savings record attributed to a subagent host — so
the capability exists and is measured (the open question is only whether it
*fires*, not whether it's possible).
"""
from __future__ import annotations

import json

from llm_router.hooks import savings_logger as sl


class _Model:
    provider = "ollama"
    model = "qwen3-coder:30b"


class _DirectResult:
    """Duck-typed DirectResult: the fields log_direct_savings reads."""
    model = _Model()
    input_tokens = 1000
    output_tokens = 2000


def test_subagent_direct_routing_writes_credited_savings(tmp_path, monkeypatch):
    log_path = tmp_path / "savings_log.jsonl"
    monkeypatch.setattr(sl, "_savings_log_path", lambda: log_path)
    # Isolate the session-spend mirror so it can't touch the real ~/.llm-router.
    from llm_router import session_spend as ss
    monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")

    sl.log_direct_savings(
        _DirectResult(),
        task_type="code",
        complexity="complex",
        session_id="sess-subagent",
        host="claude_code_subagent",
    )

    assert log_path.exists(), "subagent routing must produce a savings record"
    rec = json.loads(log_path.read_text().strip().splitlines()[-1])
    # Attributed to the subagent host, on a cheap model, with real credited savings.
    assert rec["host"] == "claude_code_subagent"
    assert rec["model"] == "ollama/qwen3-coder:30b"
    # Opus baseline for 1000/2000 tokens ($5/$25) minus $0 free-local cost.
    assert rec["estimated_saved"] > 0.0
    assert rec["external_cost"] == 0.0


def test_log_direct_savings_accepts_host_param():
    # Signature-level guarantee that subagent hosts are a first-class attribution.
    import inspect

    sig = inspect.signature(sl.log_direct_savings)
    assert "host" in sig.parameters
