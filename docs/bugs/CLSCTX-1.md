---
id: CLSCTX-1
status: fixed in `fix/classifier-shared-runner` (deploy: restart the MCP server and proxy)
---
## CLSCTX-1. The classifier ran the one local model at num_ctx 4096 and keep_alive 30m, evicting the proxy's 32768 runner

- **Symptom (2026-11-09/10, :11435 Ollama server log).** One-model policy (`LLM_ROUTER_LOCAL_MODEL=qwen3.6:35b-a3b-coding`,
  `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_CONTEXT_LENGTH=32768`, `OLLAMA_KEEP_ALIVE=-1`).
  Load events by ctx: 21:26:42 32768; 21:27:14 4096; 21:27:21 32768; 21:28:10 4096; 21:31:20 32768; 23:42:28 4096
  (request queued 2h11m, cancelled 499, load aborted, nothing resident); 00:04:40, 07:28:07, 07:50:00, 08:21:35 4096.
  `/api/ps` at 08:21: context_length 4096, expires 10 minutes out. Each 4096 load answered `/api/chat` HTTP 500
  after about 7.8 s; the classifier timeout is 2.0 s, so it could never use a cold load.
- **Cause.** `local_classifier` hard-coded `NUM_CTX = 4096` and `keep_alive` 30m, and `_is_loaded` reported the
  model cold at any other context, so `_warm` reloaded it at 4096. Ollama keys a runner on num_ctx; with one slot
  each warm-up evicted the proxy's runner, then the short keep_alive expired the 4096 one.
- **Fix.** One helper, `toolkit/adapters/ollama.shared_runner_opts(model) -> (num_ctx, keep_alive) | None`: set only
  when `LLM_ROUTER_LOCAL_MODEL` equals the model; num_ctx is the `local_models` table value only (no env: the live proxy has neither `LLM_ROUTER_PROXY_NUM_CTX` nor
  `LLM_ROUTER_LOCAL_MODEL`, so every process must derive the same number), keep_alive
  is -1. It beats `LLM_ROUTER_AGENT_NUM_CTX` / `LLM_ROUTER_LOCAL_NUM_CTX` (`ollama.num_ctx` consults it, so the agent
  loop, `direct_executor._local_num_ctx` and `warm.warmup_payload` follow). Callers using it: `local_classifier`
  (payload, warm-up, `_is_loaded`), `decision_classifier.payload`, `proxy/backends.OllamaBackend`,
  `proxy/server.ProxyConfig` (warm-up body), `warm.edit_keep_alive(model)` (so `zero_claude_edit` and the edit warm-up),
  `direct_executor.call_ollama`, and the `hooks/session-start.py` generic warm-up. Without the policy nothing changes.
  Also `providers._ollama_num_ctx` (policy beats an explicit `LLM_ROUTER_OLLAMA_NUM_CTX`) and `context_summary._generate`.
  `door_verdict.warm` calls `local_classifier.classify_local`, which builds its body with `_payload`, so it is covered.
  An explicit proxy `--num-ctx` that differs from the table value is kept but `ProxyConfig` logs a WARN naming the
  thrash risk.
- **Deliberate escalations, not changed.** `pack_overflow.py` (lines 226-228) and `providers.py` (line 373) escalate to a
  larger window or model on overflow, which changes the runner on purpose: refusing the overflow would drop the request.
- **Test.** `tests/test_shared_runner_opts.py` (one per caller; 10 of 12 fail on origin/main) and the `test_one_model_*`
  tests in `tests/test_local_classifier.py`.
