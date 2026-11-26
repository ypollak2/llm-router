"""D-626 fix 3 part 1: tier-rewrite (model swap) accounting and its quality signal, on fake ledgers.

Pins: (1) C's excluded count is REWRITTEN rows without a price, the other unpriced tier rows are counted
apart; (2) claude-fable-5-2 is priced at the published rates (platform.claude.com pricing page,
2026-11-26: $8 in, $40 out, $0.20 cache hit); (3) C by part: rewrites gross, other tier rows, net, the
negative rewrites and the model pairs, through the rollup; (4) prev_tool_error / prev_tier_rewritten
fields and the rewrite_quality reader."""
from __future__ import annotations

import json

from llm_router import ledger_view as lv
from llm_router import pricing
from llm_router import savings_rollup as sr
from llm_router.proxy import ledger as L
from llm_router.proxy import rewrite_quality as rq
from llm_router.proxy import steps

USAGE = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}


def _row(ts=1000.0, req="claude-sonnet-5-5", srv=None, usage=USAGE, session="s1", reason="sticky", **kw):
    r = {"ts": ts, "session_id": session, "decision": "forwarded", "upstream_status": 200,
         "requested_model": req, "served_model": srv or req, "tier_reason": reason, "usage": usage}
    r.update(kw)
    return r


def _summ(rows):
    return sr.summarize(rows)


# 1 ── c_unpriced counts rewritten rows only --------------------------------------------------------------

def test_c_unpriced_counts_only_rewritten_rows_without_a_price():
    rows = [
        _row(srv="claude-haiku-5-5"),                                       # rewrite, priced
        _row(srv="claude-haiku-5-5", usage=None, usage_unknown="truncated"),  # rewrite, usage unknown
        _row(),                                                              # not a rewrite, priced
        _row(usage=None, usage_unknown="truncated"),                         # not a rewrite, usage unknown
        _row(req="claude-unlisted-9", usage=USAGE),                          # not a rewrite, model not priced
        _row(req="claude-unlisted-9", usage=USAGE),
    ]
    s = _summ(rows)
    assert s["c_rewritten"] == 2
    assert s["c_unpriced"] == 1                    # was 4 (calls - priced_n): every unpriced tier row
    assert s["c_unpriced_usage_unknown"] == 1      # kept apart, not hidden
    assert s["c_unpriced_unknown_model"] == 2


# 2 ── claude-fable-5-2 priced from the published table -------------------------------------------------

def test_fable_5_2_is_priced_at_the_published_rates():
    p = pricing.price_for("claude-fable-5-2")
    assert p is not None
    assert (p.input, p.output, p.cache_read_rate) == (8.0, 40.0, 0.20)
    assert pricing.price_for("claude-fable-5-2").cache_write_rate == 10.0  # $10 / MTok 5m write (derived 1.25x)
    cost = L.anthropic_cost(_row(req="claude-fable-5-2", usage=USAGE))
    assert cost is not None and abs(cost - (1000 * 8 + 500 * 40) / 1e6) < 1e-12
    assert L.tier_stats([_row(req="claude-fable-5-2", reason="unknown_model")])["priced_n"] == 1


# 3 ── C by part --------------------------------------------------------------------------------------------

def _mixed():
    big = {"input_tokens": 100000, "output_tokens": 2000, "cache_read_input_tokens": 0,
           "cache_creation_input_tokens": 0}
    return [
        _row(srv="claude-haiku-5-5", usage=big),                             # rewrite, gains
        _row(srv="claude-haiku-5-5", usage=big),
        _row(req="claude-opus-5-5", srv="claude-haiku-5-5", reason="haiku_rewrite",
             usage={"input_tokens": 10, "output_tokens": 4000, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0}),                      # rewrite that loses? (see assert)
        _row(tier_switch=True, usage={**USAGE, "cache_creation_input_tokens": 50000}),   # not a rewrite
        _row(),                                                              # not a rewrite, no switch
    ]


def test_c_parts_add_up_to_the_unchanged_net():
    ts = L.tier_stats(_mixed())
    assert ts["est_saving_usd"] == round(ts["rewrites_gross_usd"] + ts["nonrewrite_usd"], 4)
    assert ts["rewrites_priced_n"] == 3 and ts["nonrewrite_priced_n"] == 2
    assert {tuple(p[:2]) for p in ts["rewrite_pairs"]} == {("claude-sonnet-5-5", "claude-haiku-5-5"),
                                                           ("claude-opus-5-5", "claude-haiku-5-5")}
    assert sum(p[2] for p in ts["rewrite_pairs"]) == 3
    assert round(sum(p[3] for p in ts["rewrite_pairs"]), 4) == ts["rewrites_gross_usd"]


