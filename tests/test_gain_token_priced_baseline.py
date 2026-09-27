"""2026-09-27: `llm-router gain`'s Opus baseline was ALWAYS $0.00 for a
free/local route.

`SavingsAnalytics.estimate_opus_cost(selected_model, estimated_cost)` priced
the baseline by multiplying `estimated_cost` — the ACTUAL dollar cost — by a
per-model multiplier. For a free/local route (Ollama, etc.) that cost is
correctly `$0.00`, and `0 * anything == 0`: on a real machine, 23 Ollama calls
all showed "Actual $0 / Opus $0" — not because routing saved nothing, but
because the call was never priced against the baseline at all.

The fix prices the actual TOKEN VOLUME against
`pricing.savings_baseline_model()` (the same baseline every other savings
surface uses), and returns `None` — not `0.0` — when there is truly nothing
to price, so a caller can render "n/a" instead of a fabricated $0.00 that
reads as "confirmed no saving".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from llm_router import pricing
from llm_router.commands.gain import SavingsAnalytics


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_routing_decisions(path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE routing_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        task_type TEXT,
        complexity TEXT,
        budget_pct_used REAL,
        final_model TEXT,
        cost_usd REAL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        session_id TEXT
    )""")
    for model, cost, in_tok, out_tok in rows:
        conn.execute(
            "INSERT INTO routing_decisions (timestamp, task_type, complexity, "
            "budget_pct_used, final_model, cost_usd, input_tokens, output_tokens, "
            "session_id) VALUES (?, 'code', 'moderate', 0, ?, ?, ?, ?, 's1')",
            (_now_iso(), model, cost, in_tok, out_tok),
        )
    conn.commit()
    conn.close()


def test_free_route_with_tokens_prices_a_nonzero_baseline():
    """The exact real-machine shape: cost_usd == 0.0 (free/local), but real
    token volume. Before the fix this was ALWAYS $0.00 -- `0 * multiplier`."""
    analytics = SavingsAnalytics()
    opus_cost = analytics.estimate_opus_cost(
        "ollama/qwen3-coder:30b", estimated_cost=0.0,
        input_tokens=10_000, output_tokens=2_000,
    )
    assert opus_cost is not None
    assert opus_cost > 0.0
    in_rate, out_rate = pricing.savings_baseline_rates()
    expected = (10_000 * in_rate + 2_000 * out_rate) / 1_000_000
    assert opus_cost == pytest.approx(expected)


def test_free_route_with_no_tokens_and_no_cost_is_unpriceable_not_zero():
    """No signal at all to price against -- must be None (renders "n/a"),
    never a fabricated $0.00 that reads as "confirmed no saving"."""
    analytics = SavingsAnalytics()
    assert analytics.estimate_opus_cost("ollama/x", 0.0, 0, 0) is None


def test_paid_route_with_no_tokens_falls_back_to_the_cost_multiplier():
    """A paid API call whose usage wasn't logged: no tokens, but a real
    nonzero cost -- the old multiplier heuristic is still the best available
    signal, kept for this narrow case only."""
    analytics = SavingsAnalytics()
    opus_cost = analytics.estimate_opus_cost("gpt-4o-mini", 0.01, 0, 0)
    assert opus_cost == pytest.approx(0.01 * 8)


def test_compute_savings_prices_every_free_decision_and_flags_none_as_unpriced(tmp_path):
    db = tmp_path / "usage.db"
    _seed_routing_decisions(db, rows=[
        ("ollama/qwen3-coder:30b", 0.0, 10_000, 2_000),  # priceable from tokens
        ("ollama/qwen3.5:latest", 0.0, 0, 0),             # genuinely unpriceable
    ])
    analytics = SavingsAnalytics(db_path=db)
    result = analytics.compute_savings(days=1)

    assert result["total_decisions"] == 2
    assert result["unpriced_decisions"] == 1
    # Root cause fixed: the priceable free row must contribute a NONZERO
    # baseline -- pre-fix this was 0.0 for every free row, priced or not.
    assert result["total_opus_cost_usd"] > 0.0

    by_model = result["by_model"]
    assert by_model["ollama/qwen3-coder:30b"]["opus_cost"] > 0.0
    assert by_model["ollama/qwen3-coder:30b"]["unpriced"] == 0
    assert by_model["ollama/qwen3.5:latest"]["unpriced"] == 1


def test_format_savings_renders_n_a_for_a_fully_unpriced_bucket(tmp_path):
    """The COST BREAKDOWN table must never print a bucket's Opus/Saved cells
    as "$0.0000" when every decision inside it was unpriceable -- that reads
    as a confirmed zero saving instead of "we don't know"."""
    db = tmp_path / "usage.db"
    _seed_routing_decisions(db, rows=[("ollama/x", 0.0, 0, 0)])
    analytics = SavingsAnalytics(db_path=db)
    result = analytics.compute_savings(days=1)
    rendered = analytics.format_savings(result, period_days=1)
    assert "n/a" in rendered
