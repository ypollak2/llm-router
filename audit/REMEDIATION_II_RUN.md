# Remediation II — run state

Live file. Updated after every task, not at the end.

| ID | Status | Task | Gate (observable) |
|---|---|---|---|
| S1 | **done** (v15.0.1) | Reading a counter does not change it | 3 reads identical, all 7 counters |
| S2 | **done** (v15.0.1) | A counter's zero is reachable | fresh store/process reads 0 |
| S5 | **done** (v15.0.1) | Audit output carries no identity | 83 leaks redacted, scan enforced |
| S6 | **done** (v15.0.1) | One migration per column | 0 fail-opens across 3 DB opens |
| S7 | **done** (v15.0.1) | Rendered output is rendered | 0 markup tokens in output |
| C1 | **done** | **Correct the published S4 claim** | every copy of the false claim gone |
| S4a | **done** | Paid-model latency/cost are placeholders | >1 distinct value per paid model |
| S4b | **done** | Latency term in the reward | delta measured before default flips |
| S3b | `todo` | Context-dependence detector over-fires | the xfail XPASSes, honestly |
| S8 | `todo` | Word order changes the route | the K4 xfail XPASSes, honestly |

## C1 — what was wrong

I asserted, in four audit documents, one test docstring, one commit message and
the **published 15.0.1 CHANGELOG**, that the routing reward means:

> "pick the largest free model is exactly what this formula maximises"

**That is false.** Measured on `routing_decisions`, the table the bandit
actually reads:

    final_model                   n   succ    avg_$   avg_ms        EV
    openai/gpt-4o              1057  1.000  0.01000      500   0.04000
    anthropic/claude-opus       330  1.000  0.01000      500   0.04000
    ollama/lfm2.5:8b             99  1.000  0.00000    10111   0.05000  <- wins
    ollama/qwen3-coder:30b       95  0.926  0.00000    13674   0.04632

The **8B** model wins, not the 30B. The cost term does vanish for free models,
so success rate decides — and here that favours the SMALLER one. My prediction
was the opposite of the behaviour.

Two further errors in the same analysis:

* I read the `usage` table (286 rows, all `success=1`, all cost 0) and
  concluded the reward was a constant. **The bandit does not read that table.**
  It reads `routing_decisions`, where success is honest (7 failures in 1600).
* I wrote that `avg_latency_ms` is "collected and unread". It is already
  `SELECT`ed in `aggregate_stats`'s query — collected, surfaced, and unused by
  `expected_value` only.

## S4a — the finding that survived

    openai/gpt-4o           latency: 1 distinct [500.0..500.0]   cost: 1 distinct [0.01..0.01]
    anthropic/claude-opus   latency: 1 distinct [500.0..500.0]   cost: 1 distinct [0.01..0.01]
    ollama/lfm2.5:8b        latency: 99 distinct [1384..55437]   cost: 1 distinct [0.0..0.0]

**1,387 paid-model rows carry identical placeholder values** — exactly 500 ms
and exactly $0.01. Not measurements. The local model has 99 distinct real
latencies.

So the reward compares MEASURED local models against CONSTANT-STAMPED paid
ones. Adding a latency term now would let a 500 ms placeholder beat a
10-second measurement — optimising a recording defect rather than a model.

**S4a before S4b, without exception.**


## C1 — done

Corrected in `tests/test_s4_free_is_not_costless.py` (docstring and fixtures),
`audit/23_UNKNOWN_UNKNOWNS.md`, `audit/25_REMEDIATION_PLAN_2.md`, and
`CHANGELOG.md` — the last annotated in place rather than silently edited,
because 15.0.1 is published and the wrong sentence is in what people have.

## S4a — done, and the mechanism already existed

`routing_decisions` has a `provenance TEXT` column, written by
`_write_provenance()` at insert time, deliberately with NO DEFAULT. cost.py's
own docstring explains why, and indicts the older `is_real INTEGER DEFAULT 1`
for "asserting the very thing it should be recording".

It separates the two populations perfectly:

    NULL      1387 rows, 1 distinct latency   (500ms / $0.01 placeholders, Jul-Aug)
    runtime    214 rows, 214 distinct         (measured)

