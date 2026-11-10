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
- **Fix.** When `LLM_ROUTER_LOCAL_MODEL` is set and is the classifier's model, `_num_ctx_for` returns the proxy's
  context (`LLM_ROUTER_PROXY_NUM_CTX`, else `local_models.num_ctx`), `_keep_alive_for` returns -1, and
  `_is_loaded` accepts that context. `decision_classifier.payload` uses the same keep_alive. Without the policy,
  nothing changes.
- **Test.** `tests/test_local_classifier.py`: `test_one_model_*` (payload ctx and keep_alive, `_is_loaded`, no
  warm-up when resident at the proxy context, warm-up body) and `test_without_one_model_policy_payload_is_unchanged`.
