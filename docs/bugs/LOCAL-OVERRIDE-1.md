---
id: LOCAL-OVERRIDE-1
status: fixed in `fix/openai-compat-local-override`
---
## LOCAL-OVERRIDE-1. An explicit call to a self-hosted `openai_compat` server could leak to the cloud and was never logged

- **Symptom.** Found 2026-10-11 while calling a local MLX server (Qwen3.8-27B, `mlx_vlm server` on 127.0.0.1)
  through `route_and_call(..., model_override="openai_compat/<id>")`, with `block_providers: [openai_compat]`
  keeping it out of default chains. Three faults:
  (a) The call answered, but logged `routing_decisions insert rejected: invalid provider 'openai_compat'`, so it
  never reached the ledger that `northstar` counts.
  (b) If the server is down, the failed override walked the BUDGET emergency chain, so the prompt went to whatever
  paid cloud model that chain held.
  (c) Savings bookkeeping treated it as paid: `tiers.tier_of` → `paid_api`, `log_usage` kept litellm's cost, and the
  quota hint had no free-local bucket for it.
- **Cause.** `openai_compat` existed as a provider (`config.py`, `provider_quirks.OpenAICompatQuirks`) but was never
  added to the hand-written provider sets: `cost.VALID_PROVIDERS`, the forced-$0 set in `log_usage`,
  `tiers._TIER_PREFIXES`, and `quota_savings._FREE_LOCAL_PROVIDERS`. The emergency fallback in
  `_dispatch_model_loop` had no `model_override` check, so it substituted models for an explicit local pin.
- **Fix.** Add `openai_compat` to those four sets. Skip the emergency fallback when the override's provider is in
  `qa_policy.QA_STRIP_PROVIDERS` (= `types.LOCAL_PROVIDERS ∪ {openai_compat}`), and log
  `emergency_fallback_skipped reason=local_model_override` (only where the fallback would have run). The provider is
  read from the part before the first `/`, so a bare `ollama` override is covered. `codex` and `gemini_cli` are not in
  that set and keep the fallback, as does any failed cloud override (owner decision).
  Deliberately not changed: `context.py` privacy-`local` set (needs a loopback check on the base URL, since
  `openai_compat` can point at a remote server), `types.LOCAL_PROVIDERS` (it feeds budget and chain_builder), and about
  10 duplicated free-provider literals elsewhere (needs a separate consolidation PR).
- **Test.** `tests/test_openai_compat_local_override.py`: a failed local override calls only the local model and logs
  the skip reason; a failed cloud override still reaches the emergency chain (scope control); tier, quota bucket and
  $0 usage row. `tests/test_gf_validate_routing_insert.py` adds `openai_compat` to the accepted providers. Each was
  red-checked by deleting only its one token or condition.
