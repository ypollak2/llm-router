---
id: HAIKU55-1
status: fixed in `fix/proxy-unknown-model-haiku55` (deploy: restart the proxy; rows written before it keep null tier and cost)
---
## HAIKU55-1. Every `claude-haiku-5-5` proxy row was written with tier and cost null

- **Symptom (2026-10-10).** `llm-router kpi` G3 (gate P0.8 recompute, window since 2026-10-03, copy of
  `~/.llm-router/proxy_calls.jsonl` taken 2026-10-10 08:19Z): proxy writer FAIL, 5261/5490 rows
  complete (95.8%; missing tier 3.9%, cost 4.2%). 215 of the 229 incomplete rows are one session's burst
  on 2026-10-09 20:02-20:16Z, all `requested_model == claude-haiku-5-5`, `tier_reason ==
  unknown_model`, `tier` and `anthropic_cost_usd` null. Across the whole ledger copy, all 328
  `claude-haiku-5-5` rows (2026-10-08 16:03Z to 2026-10-10 08:18Z) look the same, and no other model is
  `unknown_model`. 228 of the 328 had a prompt over 100,000 tokens.
- **Cause.** Two tables did not know Claude Haiku 5.5. `pricing._ANTHROPIC` had no entry, so
  `pricing.resolve` returned None and `ledger.anthropic_cost` left the cost null. The tier policy's
  `haiku` tier names `claude-haiku-4-5` only, so `ClaudeTierPolicy.tier_of` returned None, the
  decision was `unknown_model`, and that path wrote `tier: None`. The pass-through itself is
  correct: listing Haiku 5.5 under the `haiku` tier (`also:`) would serve those requests on the tier's
  `model`, i.e. move a Haiku 5.5 call onto Haiku 4.5 ($1/$5, ten times the $0.10/$0.50 rate).
- **Fix.** `pricing` prices `claude-haiku-5-5` with both of its rate cards (pricing page, "Model
  pricing" and "Long context pricing", checked 2026-10-10): $0.10/$0.50 per MTok when the prompt is
  100,000 tokens or fewer, $0.50/$2.50 over, the prompt counting input, cache-read and cache-write
  tokens; cache rates are the standard ratios of the applicable input rate. `Price` gains
  `long_prompt_over` / `long_input` / `long_output` and `at_prompt()`; `price_for`, `rates_per_m` and
  `cache_write_1h_rate` take an optional `prompt_tokens`, `cost_usd` and the proxy ledger's
  `_price_tokens` pass it. A flat $0.10 entry would have under-priced 228 of the 328 rows by 5x.
  `ClaudeTierPolicy.family_tier` labels an `unknown_model` row with the configured tier whose name is a
  word of the requested id (`claude-haiku-5-5` -> `haiku`, `claude-3-opus` -> `opus`, a custom id ->
  null). The call is still forwarded unchanged and the reason stays `unknown_model`; `tier_of` is
  untouched, so the label never becomes a routing target. Residual: `claude-haiku-5-5[1m]` is still
  unpriced (Haiku 5.5 is not a standard-rate long-context model), and `claude-mythos-5-1` has no price.
- **Test.** `tests/test_pricing_haiku55.py`: base and long-prompt rates, the 100,000 boundary, cache
  tokens counted in the prompt length, other models unaffected, the burst row's shape priced.
  `tests/test_proxy_tiers.py::test_haiku_5_5_is_forwarded_unchanged_with_a_tier_label_and_a_cost`
  (through the proxy, under and over 100K: upstream gets `claude-haiku-5-5`, the row has `tier: haiku`,
  `tier_reason: unknown_model` and the dollar cost) and `test_unknown_model_label_is_a_word_match_only`
  (`decide` and `decide_unclassified`). All eight fail on `origin/main` (cf444090).
- **Gates to recompute after deploy, on rows written after it:** G3 (proxy writer). Rows already in the
  ledger keep their stored nulls; a G3 window that starts before the restart still counts them.
