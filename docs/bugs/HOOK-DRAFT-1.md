---
id: HOOK-DRAFT-1
status: fixed in `fix/hook-draft-list-non-generators`
---
## HOOK-DRAFT-1. The hook's draft chain offered decision-only and classifier models

- **Symptom.** Found 2026-10-11 while surveying which paths pick an Ollama model automatically. On a machine with
  `nimble:9b`, `llmr-classifier:latest` and two `llamacpp:<sha>` imports installed, `hooks/chain_builder.build_chain("moderate", ..., "code")`
  returned `['qwen3.8-uncensored:latest', 'nimble:9b', 'qwen3.5:latest', 'llamacpp:44a3…', …]`. `nimble:9b` answers only a typed
  `decision` call and returns HTTP 400 on generate (bug N19), so a draft that reached it spent an attempt and a
  timeout slot before the next model ran. `model_discovery.first_installed()` read the same list, so session warm-up,
  Playwright compression and the other `installed[0]` fallbacks could pick one of these models whenever it was listed first.
- **Cause.** Two lists of "local models that can generate" used two filters. The router's list
  (`discover.ollama_can_generate`) applies the classifier/unnamed-import name rule and the `/api/show` capability check.
  The hook's list (`model_discovery._is_completion_model`) only dropped embedding models. It never read
  `local_models.ROLES`, where `nimble:9b` is declared decision-only, or the name rule, which lived in `discover`.
  That module costs ~0.56 s to import, too much for the per-prompt hook.
- **Fix.** Move the name rule (`NON_GENERATE_NAME_PREFIXES`, the configured-classifier check, the
  `LLM_ROUTER_LOCAL_ALLOW_MODELS` override) from `discover` to `local_models` (~1 ms import). `discover._is_non_code_model`
  delegates to it and keeps its debug line. Add `local_models.serves_generate` = declared `generate` role and not
  name-ruled. `model_discovery._is_completion_model` now requires it, for both the live probe and the cache.
  The explicit env overrides (`LLM_ROUTER_OLLAMA_MODEL`, `OLLAMA_BUDGET_MODELS`, `OLLAMA_MODELS`) stay unfiltered, as operator intent.
- **Test.** `tests/test_model_discovery.py`:
  - On a mixed list, the cache, `first_installed()` and `chain_builder._ollama_models()` all return only the generator.
  - The role and the name rule each exclude a model by itself.
  - The live probe filters too.
  - The allow-list still admits a named `llamacpp:` import.
  - Importing the hook list does not load `discover`.

  Each test was red-checked by breaking only the condition it names.