def test_negative_rewrites_are_counted_with_their_dollars():
    # served model dearer per token than the requested one: a rewrite that loses money
    neg = L.tier_stats([_row(req="claude-haiku-5-5", srv="claude-sonnet-5-5", reason="policy")])
    assert neg["rewrites_negative_n"] == 1 and neg["rewrites_negative_usd"] < 0
    assert neg["est_saving_usd"] == neg["rewrites_gross_usd"] == neg["rewrites_negative_usd"]
    assert L.tier_stats(_mixed())["rewrites_negative_n"] == 0


def test_the_parts_survive_the_rollup_sum_and_are_additive_across_days():
    rows = _mixed()
    a, b = _summ(rows[:2]), _summ(rows[2:])
    tot = dict(sr._ZERO)
    tot[sr._PAIRS] = {}
    sr._add(tot, a)
    sr._add(tot, b)
    whole = _summ(rows)
    for k in ("c_usd", "c_rewritten", "c_rw_priced", "c_rw_usd", "c_other_n", "c_other_usd", "c_rw_neg_n"):
        assert round(tot[k], 4) == round(whole[k], 4), k
    assert tot[sr._PAIRS].keys() == whole[sr._PAIRS].keys()
    assert sum(v[0] for v in tot[sr._PAIRS].values()) == 3


def test_combined_saved_exposes_c_parts_and_keeps_c_net_unchanged(tmp_path):
    ledger = tmp_path / "proxy_calls.jsonl"
    now = 1_800_000_000.0
    rows = [{**r, "ts": now - 60 + i} for i, r in enumerate(_mixed())]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    fig, parts = lv.combined_saved(lv.calendar_bounds(1, now=now), db_path=tmp_path / "none.db",
                                   proxy_path=ledger, rollup_path=tmp_path / "roll.json")
    assert parts["proxy_available"]
    cp, c = parts["C_parts"], parts["C"]
    assert c["usd"] == cp["net"] == round(cp["rewrites_gross"] + cp["tier_switch_cost"], 4)
    assert c["n"] == 3 and cp["rewrites_n"] == 3


# 4 ── quality signal: fields and reader --------------------------------------------------------------------

def _cont(error: bool) -> dict:
    return {"messages": [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x", "is_error": error}]}],
        "tools": [{"name": "Bash", "input_schema": {}}]}


def test_prev_tool_error_is_a_flag_only():
    assert steps.step_fields(_cont(True))["prev_tool_error"] is True
    assert steps.step_fields(_cont(False))["prev_tool_error"] is False
    first = {"messages": [{"role": "user", "content": "hi"}]}
    assert steps.step_fields(first)["prev_tool_error"] is None


def test_reader_per_pair_rates_with_n_and_unknowns():
    t = [{"name": "Read", "h": "aa"}]
    rows = [
        # rewritten session: step 1 (rewritten) -> next has a tool error and repeats the same target
        _row(ts=1, srv="claude-haiku-5-5", session="a", stop_reason="tool_use", prev_tool_targets=t),
        _row(ts=2, srv="claude-haiku-5-5", session="a", stop_reason="end_turn", prev_tool_error=True,
             prev_tool_targets=t),
        # plain session: next step clean, different target
        _row(ts=1, session="b", stop_reason="tool_use", prev_tool_targets=t),
        _row(ts=2, session="b", stop_reason="end_turn", prev_tool_error=False,
             prev_tool_targets=[{"name": "Read", "h": "bb"}]),
        # old row without prev_tool_error: unknown, left out of the rate's n
        _row(ts=3, session="b", stop_reason="end_turn"),
        {"event": "lifecycle", "ts": 5, "session_id": "a"},                  # events never count
    ]
    st = rq.collect(rows)
    rw = st["claude-sonnet-5-5>claude-haiku-5-5"]
    assert rw["rewritten"] is True and rw["n"] == 2 and rw["stops"] == {"tool_use": 1, "end_turn": 1}
    assert (rw["err_n"], rw["err_known"]) == (1, 1) and (rw["redo_n"], rw["redo_known"]) == (1, 1)
    plain = st["claude-sonnet-5-5>claude-sonnet-5-5"]
    assert plain["rewritten"] is False and plain["n"] == 3
    assert (plain["err_n"], plain["err_known"], plain["next_n"]) == (0, 1, 2)   # the third step is the unknown
    assert (plain["redo_n"], plain["redo_known"]) == (0, 1)
    text = rq.render(st, window="30d", source="fake", rows_in_window=6)
    assert "LIMITS" in text and "not causal" in text and "n=2" in text


def test_reader_on_an_empty_window_says_n_0_not_zero_percent():
    text = rq.render(rq.collect([]), window="30d", source="fake", rows_in_window=0)
    assert "0 Claude steps" in text and "0.0%" not in text
