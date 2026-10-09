---
id: STALE-OLLAMA-1
status: fixed in `fix/stale-ollama-default-and-silent-drop`
---
## STALE-OLLAMA-1. The static chains named an Ollama model nobody installed, and the filter that dropped it said nothing

- **Symptom (env-doctor, 2026-10-09).** `get_model_chain` / `ROUTING_TABLE` / the YAML showed balanced chains with no
  local model that was actually installed (`qwen3-coder:30b`, `qwen3.8`, `nimble:9b`), and a diagnosis read that as
  "local is not reachable". Live routes did reach local (`llm(task="code")` served by `ollama/qwen3-coder:30b`).
- **Cause.** `policies/standard.yaml` (the Plan 07 mirror of `profiles.py`, commit 2faaa08d) named `ollama/qwen3:32b` in
  every balanced, premium and reasoning chain. `profiles.filter_ollama_by_installed` dropped it with a DEBUG line;
  with an empty discovery cache it passed everything through with no line at all; `get_model_chain` wrapped the call
  in a bare `except Exception: pass`. Local models reach the cheap tiers only because `router._build_and_filter_chain`
  injects `config.all_ollama_models()` at the front, so the static chain never showed them. Two more places kept the
  dead entry alive: the RESEARCH branch of `get_model_chain` returns before the filter, and
  `dynamic_routing.build_dynamic_routing_table` copies `ROUTING_TABLE` with no installed-model filter at all.
- **Fix.** `ollama/qwen3:32b` removed from every chain, `workhorses` and `fallback_chain_complex`; the YAML header
  says local models are injected at route time. No placeholder/loader was added: the only "discovered local model
  for this task" mechanism (`_task_aware_default_order`) lives in the router and runs per request, and resolving it
  at import would read the discovery cache (and probe the network when empty). `filter_ollama_by_installed` warns
  once per dropped model and once per process on the empty-cache skip (only when the chain names an `ollama/*`
  model); `get_model_chain` records `CHZ-FO-PROFILES-OLLAMA-FILTER` and logs a warning.
- **Known and left alone.** `ollama/qwen3.5:latest` and `ollama/hermes3:8b` (budget and balanced/query) are the same
  kind of one-machine default; the RESEARCH branch still skips the filter; the dynamic table still does no
  installed-model filtering. On a machine that has `qwen3:32b`, PREMIUM/REASONING lose it as their last entry
  (those tiers get no injection). The hook's `selected_model` telemetry for balanced research/generate/analyze/code
  was `ollama/*`, now the first non-local entry.
- **Test.** `tests/test_stale_ollama_default.py`: chain per profile x task type (static and dynamic path, fake cache)
  equals the pre-fix golden `tests/fixtures/stale_ollama_chains_base.json` minus the stale entry
  (`test_live_chain_equals_pre_fix_chain_for_every_profile_and_task`,
  `test_the_difference_from_base_is_exactly_the_stale_entry`); no chain or policy field names it; the three log
  behaviours and the recorded fail-open. 8 of the 9 tests fail on the base commit.
