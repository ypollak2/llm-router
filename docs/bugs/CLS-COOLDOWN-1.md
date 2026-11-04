---
id: CLS-COOLDOWN-1
status: fixed for the hook path in `w10/local-classifier-decides`; the proxy shadow seam keeps its cooldown
---
## CLS-COOLDOWN-1. One timeout silenced the classifier for 30 seconds

- **Symptom.** `classifier_shadow.jsonl`, 7 days (n=2,417): 170 `timeout` rows, of which 129 (76%) have `ms == 0.0`: they
  never reached the model. Only 41 were real timeouts (16 at about the 2.0 s budget, 16 at about 1.0 s, 9 longer).
- **Cause.** `local_classifier._classify` sets `_cool_until = now + 30 s` on any exception or timeout, and
  `classify_async` answers `timeout` without a request while it runs.
- **Fix.** `door_verdict` keeps a breaker in the state dir (`door_verdict_state.json`): it opens for a minute only after two
  consecutive failures within two minutes, a verdict closes it, and one slow call costs one verdict.
- **Test.** `test_one_slow_call_does_not_stop_the_next`, `test_two_failures_in_a_row_open_the_breaker_and_a_verdict_closes_it`,
  `test_failures_far_apart_do_not_accumulate`.
