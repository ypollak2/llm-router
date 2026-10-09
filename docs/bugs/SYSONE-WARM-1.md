---
id: SYSONE-WARM-1
status: fixed in `fix/systemone-warmup`
---
## SYSONE-WARM-1. The decision-model backend never loaded its model: every verdict was `cold`

- **Symptom (verified live 2026-10-09).** With `LLM_ROUTER_CLASSIFIER_BACKEND=systemone` (model
  `nimble:9b`), 133+ rows in `~/.llm-router/classifier_shadow.jsonl` and 0 real verdicts: all `cold`.
  Nothing in the logs said why.
- **Cause.** `local_classifier._warm` loaded the model with `POST /api/generate`. Ollama answers a
  decision model there (and on `/api/chat`) with HTTP 400 `"nimble:9b" does not support generate`,
  so the model never loaded; `classify` saw it absent from `/api/ps`, re-kicked the warm-up and
  returned `cold`. `_warm` only caught exceptions and never looked at the status, so the 400 was
  swallowed. Separately, `_keep_alive()` passed `LLM_ROUTER_CLASSIFIER_KEEP_ALIVE` verbatim; Ollama
  parses a string with Go `time.ParseDuration`, which rejects a unitless `"-1"`.
- **Fix.** The systemone warm-up POSTs `decision_classifier.payload(model, ...)` to
  `/v1/systemone` (the endpoint that loads it, ~1.7 s cold). A non-200 warm-up answer is recorded
  once per process as `CHZ-FO-LOCAL-CLASSIFIER-WARMUP` (`llm-router` failopen counters) with the
  backend and status. `_keep_alive()` returns an `int` for a unitless number (`-1`, `300`), a string
  otherwise; `decision_classifier.payload` no longer treats an explicit `0` as unset.
- **Test.** `tests/test_decision_classifier.py`: `test_cold_model_is_warmed_through_the_systemone_endpoint`,
  `test_a_refused_warmup_is_recorded_once`, `test_keep_alive_unitless_numbers_are_sent_as_integers`
  (5 failed before the fix; the fake Ollama now answers `/api/generate` with the real 400).
