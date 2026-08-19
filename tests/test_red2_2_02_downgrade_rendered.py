"""Regression: RED2-2-02 — a cap downgrade must be VISIBLE in user-facing output.

P-OBSERV set LLMResponse.cap_downgraded but summary()/header() (the actual
CLI/MCP strings a customer sees) never rendered it, so a cap-forced downgrade was
still an unexplained quality drop. summary()/header() now show it.
"""
from __future__ import annotations

from llm_router.types import LLMResponse


def _resp(**kw):
    base = dict(content="ok", model="ollama/qwen2.5:7b", input_tokens=10, output_tokens=5,
                cost_usd=0.0, latency_ms=12.0, provider="ollama")
    base.update(kw)
    return LLMResponse(**base)


def test_summary_shows_downgrade():
    r = _resp(cap_downgraded=True, cap_downgrade_reason="Daily spend limit exceeded")
    s = r.summary()
    assert "cap" in s.lower(), f"summary must surface the cap downgrade: {s!r}"


def test_header_shows_downgrade():
    r = _resp(cap_downgraded=True, cap_downgrade_reason="Daily spend limit exceeded")
    h = r.header()
    assert "cap" in h.lower(), f"header must surface the cap downgrade: {h!r}"


def test_normal_response_has_no_downgrade_marker():
    r = _resp()
    assert "cap" not in r.summary().lower()
    assert "cap" not in r.header().lower()


def test_cap_messages_are_local_not_utc():
    """Q-MSG: the daily-cap error text must not claim UTC when the boundary is local."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "llm_router" / "router.py").read_text()
    assert "today UTC" not in src and "midnight UTC" not in src, (
        "cap message still claims UTC but the query uses localtime"
    )
