---
id: CLS-COLD-1
status: fixed for the hook path in `w10/local-classifier-decides`; the proxy shadow seam keeps its no-wait rule (it never blocks a request)
---
## CLS-COLD-1. A model that was not resident meant "no verdict", and session start warmed a different model

- **Symptom.** `classifier_shadow.jsonl` (~/.rsi/research/primary-plan/v16/coord/CLASSIFIER-DIAG-20261104.md), 7 days (n=2,417): 399 rows (16.5%) `source=cold`, p50 3.8 ms (no model call). 30 days
  (n=5,161): 1,111. Only 42.7% of the 7-day cold rows (n=398 with a previous good verdict) came within 600 s of it, so the
  10-minute `keep_alive` was not the main cause.
- **Cause.** (1) `local_classifier._classify` checks `/api/ps` and, for a model that is not loaded, starts a background
  warm-up and returns `cold` instead of waiting. (2) Ollama evicts to fit memory, and the classifier loses. Measured
  2026-11-04 08:05Z: `ollama ps` showed qwen3.8-uncensored (22 GB) with 9 minutes left; one verdict call later it
  showed only nimble:9b. A smaller model did co-reside (gemma3:12b, 8.3 GB, with nimble at 08:30Z). (3)
  `hooks/session-start.py` `_warm_ollama_bg` warmed `model_discovery.local_model()` (qwen3.8-uncensored, 22 GB, default
  keep_alive) and `_warm_edit_model_bg` pinned the same model for 600 s, so every session start put the wrong model in
  memory. Cold start of nimble:9b, n=7 calls that began with the model
  not resident (2026-11-04 08:05Z-08:30Z, this machine): 2.04, 4.44, 4.78, 5.02 (timed out at a 5 s budget), 5.82, 6.71 and
  10.12 s. The 2.04 s one ran on a quiet machine; the other six at load average 5.3-6.8, with gemma3:12b or qwen3.8 resident
  or being evicted. Median 5.02 s. The 4.44 s and 10.12 s values are the session-start preload (`door_verdict.warm`).
- **Fix.** The hook client never makes a prompt wait for the load (docs/bugs/CLS-STALL-1.md). In `on`, session start loads
  the decision model through its own endpoint (`door_verdict.warm`, detached child, same keep_alive as the verdict calls)
  and skips the edit and generic warm-ups; `LLM_ROUTER_CLASSIFIER_PRELOAD=off` restores the old order. In `shadow` it
  loads nothing (docs/bugs/CLS-EVICT-1.md): a log-only verdict must not evict another lane's model. The trade in `on` is
  disclosed: the zero-Claude edit model is no longer pinned at session start (0 of 571 real prompts were edit-eligible in
  30 days, ROUTING-RESEARCH-20261103).
- **Test.** `test_a_cold_model_is_waited_for_and_answers`, `test_session_start_warms_the_decision_model_only_in_on_and_skips_the_other_two`,
  `test_warm_loads_the_decision_model_only_in_on`, `test_warm_is_a_noop_when_off_or_opted_out`.