**Nothing read it.** All 1601 rows also read `is_real = 1`, including every
placeholder. Built, correct, populated, unused — the CLASS-A shape in the
bandit's only source of evidence.

`aggregate_stats` now filters `provenance = 'runtime'`, fail-closed, the same
rule the money surfaces already apply.

**Delta measured before changing it**, because this touches routing:

    BEFORE  6 models, top pick codex/gpt-5.5 EV 0.05000
    AFTER   4 models, top pick codex/gpt-5.5 EV 0.05000   <- unchanged

The two dropped models were ranked LAST on placeholder data, and neither leaves
routing: `reorder()` explores from `candidates`, not `eligible`, so a model
with no trusted rows becomes under-sampled rather than removed. That property
is now pinned by a test, because the filter made it load-bearing.

**RED-CHECK:** delete the WHERE clause and leave it as a comment ->
`n_samples=26, expected 6`.

The behavioural test nearly shipped SKIPPED (`aggregate_stats` takes no
`db_path`, so the first version skipped itself and left only AST coverage).
Redirected via `LLM_ROUTER_DB_PATH` so it actually runs — the AST test proves
the clause is written, only this one proves it does anything.


## S4b — done, re-scoped by the measurement

S4a made the ledger trustworthy, which changed what S4b should be. On trusted
rows only, THREE models tie at exactly EV=+0.05000 — all free, all 100%
success — and `max()` resolved the tie onto `qwen3.8:latest` at **54.3s** over
`lfm2.5:8b` at **10.1s**, purely by iteration order.

So the defect is not "latency should cost money". It is that the reward ties
and the tie is broken arbitrarily, landing on the slowest.

**Latency is now a TIE-BREAK, not a cost term.** A cost term needs a $/second
rate, and none is justifiable from this data: every trusted row is free or
subscription, so there is no paid/free trade-off to calibrate against. A rate
picked anyway is a guess embedded in routing policy. A tie-break needs no rate
and cannot reorder any pair whose expected values differ — it replaces
"arbitrary" with "faster" only where the reward is silent.

**My first implementation had the bug I have been fixing all session.**
`getattr(s, "avg_latency_ms", 0.0)` makes an unrecorded latency read as
*instantaneous*, so a row with no measurement wins every tie. Unknown rendered
as the most favourable answer — the same shape as unknown provenance counting
as production, an unknown table counting as a missing column, and an unreadable
counter counting as zero. Absent latency now sorts LAST.

Caught by my own test (`test_missing_latency_does_not_crash_or_win`), which I
wrote before looking at whether the implementation handled it.

**RED-CHECK:** revert the tie-break -> 3 tests fail; invert the sign ->
`assert 'ollama/qwen3.8:latest' == 'ollama/lfm2.5:8b'`, i.e. it picks the
54-second model again.

Also updated `test_t09_bandit_reward_is_bounded` to FOLLOW the new indirection
(`key=_rank`) rather than loosen: a named key function that stopped using
`expected_value` would otherwise pass by virtue of being named.

---

## S8 — "word order changes the route"

**The framing was wrong, and the real defect is ~100x bigger than the row that
found it.**

The plan item was: `what is the capital of Portugal?` routes to query,
`tell me what the capital of Portugal is` routes to analyze. I built a
14-pair paraphrase corpus and measured all three policies before touching
anything:

```
before  GATEWAY_POLICY   order-sensitive 10/14   negatives pulled into query 0/8
before  ROUTER_POLICY    order-sensitive  0/14   negatives pulled into query 2/8
before  HOOK_POLICY      order-sensitive  0/14   negatives pulled into query 2/8
```

**Only the gateway is order-sensitive.** The live routing paths are already
0/14 — so the first thing the measurement did was contradict the finding.

Scoring the prompts individually explains why, and it is not word order:

```
what is the capital of Portugal?           {'query': 4}   confident
tell me what the capital of Portugal is    {}             score 0
analyse why the build is failing           {}             score 0
```

They score **zero in every category**. `classify_signals` then falls through to
`policy.low_signal_default` — `"query"` for the hook and router, `"analyze"`
for the gateway. The hook gets the better answer *by luck, not by measurement*,
and the two doors return different task types for the same prompt.

### The denominator

