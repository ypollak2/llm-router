This file is frozen by the hash in `PREREG-P20-requal-AMEND5.sha256`.

# PREREG-P20-requal AMEND5: the free-memory floor counts the arm's own resident model as available

When frozen, this file is frozen by the hash in `PREREG-P20-requal-AMEND5.sha256`.

It amends `PREREG-P20-requal.md` (sha256 `59e27eb889601b4eff80cac645b21442333ff850109219ef10b6de68bd2029b2`,
frozen_utc 2026-10-16T15:43:02Z) as already amended by:

| Doc | sha256 | frozen_utc |
|---|---|---|
| `PREREG-P20-requal-AMEND1.md` | `6c5b867ac97198b70109d1b5961de6ec64e2f1aba3b0b38068c090f70cfdeabd` | 2026-10-16T17:22:05Z |
| `PREREG-P20-requal-AMEND2.md` | `03120fc95cbb2ccbc93f33b5b698093712b15a52f6798a9c87f5033f363dbdc7` | 2026-10-17T00:16:43Z |
| `PREREG-P20-requal-AMEND3.md` | `3b3e981a5ebd99f251381f9f94f659d3ca74ab04cb91cef854164e064c5de741` | 2026-10-17T17:45:51Z |
| `PREREG-P20-requal-AMEND4.md` | `3c60a1268b3221be7173479d1afb0784ba986d2517f74ee03ec7e08d3d514362` | 2026-10-17T17:59:05Z |

It changes only the items below. Everything else in the prereg and AMEND1-4 stands word for word.

## Why

AMEND3's quiet gate requires `memory_pressure -Q` free percentage >= `QUIET_FREE_MEM_MIN_PCT` (40)
before every local call. The arm's own model is resident during its run (keep_alive 10m), and it
is large. Coordinator measurement, 2026-10-17 23:46Z, same machine (48 GB RAM), lock held, no run
active: free 76% before loading `qwen3.8-uncensored:latest`; 27%, 27%, 28% (three readings 5 s
apart) with it loaded at num_ctx 32768; `/api/ps` size 24.6 GB (= size_vram); 77% after unloading.

So every gate after the warm-up fails by construction, independent of any other process. Run
evidence: `requal/b2/quiet.jsonl` has 3 rows, all `stage: warmup` (free 80-85%); every following
gate parked with "machine not quiet" (rc 3); `answers/qa.jsonl` was never written. Over the same
period load1 was not the binding condition: an 18 s, 2423-prompt-token call on a warm model kept
load1 at 2.1-2.6, and a cold load raised it from 1.58 to at most 2.66.

The floor exists to keep other memory consumers from distorting the arm's latency. The arm's own
weights are the treatment, not contention, so they must not count against it.

## Changes

1. **Free-memory basis.** With `QUIET_FREE_MEM_BASIS=excl_own_arm`, each quiet reading passes the
   memory condition when `free_pct + own_pct >= QUIET_FREE_MEM_MIN_PCT`, where
   `own_pct = 100 * resident_bytes(arm) / hw.memsize`. `resident_bytes(arm)` is the sum of
   `/api/ps` `size` over entries whose `name` equals the arm exactly (0 when it is not loaded); a
   foreign model's memory never counts. A non-finite or negative `own_pct` parks. The threshold
   (40), `QUIET_LOAD1_MAX` (4.0), `QUIET_READINGS` (3), `QUIET_INTERVAL_S` (10), the foreign
   resident model check and the exclusive lock are unchanged.
2. **Recorded.** `quiet.jsonl` rows keep the raw `free_mem_pct` readings and add
   `own_resident_pct` (one value per reading).
3. **AMEND5 is enforced.** If `PREREG-P20-requal-AMEND5.sha256` exists, `frozen()` requires all of:
   AMEND4 is frozen; the sidecar matches this file; its `frozen_utc` is timezone-aware, strictly
   after AMEND4's and before now; the constants block has exactly the keys
   `QUIET_FREE_MEM_BASIS` (value `excl_own_arm`) and `REPO_SHA` (40 lowercase hex); if
   `<run-dir>/PREREG-AMEND5.md` exists it is byte-identical. A sidecar without a frozen AMEND4
   refuses. `score`'s `started` line also records `amend5_sha256`.
4. **REPO_SHA re-pin.** AMEND5's `REPO_SHA` replaces AMEND4's, because the code changed.

`scripts/p20_code_runner.py` gates through `p20_requal.local_call` and reads `frozen()`, so the
code stage gets the same basis with no change to that file.

## What does not change

Selection, seeds, n, arms and identity pin, `think: false`, timeouts, retries, references, judge,
scoring and decision rules, privacy rules, quota guard, spend cap and the order of section 13.
`CODE_RUNNER_SHA256` stays `defa41e508fa804b5634a72ffa606cc6d6e6d1940e81ac12fb61753b57430b41`
(`scripts/p20_code_runner.py` is byte-identical; `shasum -a 256` gives that value).

## Known biases

None added to outcomes: no answer or score has been produced under any amendment. The gate now
admits calls while the arm's model is resident and free memory is under 40% raw; that is the
state every local call of this run is in, and it was the intended state under AMEND3.

## Pins

* **Changed by the block below:** `QUIET_FREE_MEM_BASIS` (new) and `REPO_SHA`.
* **REPO_SHA** `e8b96bc4e1e5acaf24640344757221ed623b834e` is the commit with the gate change and
  tests (branch `feat/p20-amend5`, parent `36c0d0b7`), the last commit touching `scripts/`,
  `src/` or `tests/`. If it is rebased before the freeze, REPO_SHA is recomputed.

## Freeze procedure

1. Independent review of `git diff 36c0d0b7..<REPO_SHA>` and of this doc (verifier is not the
   author), including the AMEND4 step-3 rule.
2. Verify pins: `git rev-parse` of the code commit equals REPO_SHA; the CODE_RUNNER_SHA256 above.
3. Real-path precondition dry run before committing the sidecar: from a worktree at the freeze
   commit with the sidecar written uncommitted (no run-dir copy yet), run the real `frozen()`,
   the code gate and `run()`'s preconditions through the identity check against `requal/b2`, plus
   one real `quiet()` reading pair with the arm loaded and not loaded (lock held, no row written
   to `answers/` or `quiet.jsonl`). Record counts only: constants key count,
   `QUIET_FREE_MEM_BASIS`, `ID_qwen3.8-uncensored:latest` present, REPO_SHA equals the pin, code
   gate passes, qa item count, and the two gate outcomes. On any failure delete the sidecar and
   do not freeze.
4. Commit `PREREG-P20-requal-AMEND5.sha256` (`<sha256>  PREREG-P20-requal-AMEND5.md` plus
   `frozen_utc <date -u>`, after 2026-10-17T17:59:05Z) and a byte-identical 0444 copy to
   `requal/b2/PREREG-AMEND5.md`.
5. Run from a worktree at the freeze commit.

## Constants (add QUIET_FREE_MEM_BASIS; override AMEND4's REPO_SHA)

```text P20-AMEND5-CONSTANTS
QUIET_FREE_MEM_BASIS=excl_own_arm
REPO_SHA=e8b96bc4e1e5acaf24640344757221ed623b834e
```
