# llm-router — adversarial audit, executive summary

**HEAD:** v14.1.0 (`032b473`, published to PyPI and npm) · **Date:** 2026-09-21
**Method:** ten parallel domain audits. Every CRITICAL and HIGH finding was
independently re-verified by the summary author before inclusion.

---

## Overall state

**The router routes. Almost nothing it says about itself survives checking.**

The execution path is real: prompts are classified, models chosen, fallback
chains run, local models preferred, answers returned. That is not in question.

What is in question is every number and guarantee layered on top. The measurement
system is not merely inaccurate — it is *structurally incapable* of recording
what it reports on. The quality ledger cannot record a failure. The savings
figure is computed over a population that is majority test fixtures. The cache
can answer the wrong question. The provenance mechanism added to fix
contamination is called by almost nothing.

**A working router with a reporting layer that should not be trusted, published,
or built upon until repaired.**

---

## What genuinely works

1. **The routing hot path** — `route_and_call` and its fallback chain; 99% of
   chosen-vs-final divergence is explained.
2. **Local-first behaviour** — real and measurable.
3. **The canonical scrubber** for the stores that call it (`result_cache`,
   `semantic_cache`, `session_store`, `context`, `envelope`, `prompt_capture`).
4. **`result_cache` hygiene** — `secure_delete=ON`, VACUUM after purge, 0600
   repair on the db and WAL sidecars. Reference quality.
5. **`budget_backend.py`** — correct cross-process design. Use as the model.
6. **JSONL append atomicity** — 2,000 concurrent appends, zero corruption.
7. **Truncated-file survival** — all three readers handle a mid-write kill.
8. **`env_registry.py`** — hand-maintained with a non-circular validation test
   that guards against the "0 failures because nothing was scanned" trap.
9. **The test suite's mutation resistance** — 6 of 6 deliberate defects caught.
10. **Ground Truth's conservative core** — three-state outcomes, hard/soft split
    honoured where the label is emitted, and **no generative step** in verifier
    authoring (so correlated-LLM-failure does not apply).

---

## Top 10 risks

| # | Risk | Severity |
|---|---|---|
| 1 | **The quality ledger cannot record a failed route.** 0 of 16,869 real rows have `route_succeeded=False`; `record_route()` is only called from `_finalize_successful_route`. Every success rate is 100% by construction | CRITICAL |
| 2 | **Savings are the wrong sign.** +$83.49 raw → +$87.96 name-filtered → **−$1.15** with fixtures removed. 1,813 stub rows wear real model names | CRITICAL |
| 3 | **Semantic cache answers different questions.** Measured: "retry 3" vs "retry 30" = **0.9925** cosine, threshold 0.95. Model never called. No bypass, no clear tool | CRITICAL |
| 4 | **Raw secrets reach disk.** `trace.py` and `tool_intercept.py` have **zero** scrubber references. `intercepts.jsonl` is mode 644, live, no TTL | CRITICAL |
| 5 | **Provenance computed then ignored.** Four mechanisms; three broken. `is_evaluable()` has one consumer; `attribution.py` has zero | HIGH |
| 6 | **README hero stat is a splice** — "105 prompts, 76%/72%" combines three cells; no run produced it | HIGH |
| 7 | **Gateway drops all tool definitions** and hardcodes `finish_reason: "stop"` | HIGH |
| 8 | **`classification_method` 0% populated** — writer reads `.get("method")`, builders write `"classifier_type"` | HIGH |
| 9 | **Concurrent corruption** — `quota_tracker` 32–38% read failure (10 hooks consume it); `Pool.admit` loses increments (19 vs 21) | HIGH |
| 10 | **A shipped module cannot be imported** — `control_plane.api` raises `ImportError` | MEDIUM |

---

## Top 10 strengths

1. Routing hot path correct and well-commented.
2. Canonical scrubber genuinely unified where adopted.
3. `result_cache` secure-delete/VACUUM/permissions.
4. `budget_backend` cross-process correctness.
5. Append atomicity under real concurrency.
6. Readers survive truncated files.
7. `env_registry` with non-circular validation.
8. **6/6 mutation probes caught** — safety-critical logic is genuinely defended.
9. Ground Truth's AMBIGUOUS state and label-withholding.
10. A culture that documents its own past incidents. Most failures below are a
    known lesson not carried to a sibling.

---

## Ready for wider real-world usage?

**As a router: yes, with caveats** — but disable or retune the semantic cache
first.

**As a measurement platform: no.** Publishing any figure today publishes a number
about test fixtures.

**As a dependency for others: not yet.** `llm-router status` — the flagship
savings command — crashes on a clean `pip install` (`ModuleNotFoundError: rich`),
flagged three weeks ago. `health` and `gain` are documented and absent. Two
documented host integrations are rejected by the installer.

---

## The biggest blocker to trustworthy router evaluation

**Not the Ground Truth system. The ledger underneath it.**

Ground Truth reads a ledger that cannot represent failure, whose provenance field
almost no consumer reads, whose `classification_method` is universally empty, and
which excludes cache hits. **97% of routes are `completion`-kind and not one is
verified.**

Fixing the Ground Truth pipeline further is premature. The instrument has to be
able to record a failure first.
