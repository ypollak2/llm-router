---
id: DISC-PROMOTE-1
status: fixed in `fix/p015-safety-floor` (PLAN v16 P0.15 task 2; owner decisions D-60b = A, D-60c = A, D-62 = A)
---
## DISC-PROMOTE-1. A newly discovered local model led every cheap-tier chain with no validation

- **Symptom.** An Ollama model pulled today entered the front of the BUDGET and BALANCED chains
  (and, for an OpenAI-compatible server, every chain) as soon as it showed up in `discovery.json`
  or `/api/tags`, whatever it was and whatever the task. Order inside the injection was a name
  heuristic. Nothing had measured the model.
- **Cause.** Every injection door took the list of locally available models as trusted:
  `router._build_and_filter_chain` for `config.all_ollama_models()` and for
  `config.all_openai_compat_models()` (the proxy `policy_chain` inherits this path),
  `hooks/chain_builder._ollama_models` (also reached by the SDK and the gateway), and the
  `_nowait` list the `auto-route` hook builds at import. `discover.py` also stamped every model
  with the task list `["query","generate","analyze","code"]` as if observed.
- **Fix.** `model_discovery.order_for_chain(models, task_class, *, critical)` splits the list into
  `front` (user-configured: `OLLAMA_BUDGET_MODELS`, `OLLAMA_MODELS`, `LLM_ROUTER_OLLAMA_MODEL`, the
  `Config` fields, a `routing.yaml` pin; or grandfathered) and `tail` (everything else). `front` is
  placed as before. D-60c = A: `tail` is appended after the last entry of a simple (BUDGET)
  query/generate/analyze chain, after every reorder, and is absent from every other chain (code,
  moderate and harder). Matching is by id only. D-62 = A: `grandfathered_models.json` is written once,
  atomically, at the first run, from the installed list, and only when prior llm-router state exists
  (`discovery.json`, `profile.yaml` or `usage.db`); a fresh install writes an empty `ids`; a deleted
  file is not recreated (a `grandfathered_models.init` marker records the one-time write) and
  `llm-router doctor` reports it. `discover.py` marks the assumed task list `task_types_assumed: true`.
  `LLM_ROUTER_PROMOTE_GATE=off` restores today's chains (logged once per process).
  **Accepted trade-offs.** A model pulled after the upgrade is not preferred until it is configured
  (graded promotion is 16.3). Models already installed at the first run stay where they are but are
  still unvalidated. A fresh install is told apart from an upgrade only by the three files above;
  a `usage.db` created before the first probe makes a fresh install look like an upgrade.
- **Test.** `tests/test_p015_promote_gate.py`: P0.15-b (router: unvalidated model last in 3 of 12
  chains and absent from 9, first in 0; validated models keep their place; non-balanced policies do
  not move it), P0.15-e (hook), P0.15-c (grandfather file), P0.15-f (an AST audit of every use of
  the five list functions; fails when a new one appears unmarked), the escape valve against chains
  recorded from origin/main, and one mutant per door. `tests/test_p015_chain_replay.py` (T13f) runs
  a 300-cell matrix.
