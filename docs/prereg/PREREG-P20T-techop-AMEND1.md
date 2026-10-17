This file is frozen by the hash in `PREREG-P20T-techop-AMEND1.sha256`.

# PREREG-P20T-techop AMEND1: the free-memory floor counts the arm's own resident model as available

When frozen, this file is frozen by the hash in `PREREG-P20T-techop-AMEND1.sha256`.

It amends `PREREG-P20T-techop.md` (d6; sha256
`293ae8d2bd6919f35d0e5a2e134eb49001ebc77c9bb54118a13de384aba203b9`, frozen_utc
2026-10-17T18:44:41Z). It changes only the items below; everything else stands word for word.
It is the P2.0-T counterpart of `PREREG-P20-requal-AMEND5.md` (sha256
`a721dabd2dffde7d7f7e5f1e376e484612d2938f0acd48949420e1a9519e2da7`, frozen_utc
2026-10-17T23:53:15Z), which fixed the same defect in the sibling harness.

## Why

Section 11 requires `memory_pressure -Q` free percentage >= `QUIET_FREE_MEM_MIN_PCT` (40) in each of
three readings before every local call, warm-up included. The arm's own model stays resident
between calls (keep_alive 10m). Coordinator measurement, 2026-10-17 23:46Z, same machine (48 GB
RAM), lock held, no run active: free 76% before loading `qwen3.8-uncensored:latest`; 27%, 27%,
28% with it loaded at num_ctx 32768; `/api/ps` size 24.6 GB; 77% after unloading. A second
measurement through the P2.0 harness's real `quiet()` at 23:52Z: raw free 22-24% with the arm
loaded (own share 45.8%), 74-75% unloaded.

So every gate after the warm-up fails by construction. In P2.0 this produced 3 warm-up quiet rows
and no answer row between 21:20Z and 23:51Z. `requal/t1` has no `quiet.jsonl` and no `answers/`:
no P2.0-T local call has been made, so no data exists under the old rule.

The floor exists to keep other memory consumers from distorting the arm's latency. The arm's own
weights are the treatment, not contention, so they must not count against it.

## Changes

1. **Free-memory basis.** With `QUIET_FREE_MEM_BASIS=excl_own_arm`, each quiet reading passes the
   memory condition when `free_pct + own_pct >= QUIET_FREE_MEM_MIN_PCT`, where
   `own_pct = 100 * resident_bytes(arm) / hw.memsize` and `resident_bytes(arm)` sums `/api/ps`
   `size` over entries whose `name` equals the arm exactly (0 when not loaded). A foreign model's
   memory never counts. A non-finite or negative `own_pct` parks. The threshold (40),
   `QUIET_LOAD1_MAX` (4.0), `QUIET_READINGS` (3), `QUIET_INTERVAL_S` (10), the P2.0-process check,
   the foreign-resident-model check and the exclusive lock are unchanged.
2. **Recorded and re-checked.** Each local answer row's `quiet` readings add `own_pct`. The
   `items_run_after_failed_recheck` integrity check (`unchecked_local_rows`) applies the same
   basis; under it a reading without a finite, non-negative `own_pct` counts as unchecked.
3. **AMEND1 is enforced.** If `PREREG-P20T-techop-AMEND1.sha256` exists, `frozen()` requires: the
   sidecar matches this file; its `frozen_utc` is timezone-aware, strictly after the prereg's and
   before now; the constants block has exactly `QUIET_FREE_MEM_BASIS` (value `excl_own_arm`) and
   `REPO_SHA` (40 lowercase hex); if `<run-dir>/PREREG-AMEND1.md` exists it is byte-identical. The
   `started` line's `amend_sha256` records this file's sha256 (it was `none`).
4. **REPO_SHA re-pin.** AMEND1's `REPO_SHA` replaces the prereg's (code changed). The H13 check
   and the code gate run against the new value.

## What does not change

Selection (d6, org-first, gate-window filter), seeds, n, arms and identity pin, `think: false`,
timeouts, retries, Sonnet reference, Haiku report arm, judge, scoring and decision rules, proxy-gate
scope (tech_op only, D-207), privacy rules, quota guard and spend cap.

## Known biases

None added to outcomes: no answer exists in `t1`. The gate now admits calls while the arm's model
is resident and raw free memory is under 40%, the state every local call after warm-up is in.

## Pins

* **Changed by the block below:** `QUIET_FREE_MEM_BASIS` (new) and `REPO_SHA`.
* **REPO_SHA** `ef6ad37850b8b3b731cbb7844315dcf1ef676480` (branch `feat/p20t-amend1`, parent
  `eb25ba96`) is the last commit touching `scripts/`, `src/` or `tests/`. If it is rebased before
  the freeze, REPO_SHA is recomputed.

## Freeze procedure

1. Independent review of `git diff eb25ba96..<REPO_SHA>` and of this doc (verifier is not the
   author).
2. Verify the pin: `git rev-parse` of the code commit equals REPO_SHA.
3. Real-path precondition dry run before committing the sidecar: from a worktree at the freeze
   commit with the sidecar written uncommitted (no run-dir copy yet), run the real `frozen()`,
   `run_gate` (H13 + code gate) and `run()`'s preconditions through the identity check against
   `requal/t1`, plus one real `quiet()` reading set with the arm loaded and one with it unloaded
   (lock held, P2.0 not running, no row written). Counts only. On any failure delete the sidecar
   and do not freeze.
4. Commit `PREREG-P20T-techop-AMEND1.sha256` (`<sha256>  PREREG-P20T-techop-AMEND1.md` plus
   `frozen_utc <date -u>`, after 2026-10-17T18:44:41Z) and a byte-identical 0444 copy to
   `requal/t1/PREREG-AMEND1.md`.
5. Run from a worktree at the freeze commit.

## Constants (add QUIET_FREE_MEM_BASIS; override the prereg's REPO_SHA)

```text P20T-AMEND1-CONSTANTS
QUIET_FREE_MEM_BASIS=excl_own_arm
REPO_SHA=ef6ad37850b8b3b731cbb7844315dcf1ef676480
```
