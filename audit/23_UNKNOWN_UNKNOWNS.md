# Phase 49 — Unknown unknowns

> What did forty-seven phases not think to look for?

Phases 1-47 each had a subject: routing quality, cost, telemetry, security,
concurrency. A phase finds what it is pointed at. This one asks what no phase
was pointed at, and the answer is organised by the SHAPE of the blind spot
rather than by subsystem — because a shape recurs and a subsystem does not.

Everything here was found DURING the remediation or the clean-room run, which
is itself the finding: these classes were invisible to a reading audit and
became obvious the moment something was built, run, or released.

---

## U-01 — Nobody asked whether the observer changes the observed

**Severity: HIGH. Confidence: CONFIRMED, reproduced on the published artifact.**

Every telemetry phase asked "is this number written correctly?" and "does
anyone read it?". No phase asked **"does reading it change it?"**

    $ llm-router doctor --audit   # three runs, same state, read-only command
    fail_open_events: 4
    fail_open_events: 8
    fail_open_events: 12

The diagnostic that reports the fail-open counter generates four fail-open
events per invocation. An operator investigating a high number makes it higher.

This is a CLASS, not an instance. Any counter incremented on a path a
diagnostic also walks has it. Candidates nobody has checked:
`coverage.record_unobserved` (does rendering the coverage report classify
anything?), `session_store.lock_timeout_count` (does the status bar take the
lock?), `prompt_capture.counters` (does `status()` itself assess a candidate?).

(The published figures affected are `fail_open_events: 713 / 531` in the R12
commit and `REMEDIATION_RUN.md`.)

The general rule the repo already has for reads — `orphan_count()` deliberately
does not reap, and R9's test pins it — was applied to exactly one counter and
never generalised.

## U-02 — Nobody asked what the counters MEAN, only whether they are read

**Severity: HIGH. Confidence: CONFIRMED.**

R12's claim is "every counter has a reader", and it is true and enforced. The
question one layer down was never asked: **is the thing being counted actually
a degradation?**

`CHZ-FO-COST-MIGRATE-ALTER` fires twice per database open because two
`ALTER TABLE … ADD COLUMN is_simulated` statements hit `duplicate column name`.
That is an idempotent migration doing exactly what it should. It is counted as
a swallowed failure, and it is the single loudest code in the counter.

A counter whose baseline is "normal operation" cannot signal abnormal
operation. Phase 15-16 (telemetry trust) verified the writes were correct and
durable; nothing verified the SEMANTICS.

## U-03 — Nobody audited the enforcement layers against each other

**Severity: MEDIUM. Confidence: CONFIRMED, observed live.**

Two layers decide whether a prompt is routed, and during this very session they
disagreed on the same prompt:

  * **UserPromptSubmit** injected: *"CONTEXT-DEPENDENT PROMPT — this references
    your local files / repo / history / state, which a stateless routed model
    cannot see. No blind draft was generated (it would be fabrication)."*
  * **PreToolUse** simultaneously held `Bash` with a HARD routing directive for
    the same turn, classifying it `research/moderate`.

The request was "check this repository's git state and this machine's memory" —
something no external model can perform. One layer knew that and said so; the
other blocked the only tool that could answer it.

Every phase audited routing as ONE decision. It is at least two, in different
processes, with different inputs, and no test compares their verdicts.

## U-04 — The escape valve is expensive, and nobody priced it

**Severity: MEDIUM. Confidence: CONFIRMED, measured.**

The enforcement hook documents an escape: *"Call ANY llm_* tool (even a trivial
`llm(task="query")`) — clears the lock for this turn."*

Taking it, verbatim, with the prompt `"Reply with the single word:
acknowledged"`, routed to **qwen3-coder:30b** — a 30-billion-parameter model
loaded to produce one word. On this machine that model then held **42.6% of
system memory** and OOM-killed three background jobs, including the
pre-release verification.

A cost-saving product whose escape hatch loads a 30B model for a throwaway
query has an escape hatch that costs more than the thing it is escaping. No
phase examined the enforcement mechanism's own resource cost.

## U-05 — Nobody ran the product on its own release

**Severity: MEDIUM. Confidence: CONFIRMED.**

Phase 42-45 installed the PUBLISHED 14.1.0 and found `llm-router status`
crashing on a clean install — the best finding of the audit, and the only one
that came from running a release rather than reading a tree.

That approach was used once. It found, per unit effort, more than any static
phase. Phase 50 repeats it for 15.0.0 and immediately finds two more defects
(U-01's reproduction, and rich markup rendering as literal text).

**The blind spot is methodological:** an audit that reads a repository is
auditing a repository, and users do not install repositories.

## U-06 — Nobody audited the audit's own artifacts for leakage

**Severity: MEDIUM. Confidence: CONFIRMED, caught by tooling not by process.**

An audit specialist wrote the name and filesystem path of one of the operator's
other private projects into `audit/02_PRODUCT_CLAIM_MATRIX.md`. The repository
is public. `scripts/check_identity.py` blocked the commit.

It was caught by a pre-existing gate, not by anything the audit designed. The
K1 generator then reproduced the same class on its first run — printing
`sys.executable`, i.e. `/Users/<name>/…`, into a file destined for the public
repo — and was caught only because I wrote the privacy assertion before
trusting the generator.

**Discovery output is itself a data-handling surface**, and no phase treated it
as one.

## U-07 — Nobody asked what a fix costs the thing it fixes

**Severity: LOW-MEDIUM. Confidence: CONFIRMED.**

R6 made money surfaces fail-closed on provenance. Correct. The consequence was
that **262 of 285 rows** became invisible, because a producer never stamped
provenance — and that producer defect was introduced by the same remediation.

It surfaced only because I checked the operator's ledger for contamination
after an unrelated test. There is no phase, and no test, whose job is "what did
this change make invisible?".

The general shape: a filter that is correct in isolation can be catastrophic in
composition, and correctness review looks at one side of a composition.

---

## What would find these next time

Ordered by the ratio of what they would find to what they cost.

1. **Run the published artifact.** One phase did; it produced the best finding
   in the audit. Every audit should start with `pip install <product>` in a
   clean room and end there too.
2. **Read every counter twice.** If the second read differs from the first with
   no work in between, the instrument is measuring itself.
3. **Ask what each counter's ZERO means.** Not "is it read" but "is a non-zero
   value actually bad, and is zero actually achievable in normal operation?"
4. **Diff the layers.** Where two mechanisms decide the same thing, run both on
   the same input and compare. Nobody had ever done this for the routing
   decision.
5. **Price the escape hatches.** Every mechanism with a documented bypass
   should have the bypass's cost measured, because it is the path taken under
   pressure.
6. **Treat audit output as a data-handling surface** and run the repository's
   own secret and identity gates over it before committing.
