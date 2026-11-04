---
id: CLS-STALL-1
status: fixed in `w10/local-classifier-decides` (follow-up commits): a prompt never waits for a model load
---
## CLS-STALL-1. A hook that waits for the decision model stalls a prompt for the length of a cold load

- **Symptom.** First version of the hook client (commit 22391629) waited through a cold load. Measured 2026-11-04
  08:05Z-08:30Z, n=7 calls that began with nimble:9b not resident: 2.04, 4.44, 4.78, 5.02 (timed out at a 5 s budget),
  5.82, 6.71 and 10.12 s, six of them at load average 5.3-6.8 (docs/bugs/CLS-COLD-1.md). A prompt that hits a cold model
  would have been held for that long, on a machine where another model is often resident.
- **Cause.** The verdict was joined under a budget (5 s, then 8 s) with no distinction between "the model is warm and
  answers in about 0.4 s" and "the model is loading". In `shadow`, nothing depends on the answer at all, yet the hook
  still waited for it.
- **Fix.** `shadow` never waits: a detached worker makes the call and writes the row. `on` joins an in-process verdict
  for at most 2 s (`LLM_ROUTER_DOOR_VERDICT_PROMPT_BUDGET_MS`), and a prompt that finds the model not resident
  (`/api/ps`, a few ms) does not wait for the load: the worker loads the model and writes the verdict, the prompt keeps
  today's behaviour and the next prompt decides. An Agent spawn still waits (8 s; its hook already runs for seconds).
  Measured on the real hook (`scripts/door_verdict_hook_cost.py`, 20 model calls, 2026-11-04 09:50Z, load average 5.1 at
  the start and 6.5 at the end, no model resident at the start, scratch HOME so the hook's own work is only about
  120 ms, which hides none of the verdict): added wall time over the classifier-off baseline of 120 ms p50, per prompt, ms
  (p50 / p95 / max): shadow warm 17 / 37 / 40 (n=6); shadow cold 32 / 53 / 54 (n=4); on cold, deferred 49 / 59 / 61
  (n=4); on warm, inline 417 / 604 / 653 (n=6, the verdict itself, 383 ms p50 in the `verdict_wait` phase). With n this
  small p95 is close to the max. The blocking design it replaces would have added the 2-10 s above on a cold prompt.
- **Test.** `tests/test_door_verdict.py`: `test_shadow_never_waits_for_the_model`,
  `test_on_defers_a_prompt_that_finds_the_model_cold_and_the_next_one_decides`,
  `test_an_agent_spawn_waits_even_when_the_model_is_cold`, `test_a_prompt_is_never_held_longer_than_two_seconds_but_a_spawn_may_wait`
  (a real detached worker against a real HTTP server speaking the Ollama wire format).
