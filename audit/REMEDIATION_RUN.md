# REMEDIATION RUN — live state

Plan: `audit/22_REMEDIATION_PLAN.md`. Base: `5f90f24`.
Update after EVERY task. This file survives compaction; memory does not.

## Decisions taken (operator, 2026-09-22)
* **R3 = honesty now, containment later.** Rewrite the claim; do not build a
  sandbox this round. Containment becomes a tracked follow-up.
* **R8 = default capture ON with explicit consent at install.** The consent
  flow is load-bearing — a silent default flip is a privacy regression, not a
  fix. No capture without a recorded, informed opt-in.

## Rule for every task
Not done until its **red-check** has been OBSERVED FAILING — using the
narrowest mutation that reintroduces the bug, not a whole-file revert.
(A-10: a whole-file revert removes a pinned string, so a substring test goes
red and looks sound.)

| ID | Status | Task | Red-check (must be observed failing) |
|---|---|---|---|
| R11 | **done** | One canonical source per concept, enforced by AST scan; fixes cost.py `'google'` | add `Path.home()/".llm-router"` anywhere -> scan fails naming it |
| R13 | **done** | Ban source-text assertions; convert to behavioural/AST | apply the A-10 comment-evasion -> converted test fails |
| R1 | `done` | Scrub at the write boundary; 0600 at creation | remove scrub from ONE writer -> canary scan names it |
| R2 | `done` | Nothing leaves the machine unscrubbed; add DSN pattern | remove webhook scrub -> wire-body test fails |
| R4 | `done` | `agent_loop.run_command` env allowlist + enumerating test | revert one call site -> enumeration names it |
| R12 | `done` | Every counter has a reader, enforced | add a `record()` with no reader -> test names it |
| R14 | `done` | `attempt_log._rotate` locking | remove lock -> 8-proc barrier loses records |
| R15 | `done` | Inspect `finish_reason` | force `content_filter` -> recorded, success False |
| R6 | `partial` | One savings number across all surfaces | bypass the accessor in one surface -> test names it |
| R7 | `partial` | Label every money figure; net not gross | strip one label -> test fails |
| R9 | `partial` | Hook death visible (start marker + doctor rate) | inject 70s sleep -> doctor reports a kill |
| R10 | `partial` | Refuse unservable capability (tools/vision/schema/context) | drop refusal from one endpoint -> parametrised test names it |
| R3 | `done` | SECURITY.md honesty + interpreter corpus | add an interpreter to the allowlist -> corpus test fails |
| R8 | `partial` | Capture ON behind explicit install consent | consent absent -> capture stays off |
| R16 | `done` (finding partly INVALIDATED) | `capture()` sees cwd/tools, or scope the claim | a repo task reaches a frozen dataset, or docs say it cannot |
| R17 | `done` | Validator must discriminate | `len(answer)>5` -> capped at LOW |
| R5 | `todo` | Release the fixed HEAD | remove `rich` -> clean-room job fails |
| K1-K7 | **done** | Audit-readiness kit | see below; each red-checked |

## Sequencing rationale
P2 (R11, R13) runs FIRST: both change how every later fix and test is written.
Doing P1 first means writing it twice. R1/R2/R4 follow immediately — they are
active harm, not theoretical.

## Log
_(appended after each task)_

## Log

### R11 — done 2026-09-22
Found instances **10, 11, 12** of the path class, all escaping into the real
home: `direct_diagnostics._samples_path`, `seats.seats_path`,
`quality_feedback._loophole_jsonl_path`. Fixed `cost.py` to import
`GOOGLE_PROVIDERS` (google now reaches the ledger; nonsense-provider still
rejected). AST scan + reasoned allowlist now fail on any new instance.

Two things the work surfaced that the audit had not:

* `paths.py:5` documents this defect as fixed. It was not — `default_factory`
  runs at instance construction and `get_config()` is a process-lifetime
  singleton, so the MONEY DB path still froze. Reproduced, then converted to a
  property that re-resolves per access.
* **I broke the ledger writer and caught it in ten minutes.** Fixing the freeze
  made `db_path == state_path("usage.db")` always true, so
  `_refuse_unisolated_test_write` began silently refusing EVERY test write. A
  guard that blocks everything fails in the direction that looks like passing
  tests. It now compares against the operator's real home, where "production"
  means something. Three arms verified: isolated allowed, real-DB refused,
  tmp allowed.

