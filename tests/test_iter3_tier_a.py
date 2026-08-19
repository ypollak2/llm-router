"""Regression: iteration-3 Tier-A findings.

- RED2-3-01: cap-downgrade render must describe the REAL provider (Claude when
  smart-fallthrough routed to paid Claude, not "free-local").
- RED2-3-03: gemini_cli must be in quota_savings' free-local bucket.
- RED1-3-05: execution_ledger._load_rows must be deterministically ordered.
(RED2-3-02 claims guard is covered by test_claims_no_fabricated_magnitudes.py.)
"""
from __future__ import annotations

from llm_router.types import LLMResponse


def _resp(provider, model, **kw):
    base = dict(content="x", model=model, input_tokens=10, output_tokens=5,
                cost_usd=0.0, latency_ms=12.0, provider=provider,
                cap_downgraded=True, cap_downgrade_reason="Daily cap exceeded")
    base.update(kw)
    return LLMResponse(**base)


def test_render_free_local_downgrade():
    h = _resp("ollama", "ollama/qwen2.5:7b").header()
    assert "free" in h.lower() and "claude" not in h.lower()


def test_render_claude_fallthrough_is_honest():
    # RED2-3-01: smart-fallthrough to paid Claude must NOT say "free-local".
    r = _resp("anthropic", "anthropic/claude-sonnet-4-6", cost_usd=0.02)
    h = r.header()
    assert "free" not in h.lower(), f"header wrongly claims free-local for paid Claude: {h!r}"
    assert "claude" in h.lower(), f"header should name Claude: {h!r}"


def test_gemini_cli_in_free_local_bucket():
    from llm_router import quota_savings
    assert "gemini_cli" in quota_savings._FREE_LOCAL_PROVIDERS


def test_load_rows_has_deterministic_order():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "llm_router" / "execution_ledger.py").read_text()
    # The _load_rows query must ORDER BY (deterministic aggregation).
    assert "ORDER BY ts" in src, "RED1-3-05: _load_rows must ORDER BY for deterministic merge"
