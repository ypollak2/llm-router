---
id: CLS-EVICT-1
status: fixed in `w10/local-classifier-decides` (review follow-up): shadow never loads the decision model over another lane's model
---
## CLS-EVICT-1. A log-only verdict evicted the model the local text-job lane runs on

- **Symptom (independent review of 437f5f93, 2026-11-04).** `preload_enabled()` was true in `shadow`, so session start skipped the
  edit and generic warm-ups and loaded the decision model, and every prompt's worker called it. Ollama here keeps one big model
  resident (`/api/ps` showed qwen3.8 evicted by one verdict call, docs/bugs/CLS-COLD-1.md). The deployed commit-message lane
  (`qwen3.6:35b-a3b-coding`, enabled in `local_routing.json` since 2026-11-04T10:38Z) would then be evicted by each prompt and pay a cold
  load on its next call, and its own call would evict the decision model again. Cold loads of that lane model on the shared Ollama,
  2026-11-04 15:00Z-15:30Z, load average 7-38, n=4 (a bare load, a 1-token generation, two text-job calls): 8.8, 32.8, 36.0 and 39.4 s.
- **Cause.** `door_verdict.classify` loaded the decision model whatever else was resident, and `preload_enabled` was `mode() != "off"`.
- **Fix.** `shadow` classifies only when the decision model is already resident or no model is (one `/api/ps`); otherwise the row says
  `skipped_slot_busy` (or `skipped_slot_unknown` when `/api/ps` fails) and no request is sent. Session start loads the decision model only in
  `on`, so in `shadow` the old edit and generic warm-ups run. `on` still loads it (the verdict decides, so it needs one).
- **Measured** (`scripts/door_verdict_lane_effect.py`; raw JSON `~/.rsi/research/primary-plan/v16/coord/CLASSIFIER-LANEEFFECT-20261104-{before,after,control}.json`).
  One commit-message call to the lane model, right after one shadow prompt through the real `auto-route.py`. On the shared Ollama this could not be
  separated from other tenants (another process's qwen3-coder:30b evicted the lane model within a second of every load, so `/api/ps` never showed
  it resident), so it ran on a private `ollama serve` with `OLLAMA_MAX_LOADED_MODELS=1` (own port and HOME, the real models directory read-only) and a
  smaller stand-in lane model, `qwen3.5:latest` (6.6 GB; the 22 GB lane model could not be reloaded repeatedly without loading the shared Mac).
  Load average 8.1-9.7, n=2 per condition, text-job ms (of which model load ms):

  | condition | the prompt's verdict | lane model resident after the prompt | text job ms (load ms) |
  |---|---|---|---|
  | before (9234eff3; n=2, `scripts/door_verdict_lane_effect.py`) | `llm`, loaded nimble in 5,435 and 5,925 ms | no, only nimble:9b | 5,326 (3,695) and 5,540 (3,105) |
  | after (n=2, `scripts/door_verdict_lane_effect.py`) | `skipped_slot_busy`, 113 and 115 ms | yes | 2,038 (2.1) and 2,056 (2.3) |
  | control, no prompt | none | yes | 1,961 (2.9) |

  After the fix the text job costs what it costs with no prompt (2.0 s against 1.96 s); before it cost 3.3-3.6 s more, a cold reload. With the real
  lane model the reload is the 8.8-39.4 s above, so the effect is larger there; it was not measured cleanly. An earlier version of the script loaded the
  lane model at the server's default context and called it at 8,192, which forces a reload in every condition; those rows are discarded.
- **Test.** `tests/test_door_verdict.py`: `test_shadow_skips_when_another_model_is_resident_and_never_loads_over_it`,
  `test_shadow_classifies_when_the_slot_is_free_or_nimble_is_already_resident`, `test_shadow_does_not_guess_when_residency_is_unknown`,
  `test_on_loads_the_decision_model_even_beside_another_resident_model`, `test_warm_loads_the_decision_model_only_in_on`;
  `tests/test_door_verdict_hooks.py`: `test_shadow_does_not_load_the_decision_model_over_the_text_job_lanes_model`,
  `test_session_start_in_shadow_loads_nothing_so_the_old_warm_ups_run`.
