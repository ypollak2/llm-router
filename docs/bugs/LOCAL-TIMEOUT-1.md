---
id: LOCAL-TIMEOUT-1
status: fixed in `fix/local-coder-timeout`
---
## LOCAL-TIMEOUT-1. A local model that timed out led the chain again on the next route, so every code call paid 120 s

- **Symptom (N18, 2026-10-10).** In the live chain for `llm(task="code", tier="fast")`, `ollama/qwen3-coder:30b` is
  first. `routing_quality.jsonl`, 08:34:22Z to 09:30:14Z: it timed out at litellm's 120 s on 21 of the 21 routes that
  tried it. Each time `ollama/qwen3.8:latest` answered right after it, in 2-15 s (one call took 69 s). The M0-3 rerun
  (T0 08:59:12Z) falls in this window.
- **Cause, Ollama side (machine, not code).** Ollama server log (`~/.ollama/logs/server.log`, times are +01:00): at
  08:32:12Z the nimble:9b MLX runner was evicted so qwen3.5 could load. qwen3.8 then loaded at `num_ctx` 131072
  (predicted 42.7 GiB against 37.4 GiB of Metal memory, `metal_partial_offload`). A `/v1/systemone` request for
  nimble:9b came in at 08:32:14Z. It waited in the scheduler until 09:51:45Z (`499 | 1h19m31s`, then
  `error loading llama server: context canceled`). From 08:32:24Z to 09:51:45Z no runner started, so every request
  for a model that was not loaded waited behind it and was never logged. Requests for loaded models were still
  served. No request from the router to qwen3-coder appears in the log in that window.
- **Cause, router side (this fix).** `router._dispatch_model_loop` remembered nothing between routes. A model that
  had just timed out led the next route again and used the whole `request_timeout` (120 s) again. The hook path
  already moves chronically slow models back (`hooks/chain_builder._demote_unreliable`). The MCP `llm()` path did not.
- **Fix.** An `ollama/*` attempt that ends in a timeout (`litellm.Timeout` or `TimeoutError`) records the time.
  For `LLM_ROUTER_LOCAL_TIMEOUT_COOLDOWN_S` seconds (default 600, 0 = off), `_demote_timed_out_local` moves that model
  to the end of the chain for each route. The model is moved, never dropped. It still answers if everything before it
  fails, and it leads again when the cooldown ends. An explicit routing.yaml pin is exempt, as it is from the quality
  breaker (GH#64). A one-model `model_override` chain is never reordered. The BUDGET emergency chain is demoted the
  same way. The check that skips an emergency chain identical to the primary one compares against the order before
  demotion. The state lives in the process. While a model is cooling down, a route reaches it only when every model
  before it has failed (a timeout then restarts its cooldown). Other errors and non-local timeouts do not count.
- **Measured.** Before the fix, the same router path took 0.48 s (coder loaded) and 4.5 s (coder loaded from cold,
  with qwen3.8 evicted) on 2026-10-10 12:47Z after Ollama was restarted. So the model itself is not slow. The 120 s
  came from the stuck scheduler.
- **Known and left alone.** The stuck Ollama scheduler is an Ollama problem. Lowering memory pressure makes it less
  likely (see the N18 report: global `OLLAMA_CONTEXT_LENGTH` 131072, classifiers kept loaded with no expiry, qwen3.8
  at 131072). `nimble:9b` (capabilities `["decision"]` in `/api/tags`) is in the chain because
  `config.all_ollama_models()` returns every installed model from the discovery cache. The cache only filters out
  embedding models, so the call fails in about 30 ms with "does not support generate". That is a separate change.
  The `route_start` log line (`top_model`) and `prepare_prompt` still use the order before demotion. The ledger's
  `chain_attempts` shows the order that actually ran.
- **Test.** `tests/test_local_timeout_demotion.py` covers these cases: the second route after a coder timeout starts
  with qwen3.8; the demoted model is still tried last; it leads again after the cooldown; `0` turns demotion off;
  non-timeout errors and remote timeouts do not demote; order is kept when every model is cooling down; an explicit
  routing.yaml pin keeps its place; the emergency chain is demoted and an identical one is not re-run; the setting is
  parsed safely. On the base commit all 14 tests error (the helpers do not exist). Removing each change on its own
  fails at least one behaviour test: the primary demotion (3 fail), the emergency demotion (1), and the
  pre-demotion comparison (1).