Measured 2026-09-23 over real traffic, `scripts/groundtruth/sources.py` drop
rules applied first (they removed 1389 records: 663 system-noise, 357
synthetic-session, 145 too-short, 112 harness-artefact, 102 benchmark-sandbox):

| | n=1571 | share |
|---|---|---|
| score == 0 — nothing scored at all | 651 | **41.4%** |
| weak (0 < score < 2) | 132 | 8.4% |
| decided by `low_signal_default`, not by a score | 783 | **49.8%** |
| gateway and hook return a **different** task_type | 783 | **49.8%** |

Half of all real traffic is routed by a default. The K4 row was one visible
instance of it.

### The CLASS-A part

`ClassifySignal.confident` has recorded this since the day it was added.
`grep -rn --include='*.py' '\.confident' src scripts` outside `classify.py`
returns **nothing**. The number that says "I did not classify this" was written
for nobody — exactly the shape R12 exists to stop, found in a ninth place.

### What was fixed, and what was deliberately not

**Fixed:** `classify.low_signal_classifications()` returns
`(decided_by_default, total)` — numerator *with* its denominator, because 12
fall-throughs is a healthy hook and a catastrophe in a gateway that served
12,000 requests. Registered in `counter_registry.REGISTRY` and rendered by
`llm-router doctor`, alarming on the **share** (≥25%), and reading `0 of 0`
returns `value=None`, not a clean 0%.

**Not fixed: the route itself.** Two candidate fixes were measured and both
were refused on the evidence:

1. *Add `(?:tell|show) me (?:what|how|who|…)` to the query intent pattern.*
   Measured: gateway order-sensitivity 10/14 → 2/14, but negatives pulled into
   query went 0/8 → 2/8 on the gateway and 2/8 → **4/8** on router and hook.
   `tell me what went wrong in the deployment and propose a fix` and
   `tell me how the retry logic in gateway.py is currently implemented` both
   became `query`. It trades one defect for two.

2. *Change the gateway's `low_signal_default` to `"query"` so the doors agree.*
   This re-routes ~half of gateway traffic on no evidence that `query` is the
   right answer for it. Per CLAUDE.md, a proxy split has already misled this
   project by 4.25 points and a one-point calibration is a guess.

Both need a labelled set on the target distribution, which does not exist.
**PARKED**, with the measurement recorded so the next attempt starts from a
number rather than from the pair that started this.

The K4 `xfail(strict=True)` therefore **stays** — the route is unchanged. Its
docstring was corrected: it claimed "the fallback is the MORE expensive tier"
as a property of *the classifier*, which is true only at the gateway. The hook
and router fall to `query`, the cheaper tier, for the same prompt.

**RED-CHECK** (K7, narrowest mutation, needle left in a comment each time):

| mutation | result |
|---|---|
| drop `_low_signal_classifications += 1` | RED — `assert 0 == 4` |
| make the reader reset on read (S1) | RED — `assert (0, 0) == (1, 1)` |
| `0 of 0` returns `0.0` instead of `None` | RED — `assert 0.0 is None` |
| `alarming=False` instead of `share >= 0.25` | RED — `assert False is True` |

---

## S9 — a confidence that was never recorded is not a confidence of zero

**Found while measuring S8**, not by the plan. S8 needed a cross-process source
for the low-signal rate, which led to `routing_decisions.classifier_confidence`.

Measured on the live ledger:

```
classifier_confidence  classifier_type   rows
        0.9            heuristic         1387    <- all provenance=NULL
        NULL           unhinted           213    <- provenance='runtime'
        0.0            heuristic            1    <- provenance='runtime'
```

The 1387 rows carrying 0.9 are exactly the pre-provenance placeholder rows S4a
already excludes. On rows with **trusted** provenance the column is NULL for
**213 of 214**.

`retrospective.py` read it as:

```python
conf = d.get("classifier_confidence", 0) or 0
if conf < 0.70:
    gap_flags.append("LOW_CONFIDENCE")
    reason = f"classifier confidence only {conf*100:.0f}%"
```

A NULL becomes `0.0`, which is below every threshold. `classify_root_causes`
then promotes `LOW_CONFIDENCE` to a `CLASSIFIER_ERROR` root cause at confidence
**2 ("High")** with the evidence string **"Classifier confidence 0%"**.

