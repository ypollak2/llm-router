# PREREG-shadowlab: nimble:9b live-shadow adoption gate (PLAN v16 P1.7-d, R1)

Status: frozen by the hash in `PREREG-shadowlab.sha256`. Changing any byte of this file changes
the hash; a changed file is a new prereg and starts a new, later `frozen_utc`. Nothing below
reports a result. No labelled row has been scored against this file.

## Question

Should nimble:9b (via `LLM_ROUTER_CLASSIFIER_BACKEND=systemone`, `decision_classifier`) be adopted
as the L2 classifier layer? It runs in proxy shadow (`LLM_ROUTER_LOCAL_CLASSIFIER=shadow`) while the
rules keep authority. The adoption test is the one the owner fixed on 2026-10-08.

## Data

* **Rows.** `classifier_shadow.jsonl` records (`kind == classifier_shadow`) written by
  `proxy/llm_shadow`, one per turn-first call, each carrying the tier the client really
  requested. Only records with `ts >= frozen_utc` (see the `.sha256` file) are eligible. Rows
  written before the freeze may not be scored for this decision, because the analysis rule did
  not exist when they were written.
* **Population.** Organic sessions only (statistics rule 8: executor, judge, reviewer and smoke
  sessions are never organic). Research sessions are excluded here; if they were added the
  result is labelled "proxy population" (rule 10) and does not count for adoption.
* **One classifier model per score.** A backend switch inside the window never mixes two models
  into one number (`shadow_eval.score_by_model`).
* **Truth labels.** Written by the daily Opus-rubric labeller from `shadow_text.jsonl` samples
  (`LLM_ROUTER_SHADOW_TEXT_SAMPLE`), as `{text_sha, session_id, truth}` in
  `LLM_ROUTER_SHADOW_LABELS`. The labeller sees the prompt and context, never the nimble or the
  rules verdict (blind). A prompt it cannot label is skipped, not guessed.

## Arms and metrics (PREREGISTRATION.v2 sections 1.2-1.4, implemented in `llm_router.shadow_eval`)

* `rules_eff`: the rules' proposed tier, Haiku lifted to Sonnet unless Haiku was really served.
* `llm`: nimble's tier; the rules_eff tier when its verdict is unusable (timeout, parse error,
  cold model, abstain). `n_fallback` is reported.
* Cost c(haiku) = 1.0, c(sonnet) = 2.7, c(opus) = 6.15. `C-lambda2` = mean of
  c(pred) + 2 c(truth) when pred < truth, else c(pred). **M1-12** is `C-lambda2-clamp`: the
  prediction is first clamped to the tier the client really requested.
* Under-route = share of labelled turns with pred below truth.

## Decision rule (fixed before the first scored row)

Adopt nimble:9b as L2 only if ALL of these hold on one scoring run:

1. At least 100 labelled organic turn-first rows (`n_labeled >= 100`).
2. No session holds more than 60% of those labelled rows (statistics rule 4). Otherwise the result is
   "not informative"; extend the window once, then ask the owner.
3. `C-lambda2-clamp(llm) <= C-lambda2-clamp(rules_eff)`.
4. `under(llm) <= under(rules_eff)` (point estimates of the under-route rate).
5. Precondition P1.7-c passed (`p17c_gate.json`; harness `scripts/bench_shadow_continuation.py`).

The comparison is paired: each labelled turn is scored by both arms. The uncertainty is a
session-clustered paired bootstrap of `C-lambda2-clamp(llm) - C-lambda2-clamp(rules_eff)`:
**B = 2,000 resamples, seed = 20261003** (`shadow_eval.BOOT_B`, `shadow_eval.BOOT_SEED`). The CI is
printed; the verdict uses the point estimates above, as statistics rule 9 allows at this volume.
With fewer than 30 sessions both the wide and the normal interval are printed (rule 1).
Under-route rates print with Wilson 95% intervals.

## Not allowed

* Changing a bar, the seed, the cost table or the eligibility cutoff after the first scored row.
* Re-running with another seed or window after seeing a result.
* Scoring rows with `ts < frozen_utc`, or labelled by anything that saw a verdict.
* Calling a result a pass when any of conditions 1-5 is unmet or "not informative".

## Pending

All of it. Volume is the limit (8 organic turn-first rows in the W0 week, PLAN section 1.4 rule 4),
so reaching 100 labelled rows will take weeks; no date is promised.
