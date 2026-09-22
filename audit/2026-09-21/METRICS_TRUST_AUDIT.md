# Metrics trust audit

v14.1.0 · 2026-09-21. For each number the project reports: what it claims, what
it actually measures, and whether it can be trusted today.

The question this document answers is not "is the number right?" It is **"can
this number be wrong in a way the system would not notice?"**

---

## The four provenance schemes

Every trust question below reduces to one of four mechanisms deciding "does this
row count?" Three of them do not work.

| Mechanism | Where | Status |
|---|---|---|
| `synthetic` + `is_evaluable()` | `routing_quality.py` | **Correct.** Fails closed on rows predating the field. **1 consumer** — not `summarize()` |
| `is_simulated` | `usage.db` | **Never written.** The `INSERT INTO usage` omits the column; the filter `AND is_simulated IS NOT 1` excludes nothing, ever |
| `is_real` | `usage.db` | Defaults to 1, maintained heuristically. Drives the session-end panel users actually see |
| `_is_test_model()` | `cost.py` | Name matching. Blind to 1,813 fixture rows wearing real model names |

And `attribution.py`, written expressly to end this fragmentation — *"one
definition, consumed by every surface"* — has **zero production callers**.

**Nothing that produces a user-visible number uses the mechanism that works.**

---

## Savings

**Claimed:** money saved by routing to a cheaper model.
**Reported today:** +$83.49.
**Actually measured:** the cost delta of a population that is majority test
fixtures.

| Filter applied | Result |
|---|---|
| None (as reported) | +$83.49 |
| `_is_test_model()` — the code's own filter | **+$87.96** |
| Stub-signature and fixture-provenance removal | **−$1.15** |

**Trust: NO.** Three separate reasons, any one of which is disqualifying:

1. The sign is wrong. The honest figure is a small net loss.
2. The project's own filter makes the number *worse*, moving it +$4.47 further
   from truth. It is anti-protective, and its existence creates false confidence.
3. 1,813 fixture rows carry real model names, so no name-based filter can ever
   fix this. The fix has to be provenance at write time.

**Contaminating populations found:** 1,813 stub-signature rows under real model
names; 11 rows whose error text contains `test-key`; 733 rows on `badmodel` /
`ollama/b`.

**What it would take to trust:** write provenance at insert time (the
`routing_quality.py` `synthetic` pattern, applied to `usage.db`), then recompute.
Until then the number cannot be corrected — only recollected.

---

## Routing success rate / quality escalation rate

**Claimed:** how often routing succeeds, and how often it must escalate.
**Actually measured:** nothing. The metric has one representable state.

| Check | Result |
|---|---|
| Rows with `route_succeeded=False` | **0 of 16,869** |
| Call sites of `record_route()` | **1**, at `router.py:2004`, inside `_finalize_successful_route` |
| Failure path destination | `_emit_ledger_terminal('failed')` → execution ledger (SQLite), never the quality ledger |
| Cache hits | Excluded by the gate at `router.py:1958` |

**Trust: NO — and this is the audit's most important single result.**

A reader cannot distinguish "nothing failed" from "failure is unrepresentable".
Any success rate computed here is 100% by construction. Any escalation rate is
computed over survivors only.

Additionally, `summarize()` contains **0** references to `synthetic` or
`is_evaluable`. Demonstrated in an isolated ledger: one synthetic row alone
yields `quality_escalation_rate: 1.0`.

**Denominator warning.** Two of three terminal outcomes are absent from this
file. This is the same class of error the repo's own CLAUDE.md documents twice —
"a filter that drops nothing has not been shown to work", "a rate without its
denominator is not a measurement" — reappearing one layer up, in the store rather
than in the parser.

---

## Classification method attribution

**Claimed:** each row records *why* a model was chosen.
**Actually measured:** nothing. **0 of 23,773** rows populated.

**Cause:** the writer reads `.get("method")`; every builder writes
`"classifier_type"`. Silent on both sides — `.get()` returns `None` and the row
is written anyway.

