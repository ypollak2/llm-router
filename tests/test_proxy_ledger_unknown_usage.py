"""#261 follow-up: a row whose usage is null (unknown) is excluded from the
`proxy stats` token accumulators and counted, never read as zero tokens."""

from __future__ import annotations

from llm_router.proxy import ledger

MODEL = "claude-sonnet-4-5"
USAGE = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 3000, "cache_creation_5m": 3000, "cache_creation_1h": 0}


def _row(usage, **kw):
    r = {"ts": 1.0, "session_id": "s", "decision": "forwarded", "requested_model": MODEL,
         "served_model": MODEL, "upstream_status": 200, "stop_reason": "end_turn", "usage": usage}
    r.update(kw)
    return r


def test_stats_excludes_unknown_usage_rows_from_tokens_and_counts_them():
    s = ledger.stats([_row(dict(USAGE)), _row(None, usage_unknown="truncated"), _row(None)])
    an = s["anthropic"]
    assert an["usage_unknown_calls"] == 2
    assert an["tokens"]["input_tokens"] == 10 and an["tokens"]["output_tokens"] == 100
    assert an["unpriced_calls"] == 2                      # unchanged: unknown was already unpriced
    assert "excludes 2 call(s) with unknown usage" in ledger.format_stats(s)


def test_stats_legacy_row_without_a_usage_key_is_not_unknown():
    s = ledger.stats([_row(dict(USAGE)), {k: v for k, v in _row(None).items() if k != "usage"}])
    assert s["anthropic"]["usage_unknown_calls"] == 0


def test_tier_stats_observed_write_skips_unknown_usage_switch_rows_and_counts_them():
    known = _row(dict(USAGE), tier_reason="policy", tier_switch=True, tier_switch_cost_usd=0.01)
    unknown = _row(None, tier_reason="policy", tier_switch=True, tier_switch_cost_usd=0.01,
                   usage_unknown="no_usage")
    t = ledger.tier_stats([known, unknown])
    assert t["switch_calls_usage_unknown"] == 1
    assert t["switch_calls_cache_write_usd"] == ledger.tier_stats([known])["switch_calls_cache_write_usd"] > 0
    assert "excluding 1 switched call(s) with unknown usage" in ledger.format_stats(ledger.stats([known, unknown]))


def test_cache_creation_medians_use_known_usage_rows_only_and_say_so():
    def cc(usage, mixed):
        return _row(usage, step_class="continuation", mixed_history=mixed)

    def known(w):
        return dict(USAGE, cache_creation_input_tokens=w, cache_creation_5m=w)

    rows = [cc(known(3000), True), cc(known(0), True), cc(None, True), cc(None, True),
            cc(known(10), False), cc(known(30), False)]
    an = ledger.stats(rows)["anthropic"]
    assert (an["cache_creation_median_after_served_turn"], an["after_served_n"]) == (1500.0, 2)
    assert (an["cache_creation_median_clean_history"], an["clean_n"]) == (20.0, 2)
    assert "cache_creation median (known-usage rows only)" in ledger.format_stats(ledger.stats(rows))
