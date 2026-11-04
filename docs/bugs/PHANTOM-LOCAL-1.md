---
id: PHANTOM-LOCAL-1
status: fixed in `fix/local-chain-installed-models`
---
## PHANTOM-LOCAL-1. A local route's first attempt went to a model Ollama does not have

- **Symptom (2026-11-02..03).** In the 7 days to 2026-11-04T11:00Z (`~/.llm-router/routing_quality.jsonl`, 113 routes,
  72 with a local attempt, 94 local attempts) 9 attempts were Ollama's 404 `model 'qwen3.8:latest' not found`; 7 of
  the 9 were the FIRST attempt of their route (9.7% of the 72 local routes) and 2 the fourth, after three quality-gate
  rejections. `ollama/qwen3.5:latest` or `ollama/qwen3-coder:30b` answered each time. The same 9 are
  `attempt_failed` rows for that model in `execution_events` (`usage.db`, 99 local attempt events in the window).
  Claude session `1a45a332`: 5 local routes, 10 local attempts, 4 of them the 404 (four routes failed first on
  `ollama/qwen3.8:latest`, as reported); the status line's "4/9" is unmeasured here. The model was removed on
  2026-10-11 (`ollama list` has `qwen3.8-uncensored:latest` and no `qwen3.8:latest`;
  `LLM_ROUTER_OLLAMA_MODEL=qwen3.8-uncensored:latest` in `~/.llm-router/.env`).
- **Cause.** Two inputs and one defect. Input: the `llm_router` MCP server entry in `~/.claude.json` (and the Codex
  `[mcp_servers.llm_router.env]` block) carries `OLLAMA_BUDGET_MODELS=qwen3.8:latest,qwen3-coder:30b,qwen3.5:latest,lfm2.5:8b`
  (two of the four are not installed) and `LLM_ROUTER_ENSEMBLE_PRIMARY=ollama/qwen3.8:latest`. Defect: that list was
  believed as an inventory. (1) `model_discovery._inventory()` returned it unchecked, so `local_model()` called the
  user's real, installed setting "not installed" (it was not in the list) and returned the list's first name, the
  missing one. (2) `policies/standard.yaml` names `ollama/@local` in the BUDGET chains; `ROUTING_TABLE` resolved it
  through `local_model()` and `dynamic_routing.build_dynamic_routing_table` copied the result once, at server start,
  filtering by PROVIDER only. `_build_and_filter_chain` takes the dynamic chain whole when the table exists, so
  `profiles.get_model_chain`'s `filter_ollama_by_installed` never ran: STALE-OLLAMA-1 recorded "the dynamic table
  still does no installed-model filtering" and left it. (3) `Config.all_ollama_models()`'s budget-list fallback ran
  `ollama_can_generate`, which fails open on the 404 of `/api/show`. (4) The generic failure branch counted the 404
  with `tracker.record_failure("ollama")`: the health threshold is 2, so two missing models in one chain benched
  every installed model behind them.
- **Fix.** `model_discovery.installed_only(names)` drops names a FRESH installed list does not carry (unknown list or
  stale cache: names unchanged, a false drop is worse than a late one); it is applied to `_inventory()` (hence
  `local_model()` and `@local`), to the `OLLAMA_BUDGET_MODELS` / `OLLAMA_MODELS` branch of `available_ollama_models`
  and `_nowait`, and to the config's budget fallback. `local_model()` checks the user's setting against Ollama's own
  list, not the narrowed one. The explicit single `LLM_ROUTER_OLLAMA_MODEL` still wins as written. The dynamic table
  entry goes through `filter_ollama_by_installed` (router `_drop_uninstalled_local`, fail-open recorded as
  `CHZ-FO-ROUTER-DYNAMIC-OLLAMA-FILTER`). Runtime guard for any source left: an Ollama `model '<x>' not found`
  is remembered for 600 s per process (`_local_missing_at`), the model is dropped from the next chains (never to an
  empty chain), and the 404 is not counted against the provider. The warning "set but not installed" is now once
  per process, not on every route.
- **Known and left alone.** The owner's config still names the missing models; they are now ignored and logged once
  (`'qwen3.8:latest' is named in the environment but Ollama does not report it installed`). `LLM_ROUTER_ENSEMBLE_PRIMARY`
  naming a missing model is the classifier's, not a route's, and fails open to the heuristic (`llm-router doctor`
  reports it). The streaming door (`route_and_stream`) does not record a missing model. The F26 floor still takes
  `config.all_ollama_models()` as before.
- **Test.** `tests/test_phantom_local_1.py` (21 tests; 20 fail on `a727cb26`): `installed_only` (fresh, unknown, stale,
  probe-and-refresh), a missing budget name absent from `available_ollama_models` / `_nowait`, `local_model()` returning
  the installed setting outside a narrowed list, the warning once, the config fallback, a dynamic-table ghost absent
  from `_build_and_filter_chain` and the filter's fail-open record, and the runtime guard (not tried on the next route,
  does not bench `ollama`, tried again after the lifetime, chain never emptied, only Ollama's own 404 counts).
  `tests/test_pg4_auto_route_latency.py::test_nowait_fresh_cache_and_env_override_spawn_nothing` now gives its env
  list names the cache carries: its old expectation (an env list wins over a cache that does not list it) is the bug.