**Trust: NO.** Any analysis of "which classifier routes best" is currently
impossible, and would return an empty result that reads as "no difference found".

---

## Verification rate

**Claimed:** routed answers are verified.
**Actually measured:** `verification_attempted` is false on **0 of 23,323**
completion-kind routes — 97% of all traffic.

**Trust: N/A — there is nothing to trust.** This is the input population Ground
Truth samples from.

---

## Fallback attribution

**Claimed:** when a fallback happens, the reason is recorded.
**Actually measured:** 19.3% of real rows show `chosen_model ≠ final_model`.
**3,217 carry a reason. 34 carry `fallback_occurred=False` and
`fallback_reason=None`.**

**Trust: MOSTLY YES (99%).** The 34 are a real gap — the model that ran differs
from the one recorded as chosen with no persisted explanation — but the
mechanism works and the error rate is bounded and known.

This is the only metric in this document whose failure mode is a small known
residue rather than a structural absence.

---

## README "105 prompts / 76% / 72%"

**Claimed:** a single measured result.
**Actually:** three cells from three different rows of `docs/MEASUREMENT.md` —
different N, different windows, different conditions — combined into one triple.

**Trust: NO.** No run produced this number.

Note the asymmetry worth preserving: **`docs/MEASUREMENT.md` itself is honest**,
carrying N and conditions per row, exactly as the house rule requires. The
failure is in the summarisation for the README, not in the measurement.

---

## Routing rate from `auto-route-debug.log`

**Trust: YES, via `scripts/routing_rate.py` only.**

The known trap — test invocations writing `session_id=unknown` into the
production log, 1,037 of 1,938 entries on 2026-08-31 — is documented and the
correct parser exists. The rule holds: do not write another ad-hoc parser.

This is the one measurement area where a past denominator failure was found,
understood, written down, **and** the fix was adopted by the consumer.

---

## Interception coverage

**Trust: YES, as defined.** Bytes-of-output-avoided, quoted after the
compression gate. The definition correctly distinguishes eligibility from
interception (26 commands pass the allowlist; 9 actually intercept).

The unrelated privacy problem stands: `intercepts.jsonl` is mode 644, unscrubbed,
no TTL (C-04).

---

## Ground Truth candidate counts

**Actually measured:** the production pool is **empty**. Accumulation was enabled
during this session; no row exists yet.

Two known undercounts when it does fill:
- `Pool.admit()` loses increments under concurrency — 19 recorded where 21
  occurred, and `accumulate.py` constructs a fresh `Pool()` per call.
- `accumulate_report.py::_runtime_outcomes` has two bare
  `except Exception: return {}`, so a broken read is indistinguishable from
  "no data yet" — the exact ambiguity a report of zero must not have.

**Trust: NOT YET APPLICABLE**, and when it applies, counts will run low.

---

## Summary

| Metric | Trust | Failure mode |
|---|---|---|
| Savings | **NO** | Wrong sign; the protective filter is anti-protective |
| Routing success rate | **NO** | Failure is unrepresentable — 0 of 16,869 |
| Quality escalation rate | **NO** | Same population; provenance ignored by `summarize()` |
| `classification_method` | **NO** | Key mismatch, 0% populated, silent both sides |
| Verification rate | **N/A** | 0 of 23,323 |
| Fallback attribution | **MOSTLY YES** | 34 unexplained of 3,251 |
| README hero stat | **NO** | Spliced from three runs |
| Routing rate (via the script) | **YES** | Known trap, documented, fix adopted |
| Interception coverage | **YES** | Definition is correct |
| GT candidate counts | **N/A** | Pool empty; will undercount when it fills |

---

## The single sentence

**Every user-visible number in this project is produced by a code path that does
not use the one provenance mechanism that works, and the headline quality metric
is computed over a store in which failure cannot be written down.**

The corollary matters for sequencing: these numbers cannot be *corrected*,
because the contaminating rows are not separable after the fact. They have to be
**recollected** with provenance stamped at write time — which is why C-01 and
C-02 gate everything downstream of them, including Ground Truth.
