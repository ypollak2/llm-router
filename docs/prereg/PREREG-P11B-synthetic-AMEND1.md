# PREREG-P11B-synthetic AMEND1: stricter run rule for later fidelity runs

This file is frozen by the hash in `PREREG-P11B-synthetic-AMEND1.sha256`. It amends
`PREREG-P11B-synthetic.md` (sha256 `231fe6a4…c585`). The amendment governs every P1.1-b synthetic
fidelity run that **starts after** its `frozen_utc`, including the local stratum. It does not
change the Sonnet stratum already run: that run had n = 60 paired items and 0 errors, so it meets
this stricter rule too, and its FAIL stands as scored.

## Why

The independent review of PR #509 (finding 1) showed a gap. The original run rule makes a stratum
NOT INFORMATIVE only above 10% errored items. With 1-6 errors, that allows a verdict on n = 54-59
paired items. A `--limit` smoke run could also print a verdict. PLAN v16 P1.1-b says: "Until
n >= 60 paired the result is 'not informative'" (60 paired per stratum).

## Rule (replaces the "Run rule" bullets it names; everything else is unchanged)

* A stratum gets a PASS or FAIL verdict only with **n >= 60 paired, error-free items** and at most
  10% errored items. Otherwise it is **NOT INFORMATIVE**. With n = 60 items, any error makes it
  NOT INFORMATIVE.
* A run with `--limit` is a **harness smoke check**. It is labelled `SMOKE (not scored)` and is
  never a result.
* Everything else in the original prereg is unchanged: the items, arms, grading, bar, retry rule,
  spend and output.
