---
id: HAIKU55-TIER-1
status: fixed in `feat/haiku-tier-5-5` (deploy: set the `haiku` tier in `~/.llm-router/claude_tiers.yaml` to `claude-haiku-5-5` as in the shipped file's comment, then restart the proxy; the shipped default stays `claude-haiku-4-5`)
---
## HAIKU55-TIER-1. The Haiku tier hard-coded Haiku 4.5's behaviour, so pointing it at Haiku 5.5 would have been wrong three ways

- **Symptom (2026-10-10, before the switch).** Owner approved moving the live `haiku` tier from
  `claude-haiku-4-5` to `claude-haiku-5-5`. Changing only `model:` would have: (1) stripped adaptive
  thinking and `output_config.effort` from every Haiku call, though 5.5 takes both; (2) folded each
  mid-conversation `role: "system"` message (dropping Claude Code's per-turn effort), though 5.5 accepts
  the role; (3) applied the 150K context limit and 64K output clamp written for 4.5, with no limit tied
  to 5.5's price step ($0.10/$0.50 per MTok up to 100,000 prompt tokens, $0.50/$2.50 above).
  Switching the tier config alone to what 5.5 accepts (`thinking: [adaptive]`, effort on) would have
  made the opposite mistake: `_accepts` is true, so the rewrite path's body checks never run and a
  body 5.5 400s on (sampling parameters, assistant prefill) or one past the cost line goes to Haiku.
- **Cause.** `translate.for_haiku`, `tiers.haiku_block_reason`, `HAIKU_MAX_CONTEXT_TOKENS` and
  `HAIKU_MAX_OUTPUT_TOKENS` were written for one model and read no model id. The body checks ran only
  when the tier did not natively accept the request's thinking/effort.
- **Sources (platform.claude.com, fetched 2026-10-10).** `models/haiku-5-5/overview`: "Model ID:
  `claude-haiku-5-5`", "Context window: 1M tokens · Max output: 128K tokens", "$0.10 / MTok for prompts
  up to 100,000 tokens; $0.50 / MTok for prompts over 100,000 tokens". `models/haiku-5-5/migration-guide`:
  "`claude-haiku-5-5` is a fixed model ID with no date suffix and no separate alias"; "A `thinking` value of
  `{"type": "enabled", "budget_tokens": N}` returns a 400 error"; "If a request includes `temperature`, it
  must be `1`. If it includes `top_p`, it must be `0.99` ... So does any `top_k` value"; "Claude Haiku 5.5
  rejects [assistant prefill] with a 400 error"; "the same input text produces approximately 30% more
  tokens on Claude Haiku 5.5 than on Claude Haiku 4.5". `build-with-claude/thinking-troubleshooting`:
  Haiku 5.5 "Adaptive only", rejected `"enabled"`, `"disabled"`^2 (accepted at effort `high` or below).
  `build-with-claude/effort`: supported list includes `claude-haiku-5-5`; "Claude Haiku 5.5 supports all
  five effort levels". `build-with-claude/mid-conversation-system-messages`: "available on ... Claude
  Sonnet 5.5, and Claude Haiku 5.5. No beta header is required". `models/haiku-4-5/overview`: legacy,
  "Claude Haiku 4.5 uses manual extended thinking", effort "Not supported", "Retirement: Not sooner than
  October 15, 2026". build-with-claude/effort: "With `thinking: {"type": "disabled"}`, effort can't change mid-conversation: a per-message
  `output_config.effort` that differs from the level in effect returns a 400 error" (so `disabled` plus a differing
  per-message effort is `params`; the level in effect is the top-level effort, default `medium`). Not confirmed from docs: whether Claude Code ever sends `thinking: disabled`
  (the tier does not list it, so such a request floors to Sonnet).
- **Fix.** The Haiku rules key off the tier's `model` (`ClaudeTierPolicy.haiku_model`; `translate.haiku_is_legacy`:
  every id but `claude-haiku-5-5` is the 4.5 rule set, unchanged). For 5.5: `for_haiku` keeps thinking
  (drops only budget-token `enabled`), effort and system messages, clamps `max_tokens` to 128K, never
  folds (`haiku_folds_system` is false); `haiku_block_reason` skips the system-role check, adds `params`
  (sampling values 5.5 rejects, or a final assistant turn) and limits context
  per request to `haiku55_context_limit(body) = (1_000_000 - max_tokens) / 1.3` estimated (chars/4)
  tokens, `max_tokens` being the request's own or the 128K cap (`HAIKU55_MAX_CONTEXT_TOKENS` = 670,769, the
  worst case). That is the owner's decision of 2026-10-10 (first 75K, the 100K price step / 1.3, then
  "any call that fits the window"): `estimate * 1.3 + max_tokens <= 1M`, 1.3 being the documented tokenizer
  increase over 4.5, which the chars/4 estimate tracks. Calls over 100,000 real prompt tokens bill at
  $0.50/$2.50 per MTok, five times the base card, still below Sonnet 5.5's $2/$10. The arm's
  `haiku_body_blocked` and the tier's own checks share `haiku_block_reason`, so both use the per-model limit
  (4.5: 150K). Residual: the estimate is chars/4, so a token-dense body (JSON, code) can exceed the 1.3
  factor and 400 on the window; the 4xx retry with the client's original bytes covers it. A final guard in `decide` runs the body checks on
  any Haiku 5.5 target, whichever path chose it, and moves a blocked call up a tier with reason
  `haiku_body_blocked` (`tier_haiku_block` names the check). The 4.5 native path is not guarded (as before).
  Residual: a request that names `claude-haiku-4-5` is `unknown_model` (labelled `haiku`, forwarded
  unchanged) once the tier model is 5.5; list it under `also:` only if explicit 4.5 pins should be moved.
- **Test.** `tests/test_proxy_haiku55_tier.py` (29 cases): for_haiku and block reasons per model, the
  decision through `ClaudeTierPolicy.decide`, and that the 4.5 policy is unchanged. Mutation check
  (each reverted after): removing the final guard, letting the fold apply to 5.5, using the 150K limit
  for 5.5, and routing 5.5 through the 4.5 branch of `for_haiku` each fail at least one test.
- **Gates to recompute after deploy, on rows written after it:** the Haiku arm and `haiku_rewrite` counts
  (served model is now 5.5, `tier_body_rewrite` is rare because 5.5 takes adaptive thinking as sent),
  and cost per Haiku row at the 5.5 rate card.
