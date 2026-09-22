# Phase 48 — Attack your own audit

> An audit that invalidated nothing did not attack itself.

Mandatory phase. The orchestrator's job here is not to defend the findings but
to try to break them, and to record what broke. Everything below either fell
over or survived a deliberate attempt to knock it down.

Written AFTER the remediation and the v15.0.0 release, which changes what this
phase can do: several findings can now be attacked with evidence that did not
exist when they were made, and the remediation is itself a claim that deserves
the same treatment.

---

## Part 1 — Findings that were WRONG

### I-01 — "Wording, not difficulty, drives the route" — INVALIDATED, then PARTIALLY REVALIDATED

The Routing Scientist claimed politeness changed the route. The pair used to
demonstrate it was confounded: the two prompts differed in length as well as
phrasing, and length is a documented tier input. Marked INVALIDATED.

**That verdict was itself too confident.** The K4 corpus, built during
remediation, found an unconfounded pair:

    'what is 17 * 3?'                    -> query    (len 15)
    'Could you tell me: what is 17 * 3?' -> query    (len 34)
    'tell me what 17 * 3 is'             -> ANALYZE  (len 22)
    'Could you tell me what 17 * 3 is?'  -> ANALYZE  (len 33)

Length varies within each verdict and across it, so length is not the
explanation. The classifier matches a literal `what is` bigram; inverting the
word order loses the signal and the prompt falls to the documented "analyze
low-signal default".

**The correct verdict is: the mechanism claimed was wrong, the phenomenon was
real.** Invalidating the pair should not have closed the question. Carried as
`xfail(strict=True)` in `tests/test_k4_adversarial_corpus.py`.

### I-02 — `git core.pager` RCE — INVALIDATED

Already closed at the audited commit. The Security Red Teamer was reading a
version of the allowlist that predated the `-c` handling.

### I-03 — Symlink escape via `_resolve_path` — INVALIDATED

`Path.resolve()` is called before the containment check, not after. The finding
described the reverse order.

### I-04 — `run_verifier` environment leakage — INVALIDATED

Fixed by S-07 before the audit ran. The specialist read `dict(os.environ)` in a
diff, not in the tree.

### R16's diagnosis — INVALIDATED by its own fix's premise assertion

The finding said `capture()` lacking a `cwd` parameter made repo tasks
"permanently ineligible — **a missing signature, not a policy**".

The second half is false, and one line disproved it:

    envelope.build(..., cwd=None)  ->  RepoState(commit=…, reconstructable=True)

`envelope.build` already falls back to `os.getcwd()`. The real blocker is
`eligibility.replay_available()` returning False — H-08, a deliberate and
documented refusal to admit work the runner cannot grade. **It is a policy, and
a correct one.**

I would have shipped a fix for a non-defect and reported it as unblocking repo
tasks. What stopped it was asserting the premise rather than the conclusion.

---

## Part 2 — Findings that were RIGHT but MEASURED WRONG

### A-07's headline number was wrong by 8x

Reported: **44%** of real hook invocations reach no terminal outcome.
Actual: **5.5%**, later **6.9%** on a larger window.

The regex counting terminal outcomes missed `DIRECT SUCCESS:`, `DIRECT FAILED:`
and `OUTPUT COMPLETE`. The finding stands — a killed hook was genuinely
indistinguishable from one that declined — but the number that made it feel
urgent was an artefact of my own parser.

### SECURITY.md's "10 of 12 refused" was accurate and MORE misleading than a wrong number

The corrected figure was right. The corpus was the wrong population: all twelve
commands are obviously destructive, so a high refusal rate over them says
nothing about what an agent can do. Measured against the same capabilities via
allowlisted interpreters: **10 of 10 ALLOWED**.

Correcting a number without questioning its denominator produced a more
confident version of the same error.

### The R13 population was overstated

The detector counted `" in src"` inside prompt fixtures such as
`"Make the filter case-insensitive in src/a.py"`. The starting population was
smaller than the 62 reported.

