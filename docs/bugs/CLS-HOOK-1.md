---
id: CLS-HOOK-1
status: fixed in `w10/local-classifier-decides` (deploy: install the hooks; `LLM_ROUTER_LOCAL_CLASSIFIER=shadow` then logs a verdict per prompt and spawn, `on` lets it decide)
---
## CLS-HOOK-1. The local classifier never ran on a user prompt or an Agent spawn, and nothing read its verdict

- **Symptom (owner 2026-11-04: "looks like it doesn't really work right now").** `~/.llm-router/classifier_shadow.jsonl`
  (copy taken 2026-11-04T08:03Z; 7 days = 2026-10-28T08:03Z to 2026-11-03T16:34Z, n=2,417 rows; ~/.rsi/research/primary-plan/v16/coord/CLASSIFIER-DIAG-20261104.md) holds proxy turn-first
  requests only. In the same window the hooks saw 214 `UserPromptSubmit` runs, 81 real prompts and 49 Agent spawns
  (ROUTING-RESEARCH-20261103, organic sessions) and called no classifier for any of them. The last proxy row, in
  `classifier_shadow.jsonl` and `proxy_calls.jsonl`, is 2026-11-03T16:34Z; `~/.claude/settings.json` has no
  `ANTHROPIC_BASE_URL`, so sessions started since reach no proxy at all. The verdict's `local_eligible` was `None` on
  1,755 of 1,755 verdicts: this backend (nimble:9b on `/v1/systemone`) answers one question, the cheapest adequate
  Claude tier, so it could never say "local".
- **Cause.** The only caller was the proxy's shadow seam (`proxy/llm_shadow.ShadowScheduler`, `cls_applied` False on every
  row). `hooks/auto-route.py` classified with regex rules plus `engine.shadow` (no model) and `hooks/agent-route.py`
  with regex tables plus `_is_codex_suitable`. Readers of the log (`kpi`, `shadow_eval`, `classifier_health`,
  `daily_offload`, `decision_ledger`) only report.
- **Fix.** `llm_router.door_verdict`: the hook-side client. `auto-route.py` starts one verdict per routed prompt (after
  zero-Claude edit, the sidecar, a user prefix, the strict-ack bypass and the MCP shortcut) and joins it at the
  quiet decision; `agent-route.py` starts one per routed spawn and joins it before the Codex attempt (Explore,
  allowlisted and retrieval-only spawns take no routing decision and get none). `LLM_ROUTER_LOCAL_CLASSIFIER`: `off`
  (zero calls), `shadow` (a text-free row in `door_verdicts.jsonl`, written by a detached worker, nothing changes; the
  default when the decision model is configured), `on` (the verdict decides). The tier is used as a difficulty signal:
  opus with p_max >= 0.6 picks `claude` (the prompt hint is quieted, or the spawn makes no Codex attempt, unit
  `unsuitable` / `reason_code=verdict_claude`); haiku with p_max >= 0.7 picks `local` where a local lane can do the job
  (a passed quality batch, or a commit-message / PR-description prompt with the local text-job switch on, or the caller
  says it has one: `preferred_door(..., local_lane=...)` is the seam for a sub-agent fallback); sonnet, or haiku with no
  lane, picks `codex` (a prompt hint gains `llm(prompt="codex: ...")` when Codex is usable; a spawn goes through the
  existing NS3 gates). No verdict means today's behaviour. A verdict opens no door the existing gates keep shut.
  Share of the 7-day proxy verdicts (n=1,755, 2026-10-28 to 2026-11-03, `classifier_shadow.jsonl`) that reach each
  door at its threshold: claude (opus >= 0.6) 72 (4.1%); local-eligible (haiku >= 0.7) 120 (6.8%, before the lane test,
  which fails today for Q&A and code); codex (sonnet >= 0.5) 937 (53.4%) and the haiku verdicts below 0.7 add more.
- **Test.** `tests/test_door_verdict.py` (a real HTTP server speaking the Ollama wire format) and
  `tests/test_door_verdict_hooks.py` (real hook subprocesses, a real executable standing in for `codex`):
  `test_shadow_logs_one_verdict_for_the_prompt_and_changes_nothing`,
  `test_on_quiets_the_hint_for_a_confident_opus_verdict`, `test_spawn_on_keeps_a_confident_opus_spawn_on_claude`,
  `test_spawn_on_with_a_down_model_delegates_as_before`, `test_off_is_todays_hook_and_makes_no_call`.
