---
id: TJCHAIN-1
status: fixed in `fix/text-job-chain-any-size` (deploy: restart the MCP server; a running process keeps the old chain)
---
## TJCHAIN-1. The chain of a text job followed the size and the wording of its input, so the local lane refused every job whose prompt was over 2000 characters

- **Symptom (2026-11-04, from the brief of this fix; measured by the coordinator on the live code, not re-run here).**
  `_llm_text_job('commit_message', <real diff>)` with `LLM_ROUTER_LOCAL_TEXT_JOBS` unset, 9 input sizes: 300 and 1500
  chars gave `chain=[qwen3.6:35b-a3b-coding]` and `accepted`; 2000, 2800, 3200, 3600, 4000, 5000 and 7469 chars gave
  `status=fallback, fallback=lane_refused, attempts=0` ("the passed local model is not in the router's chain for
  commit_message") and the log line `Routing policy 'local-first' applied - top model: anthropic/claude-sonnet-4-6`.
  `passed_lane_available()` was True at every size; the lane's `scope.max_prompt_chars` is 94464 for that model. Diffs
  of the last 100 commits on main: p10 5,714 chars, p50 19,208 chars. The lane worked for tiny diffs only.
- **Reproduced (2026-11-04, a727cb26, `tests/test_lc12c.py` fixtures with the real chain builder, fake providers, no
  model).** Flag unset, input 2000 and 40000 chars: `lane_refused`, 0 attempts, no model called. Flag `=1`: a cloud
  model (`openai/gpt-4o` in that world) is tried first and the local model never. `classify.complexity_for` on
  `build_prompt("commit_message", ...)` for 13 sizes from 82 to 40000 chars: a 1920-char prompt is `moderate`, a
  2020-char prompt is `complex` (`ROUTER_POLICY`: simple up to 599 chars, complex over 2000, length only).
- **Cause.** `tools/text.py::_llm_text_job` called `route_and_call` with no `complexity_hint`, so
  `router._resolve_profile` derived the tier from the prompt: its length, and under `LLM_ROUTER_ENGINE_MODE=decide` the
  engine's verdict on its wording. A prompt over 2000 chars is `complex`, which maps to the PREMIUM profile.
  `_build_and_filter_chain` injects local Ollama models for the cheap tiers only (`_cheap_tier`, `_local_all`), so the
  PREMIUM chain held no local model. `eligibility_routing.prefer_passed` moves a model that is already in the chain
  and adds nothing, so `passed_model_leads` was False and `_drop_failed_eligibility` raised `PassedModelNotInChain`
  (flag unset). With `=1` the same chain ran on cloud entries. The lane's own scope was never the limit.
- **Fix.** `_llm_text_job` passes `complexity_hint="moderate"`, a fixed tier (BALANCED). The passed model now leads the
  chain at any size or wording inside the lane's scope. Nothing was added to `prefer_passed`; every
  guard of the lane stays: enabled, passed, digest-verified, settings-matched, inside `scope.max_prompt_chars`,
  loopback only, and a pass-only job is served by the passed model alone or handed back.
- **Compaction, TJCHAIN-3.** With the chain fixed, a replay showed the next defect: `compact_structural` in
  `src/llm_router/compaction.py` also ran on text-job prompts over the threshold in `src/llm_router/config.py`, and kept
  the first 20 and last 10 lines of the fenced diff. A judged sample of accepted messages (`~/.rsi/research/primary-plan/v16/coord/textjob-replay/JUDGE-20261104.md`,
  n=19, one judge, raw diffs from squash commits): under 16000 chars 6 of 7 accurate, 0 wrong; at 16000 and over 3 of 12
  accurate, 5 wrong. The acceptance check caught none. A text job is no longer compacted; see `docs/bugs/TJCHAIN-3.md`.
- **Test.** `tests/test_lc12c.py`: `test_text_job_chain_leads_with_the_passed_model_at_any_input_size` (300, 2000 and
  40000 chars, flag unset and `=1`), `test_text_job_chain_ignores_the_engines_verdict_on_the_wording`, the scope
  boundary (`test_input_over_the_lane_scope_is_handed_back_and_calls_no_model`, 3000 runs, 3001 is refused) and the
  three router-level refusals (over scope, no lane, passed model not configured) with `llm_text_job`'s own gate
  forced open or the real chain. Mutation check: with `complexity_hint` removed from `_llm_text_job` and the rest of the
  fix kept, 7 of the 19 new tests fail (the 2000- and 40000-char cases, the wording cases and the at-limit case).