Red-checks observed failing (narrowest mutation, per the plan's rule):
hand-copied resolver reintroduced -> scan names file and line; `cost.py`
reverted to hand-listing -> the Gemini row does not reach the ledger.

### R13 — partial, and scoped honestly
Converted the 2 assertions the audit PROVED evadable (`test_t08`) to AST
assertions via new `tests/_ast_assert.py`.

**Red-check: the exact comment evasion that let 23 tests pass this morning now
FAILS** — `router's bandit feed no longer reads quality_degraded (searched the
AST, so a mention in a comment does not count)`.

62 source-text assertions remain. Converting all 64 mechanically would be
wrong: many are genuinely structural ("this migration is in the migration
list") with no behavioural equivalent, and a forced conversion would produce
weaker tests, not stronger ones. A ratchet caps the count; a unit test proves
the AST helper rejects a commented-out call and accepts the real one.

**Not claimed as complete.** Remaining conversions are tracked work.

## PARKED — P-05 · logging-config pollution across test files

**Found while gating R11. NOT caused by R11 — reproduces identically at
`5f90f24`, before any of this work.** A new finding the audit missed.

```
pytest tests/test_calibration.py                      -> PASS
pytest tests/test_built_artifact_is_complete.py \
       tests/test_calibration.py                      -> FAIL
```

```
E   AttributeError: 'LogRecord' object has no attribute 'message'
```

Bisected to `tests/test_built_artifact_is_complete.py` as the polluter (present
in the failing window, absent from the passing one). The error is raised inside
the **logging pipeline**, not the assertion: something installs a structlog
`ProcessorFormatter` on the root logger that expects `record.message`, and
`caplog`'s handler then formats through it. Changing the assertion to
`getMessage()` does NOT fix it, which is how we know the fault is upstream of
the test.

### Why it matters beyond one test
Same class as A-07: a test that passes alone and fails in company. The
`_no_module_state_leak` guard added in F01-F06 catches module-attribute stubs
but **not logging reconfiguration**, so this class of pollution is currently
undetected. That guard's scope is narrower than its name suggests.

### Why parked rather than fixed
Restoring logging configuration correctly is its own change with its own
blast radius (every test that asserts on logs), and it is not R11. Fixing it
inside an R11 commit would hide a distinct defect inside an unrelated diff —
the exact habit this audit was convened to stop.

### Unblocks
Extend the isolation fixture to snapshot and restore `logging` root handlers
and `structlog` configuration, then delete this entry.


## R12 — every counter has a reader (done)

Commit: see `feat(observability): the counter registry`.

**What was actually unread**, found by writing the discovery scan before the
registry, so the inventory came from the code rather than from the audit's
memory of it:

| counter | production readers before |
|---|---|
| `failopen.snapshot` | 1 (doctor, added by T-07) |
| `coverage.snapshot` | 2 (dashboard, cost report) |
| `execution_ledger.dropped_event_count` | **0** — and its docstring claimed doctor read it |
| `session_store.lock_timeout_count` | **0** |
| `prompt_capture.counters` | **0** (assembled into `status()`, which nothing imports) |
| hook terminal-outcome invariant | **never computed on traffic** |

The scan matched 8 accessors in 412 source files — narrow enough that the four
non-instrumentation ones are excused individually with a reason, rather than
allowlisted wholesale. Two of those reasons name a reader file, and a test
fails if that file stops mentioning the symbol, so an excuse cannot rot into a
blind spot.

**What the registry reported on this machine the first time it ran** (it was
built to be enforced, not to be clean, and it was not clean):

    unterminated_invocations: 316 of 4551 real invocations (6.9%)
    interception_gaps: UNHANDLED_EXCEPTION=364, EMPTY_PROMPT=368
    fail_open_events: 713, of which CHZ-FO-COST-MIGRATE-ALTER = 531

Those three are findings in their own right and are recorded here rather than
fixed under R12 — R9 owns hook death, and the migrate-alter volume needs its
own look.

**RED-CHECK — three mutations, all fire:**

1. new `compaction_failure_count()` in `session_store`, no registry entry
   → `llm_router.session_store:compaction_failure_count` named in the failure
2. `lock_timeouts` reader replaced with `n = 0`
   → *"the writer fired and the registry still reports 0.0"*
3. **the A-10 evasion**: doctor's loop replaced with `for … in []` and
   `counter_registry.readings()` preserved in a comment on the line above
   → AST assertion fails. Comments are not in the AST.

**Regression repaired during the gate:** replacing T-07's hand-written doctor
block with the registry dropped the *specific wording* of the unrecordable-loss
line from `doctor`'s issue list. Exit code was still 1, so it would have looked
fine. A `CounterReading.issues` field now carries lines a counter wants
escalated verbatim — "the state store was unwritable" is a different emergency
from a large count, and only one of them is fixed by clearing disk space.


## R14 — attempt_log rotation (done)

`_rotate` was `read_text()` then `write_text()` with no coordination. A record
another process appended between the two was erased by the truncating write.

    8 procs x 400 records   run 1  0.53% lost   run 2  5.25%   run 3  0.69%

The variance is most of why it survived: a single run at 0.53% reads as a
rounding artefact, and the rate depends entirely on how many appends land
inside a rotation window. Downstream saw nothing — valid JSONL, plausible
`summary()` output, no log line.

**Locking only the rotation was the first attempt and is wrong.** An unlocked
append still lands inside a locked rotator's read-write window. The lock has to
cover append AND rotate as one critical section, which it now does via
`file_lock.exclusive_lock` — the module written for this exact defect in
`session_store` (1.83% at 6 procs, CHZ-AUD-C-01), which this file never got.

When the lock times out the two halves are treated differently, because they
fail in opposite directions: the append happens anyway (atomic under O_APPEND;
refusing it loses the record for certain to avoid losing it by chance), the
rotation is skipped (the only destructive step), and the skip is counted under
`CHZ-FO-ATTEMPTLOG-ROTATE-UNLOCKED` so "rotation has not run for a week" is
distinguishable from "the log is not large yet". R12's registry renders it.

Rotation also lands via `os.replace` now. `write_text` truncated in place, so a
`summary()` call at the wrong moment read a log whose old content was gone and
whose new content had not arrived — and reported a model with thousands of
records as having NO EVIDENCE, which callers are told to treat as "unknown,
never bad". Silent and load-bearing.

**The check-on-the-check earned its keep immediately.** The first draft wrote
3200 fat records: over the 400 KB floor, under the 5000-line one, so rotation
never fired and all three "no loss" runs passed AGAINST THE BROKEN CODE.
`test_rotation_actually_fired` caught it. PER_PROC is now 900.

Not marked `slow` — this suite excludes `slow` by default and a concurrency
gate that does not run is not a gate. ~2.4s per run.

---

## P-05 — UNPARKED and fixed (it was never logging-config pollution)

Parked across two sessions as "logging-config pollution", bisected to
`test_built_artifact_is_complete.py`, and confirmed pre-existing at `5f90f24`.
All of that was true. None of it was the cause.

`logging.LogRecord` has `msg` and `args`. It has no `message` — that name is
assigned by `Formatter.format()` as a side effect, so `record.message` resolves
only if some handler already formatted that record. Five assertions in
`test_calibration.py` and `security/test_agentic_injection.py` depended on that
accident. The product was never involved.

It had grown from one failure to three by this session's gate, which is what
forced it: a parked item that keeps claiming new victims stops being parked and
starts being noise in every subsequent gate's signal.

**The lesson is not about logging.** An order-dependent failure invites a search
for shared state, and that search can succeed — finding state that really is
shared — while the actual defect is a local API misuse in the test. Bisecting to
the neighbour that EXPOSES a bug is not the same as finding it.

Guarded by `tests/test_logrecord_message_is_not_an_attribute.py`, with an AST
lint (the phrase in a docstring cannot satisfy it) and a premise check that
deletes itself if a future Python starts populating `.message` eagerly. The
lint's own first draft fired on `r.choices[0].message` — an OpenAI completion —
and was narrowed to exact names, on the standing principle that a lint whose
first real finding is a false positive trains people to grow an allowlist until
the allowlist is the blind spot.


## R15 — finish_reason (done)

`LLMResponse` had NO `finish_reason` field. `gateway._finish_reason` read it
with `getattr(result, "finish_reason", None)` and fell through to the literal
`"stop"` on every response ever returned — while its own docstring said the
value was *"derived rather than asserted, so a truncated answer is not reported
as a complete one."* There was nothing to derive from.

The cost is not the wire format. A `content_filter` stop returns the PARTIAL
text generated before the filter fired: long, fluent, and passing
`grounding.response_is_usable`, which is the bandit's success signal. Every
censored generation reinforced the model that produced it, and
`success_rate/avg_cost` preferred whichever model gets censored most cheaply.
`test_the_censored_text_would_otherwise_have_passed_as_a_success` pins that
premise rather than asserting it, and fails if the fixture stops demonstrating
it.

**The asymmetry is the design decision.** A REPORTED non-stop reason is a
failure; an ABSENT one is unknown. Ollama and the CLI-backed providers report
no stop reason at all, and treating their silence as failure would hand every
local model a permanent penalty on no evidence — the mirror image of the bug.
The field defaults to `""`, not `"stop"`, for the same reason, and a test
fails if that default is ever changed.

`tool_calls` is deliberately not in `_FAILED_FINISH_REASONS`: the gateway
refuses tool requests outright (H-03) so it cannot arrive, and listing it would
imply handling that does not exist.

**RED-CHECK:** delete the branch (fails), delete it but keep the name in a
comment (fails — AST), default the field to `"stop"` (fails).

**Two ratchets fired on my own work during the gate, both correctly:**

* `test_failopen_ratchet` flagged a new silent handler at `router.py:3675` — a
  reflexive `try/except` around `failopen.record`, which never raises by
  construction. Removed rather than excused.
* `test_t14_silent_mutation_ratchet` went 88 → 89 because R14's atomic rotate
  replaced one silent `write_text` with two silent sites (`os.replace` plus the
  `except OSError: pass` around temp-file cleanup). A change that was entirely
  an improvement still regressed the count. Restructured so cleanup runs only
  on failure and the outer handler records `CHZ-FO-ATTEMPTLOG-ROTATE`; the
  ratchet is now **87**, lowered rather than raised.

---

## A second suite defect of the P-05 shape (fixed)

`tests/commands/test_routing.py` did `sys.modules["structlog"] = MagicMock()`
at module scope and never restored it. Import-time code runs at COLLECTION and
`sys.modules` is process-global, so every test collected after that file ran
with a MagicMock structlog for the rest of the process.
`structlog.testing.capture_logs()` then returned a MagicMock instead of a list
and `test_exhaustion_floor.py::test_floor_emits_structured_event` failed with
`expected exhaustion_floor_returned event, got: <MagicMock ...>`.

Latent for a long time; surfaced only because the suite split changed which
files share a process. The mock was never needed.

Both this and P-05 now live in `tests/test_tests_do_not_break_their_neighbours.py`.
The `sys.modules` lint is scoped to MODULE-SCOPE assignments only — there are
41 in-function ones and a lint that fires on 42 sites gets an allowlist, and
the allowlist becomes the blind spot. It currently finds zero, so it carries an
anti-vacuity self-test that runs the detector over the exact code that was in
`test_routing.py` and asserts it flags the module-scope line and not the
in-function one.


## R17 — the verifier validator must discriminate (done)

`len(answer) > 5` was validated against three short bad answers, caught all
three, scored `detected == total`, and was classified **HIGH**. It then accepted
a confidently wrong long answer. Real kill rate on the corpus: 0%.

Nothing in the validator was broken. `MIN_BAD_ANSWERS`, distinctness and
not-equal-to-good all check the shape of the OPERATOR'S probes — and those can
only ever show that a verifier rejects the wrong answers the operator thought
of. A length check passes that test honestly.

The missing question is whether it rejects an answer it has no reason to
accept. `UNIVERSAL_DECOYS` asks it with four answers that are wrong for every
task by construction: fluent prose about mitochondria, a refusal, a bare token,
lorem ipsum. Accepting any of them means the verifier is keyed to something
that is not the task, and `classify` caps it at LOW — ahead of the probe-set
rules, because it invalidates them.

LOW rather than UNUSABLE: a weak verifier may still be a useful weak signal,
and UNUSABLE invites deleting it instead of strengthening it. An ERROR on a
decoy counts as a rejection, not an acceptance — the opposite would cap the
strictest verifiers for being strict, which is pinned by its own test.

**RED-CHECK (the plan's, verbatim):** with the decoy probe removed,
`len(answer) > 5` and the word-count check both classify as
*"HIGH: passes a known-good implementation and detects 3/3 broken ones."*

**Both halves are asserted.** A rule that capped everything at LOW would pass
the worthless corpus and destroy the mechanism, so three real verifiers must
still reach HIGH.

**Two fixture bugs caught by the premise assertions**, each of which would have
made the file green and meaningless:

1. The snippets were written as `def verify(answer): ...`. A verifier is a
   SCRIPT judged by exit code, so a function that is defined and never called
   exits 0 for every input — every verifier in both corpora accepted every
   decoy, which read as a broken probe and was a broken fixture.
2. The good answer was `"51"`, shorter than the length check's own threshold,
   so `len > 5` failed its baseline and was rejected as UNUSABLE before it could
   demonstrate anything. Then `"fifty-one"` — one word to `_answer_words`,
   whose regex keeps the hyphen — broke the word-count check's baseline the
   same way. A worthless verifier has to CLEAR every existing rule or it does
   not test the new one.


## R6 / R7 — one savings number (PARTIAL — 2 of 20 surfaces migrated)

`dashboard_data.py` records the consequence in its own source: three
hand-rolled savings queries once reported **$73.97, $102.31 and $205.19 for the
same day**. The lesson was written down. A fourth query was added afterwards,
and by this audit there were twenty.

A subagent enumerated every user-facing dollar figure; I re-derived the three
load-bearing claims myself rather than taking them:

| claim | verified |
|---|---|
| `dashboard_data.py` applies no provenance filter | `grep -c` → **0** references to `production_only`/`is_simulated`/`provenance`. It feeds `llm-router status` and session-end's 14-day panel. |
| `cost.get_realized_savings` (the only filtered TRUE net) reaches one surface | 1 caller: `tools/admin.py:960` |
| `dashboard_data.query_realized_savings` has no caller | **0** outside its own module |

That last one is the find. It is the only accessor reading the execution
ledger, it carries INV-COST-004 and a docstring explaining why it must never
become a fourth independent calculation — and nothing calls it. Same CLASS-A
shape as the 58 fail-open writers with zero readers: built, documented,
correct, wired to nothing.

**Four independent reasons the twenty disagree**, all confirmed:

1. **Provenance** — 12 of 20 query SQL directly with no filter. One benchmark
   row inflates exactly those twelve.
2. **Baseline** — Opus (`cost.py`, `dashboard_data.py`) / Sonnet (web dashboard
   tile, `share.py`) / a hardcoded per-model multiplier table (`gain.py`).
3. **"Net" means two things, both printed as "net"** — net of ROUTING OVERHEAD
   (`get_realized_savings`) vs net of ACTUAL SPEND (`get_lifetime_savings_summary`,
   session-end's headline).
4. **Table membership** — `savings_stats` only / `routing_decisions` only /
   `usage` only / a union of four.

### What was built

`savings.CanonicalSavings` + `canonical_savings()`. The precedent is the same
module's own `net_saved`: a canonical function plus a lint, which
`13_HISTORICAL_DEFECT_PATTERNS.md` identifies as the only shape that has held
(the $15/$75 price bug was fixed locally four times and returned every time).
`net_saved` made the ARITHMETIC canonical; this makes the FIGURE canonical.

Every field exists because its absence produced a wrong number: `baseline_model`
(three baselines, none named), `real_dollars_avoided_usd` vs
`baseline_equivalent_avoided_usd` (under a subscription no cash is avoided —
the gate was applied on 1 of 4 surfaces), `routing_overhead_usd` (the term that
distinguishes the two "net"s), `n_rows`, `provenance_filtered`.

**`n_rows` required fixing the accessor.** `get_realized_savings` did not return
a row count, so the denominator would have been permanently zero — the exact
defect class being remediated, inside the remediation. It now SELECTs `COUNT(*)`
alongside the sums.

`R7`: nothing in this module can render a bare amount. `headline()` and
`label_money()` both attach the baseline, the denominator and the kind of
saving, and a test asserts all three are present in every rendered string.

### The registry is the deliverable as much as the migration

All 20 surfaces are named in `savings.SURFACES` with their exact divergence, and
three tests enforce it: an unmigrated surface must state how its number differs,
a file that computes its own savings figure must be registered (AST scan for a
SQL `SUM` over a savings column), and the scan itself has an anti-vacuity check.
Twenty surfaces grew because nobody could see how many there were.

### Migrated: 3 of 20

* `mcp_session_dashboard` — already on `get_realized_savings`.
* `cli_savings_report` — headline now canonical; its per-model breakdown still
  reads `savings_stats` (the only table carrying it) but is now
  provenance-filtered the same way, and the ledger total is printed BESIDE the
  canonical figure rather than instead of it.

* `cli_explain_dashboard` — a case where the subagent's read and mine differ,
  and mine is recorded here because the difference matters. It flagged this
  command as unfiltered and therefore broken: *"the tool built to diagnose
  dashboard discrepancies is itself one of the sources of them."* But its
  per-panel figures are raw ON PURPOSE — it exists to show why the panels
  disagree, and filtering it would hide the very rows causing the
  disagreement. What it lacked was a reference to compare them against. It now
  prints the canonical figure at the top and states that everything below is
  deliberately unfiltered. On this machine that line reads:

      $0.00 real dollars avoided (subscription: the plan is paid either way)
      · $0.01 baseline-equivalent vs claude-opus-4, n=1

  which is R7's whole point in one line: the subscription gate fires, the
  baseline is named, the denominator is shown.

`cli_savings_report`'s docstring claimed `savings_stats` was *"the SINGLE source of truth, so the
report can never disagree with the stored stats."* Both halves were wrong in a
way that read as a guarantee — it was not the single source, and agreeing with
its own table while disagreeing with every other surface is not the property
the sentence promised. R6 acceptance criterion 2 was "true or deleted"; it is
now true of the headline and the docstring says exactly what is and is not
covered.

### Left to do — stated, not buried

**18 surfaces still compute their own figure.** They are individually listed in
`savings.SURFACES`, so none is forgotten and a nineteenth cannot appear. The
migration is per-surface work across hooks, the web dashboard and the MCP
tools; the mechanism and the enforcement are in place, the wiring is not.

**RED-CHECK (four, all fire):** stop filtering provenance → the synthetic $999
row reaches the figure (1002.0); clamp the net → the loss test fails; add an
unregistered file summing `cost_saved_usd` → the discovery test names it; strip
the baseline from `label_money` → the qualifier test fails.


## AUD-06 returns — the lint's SCOPE was the hand-maintained part

Found while migrating `commands/share.py` for R6. The clamp lint reported
*"CHZ-SS-01 OK: no clamped savings subtraction in 10 money modules"* while
`share.py:112` read `total_saved += max(0.0, base - cost)` — AUD-06's sentence
verbatim, in a card meant to be **published**.

`MONEY_MODULES` was a hand-written tuple of eleven paths. The RULE was
structural; its SCOPE was not, so it was structural only for the files someone
had thought of. Five money surfaces were never scanned, carrying **eight**
clamped subtractions:

    commands/share.py:112             a card meant to be shared publicly
    hooks/session-end-clawcode.py     x4
    hooks/status-bar.py:240
    hooks/status-bar-clawcode.py:92
    dashboard/server.py:157           the web UI's headline tile

`13_HISTORICAL_DEFECT_PATTERNS.md` records the $15/$75 bug being fixed locally
four times because "no fix was ever made structural." This is the fifth
instance of the same shape one level up: a structural fix whose scope drifts.

**Fix:** `MONEY_MODULES` is derived from `savings.SURFACES`. The two mechanisms
now close each other's gap — an unregistered surface fails R6's discovery test,
and a registered one is linted automatically. Neither can be forgotten
independently. The lint went from 10 modules to **23** and passes.

All eight clamps are now `net_saved`. One of the eight was a false positive
worth recording: `session-end-clawcode.py:198` was `max(0.0, baseline)` in the
FREE section, where cost is zero by definition so the saving cannot be
negative. It was correct — and written in a form indistinguishable from the
three real clamps nearby, which is reason enough to drop it rather than exempt
it.

**The fallback is signed, not clamped.** Each surface wraps `net_saved` in a
try/except so a broken import cannot take down a statusline; the fallback does
the same subtraction rather than reverting to `max(0, …)`. A clamp in the error
path is the defect returning on exactly the machines where something else is
already wrong, and an AST test fails if any `_net` helper ever calls `max`.


## R10 — refuse what cannot be served (PARTIAL — tools done, the rest not)

H-03 found `/v1/chat/completions` accepting a `tools` array, discarding it
before the handler body ran (the field was not declared, so Pydantic dropped
it), and returning fluent prose with `finish_reason: "stop"`. It was fixed on
TWO endpoints. `/v1/responses`, `/api/chat` and `/api/generate` kept the
defect, because the fix was applied call-site by call-site and nothing
enumerated the endpoints.

The plan predicted this word for word: *"a new endpoint must be added or the
test fails — this is precisely how /v1/responses was missed."* It was still
missed at the time of writing, on three endpoints, including OpenAI's CURRENT
function-calling surface.

The test DISCOVERS every POST route on the app and requires each to be
classified: a completion endpoint that must refuse, or an explicitly excused
non-completion route (`/route` returns a decision and executes nothing;
`/ground` checks text produced elsewhere). A route added later is unclassified
and fails.

**Two test bugs caught, both of which would have left the file green:**

1. `TestClient` defaults to Host `testserver`, and `_guard_cross_origin`
   rejects a non-loopback Host with **403** (CHZ-SEC-04, DNS-rebinding). Every
   assertion would have been measuring the CSRF guard, not the refusal.
2. The "an ordinary request is not refused" test proved its point by ACTUALLY
   ROUTING — a 14.7s live Codex call and a real `routing_decision` row. A test
   that proves a refusal did not fire by performing the unrefused action
   spends money to learn nothing. It now asserts the helper directly.

I checked the operator's live ledger afterwards: 0 rows in `usage`,
`routing_decisions` and `claude_usage` in the window. No contamination.

**Still open under R10:** vision (`images`) silently discarded, structured
output (`format`/`response_format`) unchecked, and no context-window
pre-flight. `CapabilityRequirement` still has no structured-output dimension.
The enumerating harness is in place, so adding each is now a matter of another
parametrised dimension rather than another endpoint-by-endpoint sweep.

---

## The producer never stamped provenance — found in the live ledger

R6 made the money surfaces filter fail-closed (`COALESCE(is_simulated, 1) = 0`).
That rule is right: a row written before the column existed had its provenance
NEVER MEASURED, and counting it asserts production origin on no evidence.

It is also only safe if the producers stamp it. Checking whether the R10 test
had contaminated the operator's ledger, I read the rows around it instead:

    58 rows in 20 minutes — this session's OWN hook — every one is_simulated = NULL

All four `INSERT INTO savings_stats` sites stamp provenance. `import_savings_log`
correctly copies an entry's own provenance rather than inventing one
(`_detect_synthetic()` there would describe the IMPORTING process). But
`hooks/savings_logger.py` writes the JSONL those rows come from, and its record
carried no provenance field at all — so there was nothing to copy.

Correct writer, correct importer, correct filter, and a real figure of $0.00,
because the one link nobody looked at was the producer. Every individual link
passed its own tests, which is why the new test walks the whole chain:
record -> JSONL -> import -> filtered query.

**And a ninth and tenth clamp,** in the same file, which the lint still did not
scan because a producer is not a surface:

    savings_logger.py:201   max(0.0, baseline - external_cost)
    savings_logger.py:323   max(0.0, float(receipt.savings_usd))

The second is the worse one: `receipt.savings_usd` is computed upstream and can
legitimately be negative, so the bridge was destroying the information it
exists to carry. `savings_logger.py` is now in the lint's core list — 23
modules -> **24**, all clean.

**RED-CHECK:** remove the `is_simulated` key -> the AST test names the record;
make `_detect_synthetic` fail open -> *"it must return True."*

The suite's own T-01 guard then caught my test importing
`llm_router.hooks.savings_logger` and leaving a fileless stub on the package —
the same "a test breaks its neighbours" class fixed two commits earlier, caught
by the guard that exists because of it.


## R9 — make hook death visible (PARTIAL — 2 of 3 criteria)

A killed hook is observably identical to one that chose not to route: both end
in a turn Claude answered directly. Measured: a 60s timeout, a maximum observed
duration of 55.3s, and 5.5% of real invocations reaching no terminal outcome.

**Criterion 1 — doctor reports the unterminated rate with its N, as an ISSUE
above a threshold.** Done by R12's registry. `unterminated_invocations` renders
"316 of 4551 real invocations (6.9%)" and is `alarming` at >= 2%, which appends
to doctor's issue list rather than printing a line. A handful is noise (a
session that ended mid-turn); a systematic rate means a routing branch logs
nothing at all, which is the bug that hid `ENFORCE=off` for a day.

**Criterion 2 — a start marker, cleared on exit; an orphan is a detected kill.**
Done. `hook_liveness` writes a marker before the expensive work and clears it
via `atexit`, so every exit the process CONTROLS — including `sys.exit`, which
the hook uses on at least five paths, and an unhandled exception — removes it.
A killed process cannot, so the marker survives and the next invocation counts
it once and deletes it.

The detection cannot live inside the hook: the condition is "this process
stopped executing", so nothing in that process can record it. The evidence has
to be on disk BEFORE the kill.

Surfaced as `hook_kills` in the counter registry, so doctor reports it.

**Direction is chosen deliberately.** A marker is an orphan only when its pid
is not alive AND it is older than 90s (above the hook's 60s budget). PID reuse,
`PermissionError` from `os.kill` on another user's process, and any
undeterminable answer all resolve to NOT-A-KILL. That undercounts; it never
invents a kill. For a number an operator will act on, undercounting is the safe
direction, and a kill noticed one invocation late is still noticed.

`orphan_count()` does not reap — "run doctor twice and the number changes" is
its own kind of broken instrumentation.

**Criterion 3 — hook p50/p95/p99 measured in CI against the installed timeout,
margin asserted — NOT DONE.** That needs a timing benchmark in CI, not a unit
test, and a benchmark run on this machine would be untrustworthy anyway
(CLAUDE.md: macOS Maintenance Sleep advances `time.time()` and not
`time.monotonic()`; one benchmark task recorded 918.6s of which 902s was the
laptop asleep). Left explicitly undone rather than approximated.

**RED-CHECK — the plan's, performed with a real SIGKILL:**

    marker survived the kill: True
    kills detected: 1
    recorded as: CHZ-HOOK-KILLED = 1

Plus: moving `mark_started` out of `main()` fails the AST assertion, and making
`orphan_count` reap fails the idempotent-read test.


### R9 fallout — three of my own gates caught me, and one was a real defect

The R9 commit failed its first gate on three assertions I had written earlier
in this same run:

1. `test_failopen_ratchet[hooks/auto-route.py]` — the liveness setup block was
   wrapped in `except Exception: pass`. Correct to flag: if the marker cannot
   be written, kill detection is OFF and `hook_kills` reports a confident zero,
   which is the "empty measurement rendered as healthy" shape R9 exists to
   remove. Now records `CHZ-FO-HOOK-LIVENESS-SETUP`.
2. `test_r12_every_counter_has_a_reader::test_every_registered_counter_has_a_driver`
   — `hook_kills` was registered with no driver. Exactly what that assertion is
   for: a registry entry whose reader is never exercised against its writer.
3. `test_t14_silent_mutation_ratchet` — 87 -> 88.

The third was a genuine correctness bug, not bookkeeping. `clear_marker` used
`except FileNotFoundError: pass`, which reads as harmless — an absent marker is
the normal outcome when a later process reaped it first. But the handler shape
hid the case beside it: **a marker we cannot remove becomes an orphan, and an
orphan is reported as a KILL.** A silent permissions failure would have
manufactured kills that never happened, in the one counter built to answer
"is routing actually running". Now `missing_ok=True` for the normal case and a
counted `CHZ-FO-HOOK-CLEAR-MARKER` for the rest. Census back to 87.


## R3 — stop calling the allowlist a security control (done, option (a))

Decision taken: **honesty now, containment later.**

SECURITY.md said, correctly and reproducibly, that **10 of 12** corpus commands
are refused by `guard_command`. The number was right. The audit's
self-indictment was that it is WORSE than a wrong number, and building the
corpus test proved why.

The corpus measured the wrong population. All twelve entries are obviously
destructive or exfiltrating — `rm -rf /`, `git push --force`, `curl -d @.env`.
It measures how well the allowlist stops a command you would have caught by
reading it.

Measured against the real `guard_command`, same capabilities via programs the
allowlist PERMITS:

    python3 -c "print(open('~/.ssh/id_rsa').read())"   ALLOWED
    python -c  urllib.request.urlopen(...)            ALLOWED
    node -e    child_process.execSync('curl ...')     ALLOWED
    awk        BEGIN{ "cat ~/.ssh/id_rsa" | getline } ALLOWED
    sed -e     1e cat ~/.ssh/id_rsa                   ALLOWED
    find       -exec curl ... {} ;                    ALLOWED
    git -c     core.pager=curl ...                    ALLOWED
    pytest     -p evil_plugin                         ALLOWED
    go run / cargo run                                ALLOWED

    10 of 10 ALLOWED

`cat ../../.ssh/id_rsa` is refused. `python3 -c` reading the same file is not.

**The corpus file was read by nothing.** `grep -rln security_command_matrix
tests/ scripts/` returned empty — the 10/12 figure came from running the file
by hand. It is now `VERDICT<TAB>command` and a parametrised test re-derives
every verdict from the code, so the numbers in SECURITY.md cannot be
hand-edited or go stale.

Both counts now appear in SECURITY.md side by side, because the difference
between them IS the finding.

**RED-CHECK — three, all fire:**

1. add `ruby` and `perl` to `_ALLOWED_PROGRAMS`
   -> *"allowlisted program(s) with no classification: ['perl', 'ruby']"*
   (the plan's red-check, and the mechanism that stops an eleventh interpreter
   arriving unnoticed)
2. restore the flattering claim to SECURITY.md
   -> *"contains claim(s) the allowlist does not support"* AND *"does not state
   'not a containment'"* — removing a false claim without stating the true one
   leaves the reader with the same impression and no sentence to argue with,
   so both directions are asserted
3. delete the interpreter block from the corpus -> *"corpus has only 12 rows"*

**Not done, by decision:** option (b), real containment via `sandbox-exec` /
namespaces / a container. SECURITY.md now recommends it for untrusted repos
rather than implying the allowlist substitutes for it.


### R3 fallout — two pre-existing test defects, both about the same framing

**`test_security_md_says_its_table_measures_only_one_layer` was enforcing the
misleading claim.** It required SECURITY.md to say the twelve-command table
"UNDERSTATES" the real protection — that the allowlist blocks MORE than the
table shows. True, and the misleading half: a high refusal rate over twelve
obviously-destructive commands says nothing about what an agent can do.
Rewritten to require BOTH populations be named (10 of 12 and 10 of 10), because
the difference between them is the finding.

**`test_the_allowlist_blocks_what_the_old_table_called_unblocked` passed for the
wrong reason.** It handed `guard_command` a STRING where the function takes an
argv LIST, so `argv[0]` was `"g"` and every command was refused on *"'g' is not
in the inspection allowlist"*:

    as a string:  REFUSED: 'g' is not in the inspection allowlist
    as an argv:   REFUSED: 'git push' changes state rather than reading it

It would have passed unchanged if `git push` had been explicitly allowed. The
refusal REASON is now asserted, not just the boolean, so an accident of input
shape cannot satisfy it. The same file also leaked a module stub (T-01); loaded
by path now.

Both are the recurring lesson in a new place: a test that passes tells you
nothing until you know WHY it passes.


## R8 — capture on, behind explicit consent (PARTIAL — consent done, measurement not)

Decision taken: **(a) default capture ON with explicit consent at install** —
not the recommended option, chosen deliberately, with the note that the consent
flow is load-bearing.

The finding was never that capture is dangerous. It was that the product
CLAIMED to preserve task success while the only mechanism that could measure
that was off by default, so the claim rested on nothing.

### What was built

`ground_truth_consent` — consent as a recorded event, not a flag:

* **Silence is refusal.** A non-interactive install (CI, a Dockerfile,
  `yes |` piped into onboarding) records NO consent and capture stays off.
  Defaulting to on when nobody could answer is consent manufactured by the
  absence of a human.
* **The record answers "did anyone actually agree to this?"** — when, which
  terms version, and what was said. A bare flag cannot, and that is the only
  question that matters if it turns out they did not.
* **Terms are versioned.** Changing what is captured changes what was agreed
  to; prior consent stops matching and the operator is asked again.
* **Revocation records a refusal rather than deleting the record.** "They said
  no" and "they were never asked" are different facts.
* **Unreadable fails closed.** The cost of asking again is a prompt; the cost
  of assuming yes is capturing someone's prompts without agreement.

Onboarding writes `LLM_ROUTER_GROUND_TRUTH` on BOTH branches, so an existing
`=1` from a previous install is turned OFF by a later refusal instead of
silently surviving it. A test asserts the ternary via AST, because writing it
only in the granted branch is the natural mistake.

**RED-CHECK — three, all fire:** non-interactive defaults to yes -> refused;
revocation deletes the record -> *"'they said no' and 'they were never asked'
are different facts"*; the flag written unconditionally as `=1` -> *"not
written from a conditional expression"*.

### NOT done — acceptance criterion 1

*"a report showing downgrade-regret and upgrade-waste SEPARATELY on >= N real
tasks, with N stated."*

**Neither metric is computed anywhere in the repo.** `grep -rn "regret\|upgrade_waste"`
over `src/` and `scripts/groundtruth/` returns only unrelated uses of the word
"downgrade" (budget pressure, capability downgrade, complexity downgrade). The
metric R8 names has never existed.

It also cannot be produced today: the dataset it would run over is empty,
because capture has been off. The consent flow is the precondition, and it is
now in place — the measurement follows once real captured tasks accumulate.

Recorded here rather than approximated. A regret number computed over a handful
of tasks would be exactly the kind of figure this audit exists to stop, and
CLAUDE.md already fixes the rule: below ~50 real prompts, say "too few to tell"
instead of a number.


## R16 — the audit's own diagnosis was wrong, and a premise assertion caught it

The finding said: `capture()` has no `cwd`/`tools`/`external` parameters, so
repo- and tool-bound tasks are permanently ineligible — **"a missing signature,
not a policy."**

The second half is false. Writing the premise assertion for the fix disproved
it in one line:

    envelope.build(..., cwd=None)  ->  RepoState(commit=..., reconstructable=True)

`envelope.build` already falls back to `os.getcwd()`. A repo reference was
being captured all along; the parameter was never the blocker.

**The real blocker is `eligibility.replay_available()` returning False:**

    assess(repo task, has_repo_state=True, envelope_complete=True)
      -> replayable=False, reasons=['no-replayer-for-required-state']

That is H-08, and it IS a policy — a deliberate, documented refusal to admit
work the runner cannot execute. `scripts/groundtruth/` has zero `git checkout`
/ `git apply` / worktree call sites, so a captured repo task could never be
graded, and a gate that admitted them would report a healthy funnel while
producing nothing. The gate is tied to the CAPABILITY, not a flag, so repo
tasks become eligible automatically the day a replayer exists — pinned by a
test that monkeypatches `replay_available` and asserts the refusal disappears.

I would have "fixed" a non-defect and reported it as unblocking repo tasks.
What stopped that was asserting the premise rather than the conclusion — the
same discipline that caught the R14 rotation fixture and the R17 verifier
fixtures in this same run.

### What was actually delivered

**Acceptance criteria branch 2**, which is now the applicable one: the docs
state Ground Truth covers **state-free prompts only**, say why, and say what
would change it.

The signature fix is KEPT, described accurately: an explicit `cwd` beats the
process working directory (which is not reliably the task's repo), and
`tool_names`/`external` have no fallback at all. It is independently correct
and it is not what unblocks anything — the test file says so explicitly, so
nobody re-reads the commit and concludes otherwise.

**The gap is NOT quantified as a share of traffic, deliberately.** There is one
captured prompt on this machine and no pool, because capture has been off. A
percentage from n=1 is precisely the figure this audit exists to stop. What is
exact: 100% of tasks requiring repo state are excluded until a replayer exists.

**RED-CHECK:** restore `project_root` (the first draft's expression) -> *"uses
name(s) not bound in the function: ['project_root', 'str']"*. That one matters:
`_finalize_successful_route` does not receive a `project_root`, so it would
have been a NameError on every capture, landing in the enclosing
`except Exception: capture never breaks routing` — the feature silently dead in
exactly the way the defect it was fixing was. Also: drop `cwd` before
`accumulate` -> *"accepts 'cwd' and drops it — the same defect with a more
convincing surface."*


## R13 — source-text assertions: 62 -> 2

A-10 was the audit's most uncomfortable finding: 23 tests asserted
`"foo(bar)" in inspect.getsource(mod)` under docstrings reading *"Rule B: the
call site, not the property in isolation"*. The evasion was reproduced — phrase
in a comment, call deleted, 23 tests passed.

`tests/_ast_assert.py` gained `string_constants` / `assert_in_strings` /
`assert_reads_attribute`, which is what most of the population needed: the
biggest single category was SQL fragments, and pulling string literals from the
AST is strictly stronger than grepping the file, because comments are not in
the AST and docstrings are excluded explicitly.

**The detector had a precision bug.** `" in src"` also matched prompt fixtures
like `"Make the filter case-insensitive in src/a.py"`, so the ratchet counted
test data as violations. A detector that over-counts invites raising the
ceiling for the wrong reason. The real starting population was smaller than 62.

**Two remain, both deliberate and both named in `REMAINING_BY_DESIGN`:**

* `test_s03_route_server_auth_parity.py` checks that a specific DOCSTRING
  sentence is gone. `string_constants` excludes docstrings BY DESIGN, so
  converting it would make it pass whether the stale sentence survived or not
  — strictly weaker than the text check. (Found by a subagent, and it is the
  right call.)
* `telemetry/test_m02_benchmarks_declare_themselves.py` scans for a marker
  that is a comment by construction. There is no AST node to assert on.

**My own anti-vacuity check was wrong, and success is what revealed it.**
`test_the_scan_still_finds_the_population` asserted the repo still CONTAINED
more than ten source-text assertions, so the ratchet could not be vacuous. That
was reasonable at 62 and wrong the moment the class was nearly closed: it
required the codebase to keep the disease in order to prove the thermometer
worked. At 2 remaining it FAILED. It now proves the detector against a
SYNTHETIC positive — including that it does not flag `"in src/a.py"` — so the
repo's count is free to reach zero, which is the goal.

MAX lowered 62 -> 2. Every conversion was red-checked with the A-10 evasion
(phrase preserved in a comment, call broken); the subagents' work was
independently re-red-checked here rather than taken on report.

---

## K1-K7 — the audit-readiness kit

| | what it is | red-check |
|---|---|---|
| **K1** | `scripts/audit/freeze_state.py` emits FROZEN_STATE.md | print `sys.executable` again -> *"names the operator's home directory"* |
| **K2** | `tests/claims_ledger.py` + enforcement: 16 claims, each tagged to the test that proves it | delete a proof test -> named; add a README `95%` claim -> named; **mark the UNPROVEN claim PROVEN -> named** |
| **K3** | denominator identities: parts sum to whole for capture outcomes, fail-open codes, coverage, routing outcomes, provenance | each identity has a non-empty precondition |
| **K4** | standing adversarial corpus: 6 canary secrets, 4 interpreter escapes, routing pairs — **diagnostic only, never tuned against** | two strict xfails carry known gaps; an xpass fails the suite |
| **K5** | `llm-router doctor --audit` renders the whole registry | break one section -> the others still render |
| **K6** | `.github/workflows/self-audit.yml` runs K1-K5 + the three ratchets nightly | — |
| **K7** | the narrowest-mutation rule, written into `CLAUDE.md` | — |

### K1 leaked a personal path on its first run

`sys.executable` is `/Users/<name>/...` on a developer machine, and
FROZEN_STATE.md is committed to a PUBLIC repo. Caught by writing the privacy
assertion before trusting the generator. All paths are now redacted to `~`, and
a test fails on any absolute home path in the output.

### K2's one UNPROVEN row is the point of K2

`preserves-task-success` — the product's central claim — is marked UNPROVEN
with the reason recorded. The only mechanism that could measure it was off by
default until R8, so there is no dataset, and neither downgrade-regret nor
upgrade-waste is computed anywhere in the repo.

**My own red-check found a hole in this.** Marking that row PROVEN with a
loosely-related test PASSED everything — the ledger could be silenced by
editing the status. Promoting a `KNOWN_UNPROVEN` claim now requires removing it
from that dict too, which is a deliberate act with a visible diff.

### K4 found a new routing defect on its first run

Finding I-01 ("wording drives the route") was INVALIDATED during the audit as a
confounded pair. K4's unconfounded pair still routes differently:

    'what is 17 * 3?'                    -> query    (len 15)
    'Could you tell me: what is 17 * 3?' -> query    (len 34)
    'tell me what 17 * 3 is'             -> ANALYZE  (len 22)
    'Could you tell me what 17 * 3 is?'  -> ANALYZE  (len 33)

Not politeness, not length. The classifier matches a literal `what is` bigram;
inverting to `what <expr> is` loses the signal and the prompt falls through to
the documented "analyze low-signal default".

**The direction is what makes it cost money:** the fallback is the MORE
expensive tier, so the failure mode is silently upgrading a trivial question.
Nothing reports it — from every surface it looks like an ordinary analyze
route.

Left as `xfail(strict=True)` rather than fixed. Changing it is a routing-
behaviour change that needs evaluating on the target distribution, and tuning
the classifier against a corpus row is precisely what K4 forbids. When it is
fixed the test XPASSes and fails the suite, which is the mechanism working.