So on the current ledger the retrospective would report 213 decisions as
high-confidence classifier errors, with a quoted percentage, entirely from a
column nobody ever wrote. *Unknown rendered as the unfavourable answer* — the
mirror image of every other instance this audit found, where unknown rendered
as the favourable one. The direction changes; the defect does not.

`analyze_facts` had the same coercion, averaging NULLs in as zeros, so
"avg confidence" was approximately *the share of rows that recorded one* —
a statistic about schema coverage wearing the name of a quality measurement.

**Fixed:** a NULL gets its own `CONFIDENCE_UNMEASURED` flag and is never
LOW_CONFIDENCE; `avg_confidence` is computed over the decisions that have one
and now travels with `confidence_measured` / `confidence_unmeasured`; the
report line prints the denominator.

A genuine `0.0` is still LOW_CONFIDENCE — the fix must not buy its way out by
flagging nothing, and `test_a_measured_zero_is_still_low_confidence` pins that.

**RED-CHECK** (narrowest mutation, needle left in a comment each time):

| mutation | result |
|---|---|
| restore `d.get(..., 0) or 0` in `analyze_gaps` | RED — *"a column nobody wrote is being reported as a measured low confidence"* |
| restore the exact pre-fix expression in `analyze_facts` | RED — `assert 0.26666666666666666 == 0.8` |
| report `len(decisions)` as the measured count | RED — *"a mean with nothing behind it must say so"* |

The second mutation was run twice. The first attempt deleted the `is not None`
filter, which made `float(None)` raise — red, but on a `TypeError`, proving
nothing about the original defect. Re-run with the exact pre-fix expression it
fails on the value, `0.2667` against `0.8`, which is the defect itself.

### S9 follow-up — the class, sized but not swept

`grep -rnE '\bor 0(\.0)?\b' src/llm_router` returns **217** matches. They are
not all defects: `or 0` inside a sum of token counts is usually harmless.

The discriminator is what happens to the value next:

* **Summed** — mostly benign; the loss is silent under-counting.
* **Compared to a threshold** — S9's shape. Absent becomes a finding.
* **Averaged, or used as a denominator** — the other half of S9. Absent
  becomes a confident number nothing measured.

Candidates found by inspection but **not** swept in this pass:
`routing_quality.py:641-642` (`actual_usd` / `baseline_usd` into a ledger
record), `claude_usage.py:389-390` (credit limits), `surface_status.py:280`
(`estimated_saved`). None of them is a live threshold comparison in the code
read, so none met the S9 bar.

**PARKED** as a plan item rather than fixed here: a 217-site sweep is not a
remediation, it is a refactor, and a refactor buried in a fix is two changes
wearing one commit. The right shape is a lint — `scripts/lint_savings_sign.py`
is the working precedent — that flags `or 0` / `.get(k, 0)` whose result
reaches a comparison or a mean, with an allowlist for the sum sites. That is
its own task with its own red-check.

### S8 limitation, stated rather than discovered later

The low-signal counter is **in-process**, and `llm-router doctor` classifies
nothing. So on a real machine `doctor` renders:

```
low_signal_classifications: Unknown (no prompt classified in this process yet)
```

That is honest — it is genuinely the truth about doctor's own process, and it
is not a fake 0% — but it means the counter is load-bearing only where the
writer and the reader share a process: the hook (one process per invocation, so
a reading is about the very invocation being examined) and the gateway (a
long-lived server, which is also the door whose default is the expensive one).
`doctor` gets the registry entry and the rendering path, not a number.

This is a weaker guarantee than the other seven counters and it is written down
here because "the counter has a reader" was very nearly satisfied on a
technicality — the CLASS-A defect R12 exists to prevent, one level up.

**What would actually close it:** the hook already logs one line per invocation
to `auto-route-debug.log`, and `routing_report.unterminated_invocations()` is
the working precedent for computing a rate over that log across processes.
Logging the confident/score outcome on the existing `prompt_len=` line would
give `doctor` a real cross-process number at the cost of no new I/O. Not done
here: it changes the live hook, which is the one component the shared
classifier deliberately does not touch (Option B), and it belongs with the
consolidation of the hook's duplicated classifier rather than bolted on before
it. **PARKED** as a plan item.
