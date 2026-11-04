---
id: CLS-TIE-1
status: fixed in `w10/local-classifier-decides` (both the proxy seam and the hook client read `decision_classifier.parse_answer`)
---
## CLS-TIE-1. An exact tie at the top of the tier probabilities was refused as a parse error

- **Symptom.** `classifier_shadow.jsonl` (~/.rsi/research/primary-plan/v16/coord/CLASSIFIER-DIAG-20261104.md), 7 days (n=2,417): 93 `parse_error` rows (3.8%), p50 554 ms, so the model had
  answered. 30 days (n=5,161): 177. Six of the 177 have a text sample in `shadow_text.jsonl`; replaying them on 2026-11-04
  (6 calls to nimble:9b) gave 3 valid answers and 3 with an EXACT tie between haiku and sonnet
  (0.4825230729 = 0.4825230729 with opus 0.0349538542; 0.4718243110 twice with opus 0.0563513780; one of the three was
  the same prompt twice, answered from the endpoint's cache in 0.02 s). All three have p_max below 0.5.
- **Cause.** `decision_classifier.parse_answer` required `sum(p == top) == 1`. The endpoint quantises its
  probabilities, and a model with no preference between two tiers produces an exact tie, which is a valid answer.
- **Fix.** A tie at the top is a verdict: the tier is the highest tied tier (the safer side), the confidence stays p_max.
  A `choice` outside the tied set is still refused. `test_decision_classifier.py::test_a_tie_for_the_maximum_...` pinned
  the old behaviour and now pins the new one. Extrapolation to the other 171 rows is NOT measured: only 6 had text.
- **Test.** `tests/test_door_verdict.py::test_an_exact_tie_at_the_top_is_a_verdict_resolved_to_the_higher_tier` (the two
  replayed ties and an all-equal answer), `test_a_choice_that_is_not_an_argmax_is_still_refused`.