---

## Part 3 — Attacks on the REMEDIATION

The remediation is a claim. It gets the same treatment.

### The fail-open counter is polluted by the tool that reports it — CONFIRMED, NEW

`llm-router doctor --audit` is a read-only diagnostic. Measured on a clean
install of the published 15.0.0, same state, three consecutive runs:

    run 1   fail_open_events: 4
    run 2   fail_open_events: 8
    run 3   fail_open_events: 12

**Observing the counter increments it by 4.** Root cause: two
`ALTER TABLE … ADD COLUMN is_simulated` statements fail with
`duplicate column name` on every database open and are recorded as fail-open
events. The idempotency guard in `_migrate_alter` covers the `ADD COLUMN` form
but does not catch these two paths.

Consequences, in order of how badly they undermine earlier claims:

1. **The numbers I reported as evidence were measuring my own tooling.** The
   R12 commit message and `REMEDIATION_RUN.md` both state
   `fail_open_events: 713, of which CHZ-FO-COST-MIGRATE-ALTER = 531` as a
   finding about the product. A share of that was generated by me running
   `doctor` during development.

   **And I got the figures wrong while writing this file.** The first draft of
   this paragraph quoted "829 / 647" and attributed them to the K1-K7 commit.
   Neither number appears in any commit message; they were a later reading I
   took during K5 development and then misremembered as published. Checking my
   own citation is what caught it — in the phase whose entire job is checking
   my own work.
2. **A normal, expected condition is counted as a degradation.** An idempotent
   migration that no-ops is not a fail-open. The counter's own docstring says
   "a spike means schema migration is silently not happening" — but the
   baseline is 4-per-open, not zero, so a real spike is indistinguishable from
   someone checking more often.
3. **R12's central claim is dented.** "Every counter has a reader" is still
   true. "The counters mean something" was never separately established, and
   this one does not.

This is the audit's own thesis turned on the remediation: *convincing telemetry
about something we have not actually proven.*

### The provenance filter's cost was understated

R6 made money surfaces fail-closed. I described the effect as "savings will
drop" and gave the 285→23 figure. What I did not say plainly: **262 of 285 rows
became invisible**, and the cause was a producer that never stamped provenance
— a defect introduced by the same remediation, found only because I happened to
check the ledger for contamination after an unrelated test.

Had I not looked, v15.0.0 would have shipped a filter that zeroed real savings,
with the fix for the producer arriving later.

### Two subagent findings I accepted too readily, and one I should have

Accepted correctly: the claim that `dashboard_data.py` has zero provenance
references (verified: `grep -c` → 0) and that `query_realized_savings` has no
caller (verified: 0 outside its module).

**Accepted too readily at first:** a specialist's claim that `explain-dashboard`
was "itself one of the sources of dashboard discrepancies". Its per-panel
figures are raw BY DESIGN — it exists to show why panels disagree, and
filtering it would hide the rows causing the disagreement. I nearly
"fixed" it.

### A specialist leaked one of the operator's other private projects

An audit artifact named an unrelated private repository and its filesystem
path. `scripts/check_identity.py` blocked the commit. The audit's own tooling
caught it; no process I designed did.

---

## Part 4 — What this phase could NOT attack

Honest limits, so the next audit knows where to start.

- **The three CRITICALs were re-derived by the same orchestrator that wrote
  most of the code.** Phase 50's clean-room reproduction reduces this for
  reachability but not for judgement: I chose which behaviours count as
  defects.
- **No finding was tested against a user who is not me.** Every "an operator
  would be misled by this" is a claim about a hypothetical person.
- **The remediation's own tests were written by its author.** The red-checks
  constrain that (a test that never exercised its claim fails a narrow
  mutation) but they cannot catch a claim nobody thought to make.
- **The `xfail(strict=True)` markers are a promise, not a proof.** They record
  that a gap is known. Nothing forces anyone to close one.
