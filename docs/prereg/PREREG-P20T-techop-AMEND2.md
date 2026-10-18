This file is frozen by the hash in `PREREG-P20T-techop-AMEND2.sha256`.

# PREREG-P20T-techop AMEND2: the spend cap is $60.00, so the judge-cost reserve fits

When frozen, this file is frozen by the hash in `PREREG-P20T-techop-AMEND2.sha256`.

It amends `PREREG-P20T-techop.md` (d6; sha256
`293ae8d2bd6919f35d0e5a2e134eb49001ebc77c9bb54118a13de384aba203b9`, frozen_utc
2026-10-17T18:44:41Z) as already amended by `PREREG-P20T-techop-AMEND1.md` (sha256
`edf23456ce5c4b90c3faf8dc50e62df91d965801656798d28069433eda4a93b4`, frozen_utc
2026-10-17T23:59:25Z). It changes only the item below; everything else stands word for word.
Owner decision D-223 (2026-10-18). The sibling harness already runs at `SPEND_CAP_USD=60.00`
(`scripts/p20_requal.py` line 72).

## Why (arithmetic, outcome-blind)

`scripts/p20t_requal.py` `run()` (N9-RESERVE, lines ~666-676 of the AMEND1 tree) parks the
reference arm whenever `spent + judge_reserve + projected > SPEND_CAP_USD`. For the reference
(Sonnet) arm `projected` is 0, so the test is `spent + judge_reserve > cap`.
`judge_reserve()` (line ~629) with no judge-stage ledger row uses `FIRST_CALL_EST_USD` (0.30):
`1.25 x 0.30 x (120 judge items + 12 re-judge sample) = $49.50`. With `spent = 0` and the cap at
`$40.00`, `49.50 > 40.00` holds before the first Sonnet call, so the Sonnet arm can never start
(`tests/test_p20t_requal.py` `test_reserve_parks_before_first_sonnet_call` asserts rc 3 for exactly
this). At `$60.00` the same test is `49.50 <= 60.00`; the headroom is $10.50 of spend before the
reserve parks again.

The reason is arithmetic on constants frozen before any answer existed. It does not depend on any
answer, judgement, score or verdict.

## What was and was not read

* Read: `docs/prereg/` AMEND1 doc and sidecars, `scripts/p20t_requal.py`, `scripts/p20_requal.py`,
  `scripts/check_prereg.py`, `tests/test_p20t_requal.py`, the run dir's `PREREG.md`,
  `PREREG-AMEND1.md`, `use.log` (one `selected` line), file names in the run dir, and the tail of
  the coordinator's `p20t-run.log` / `PROGRESS.md`.
* Not read: anything under `answers/`, `judgements/`, the pools, `items/`, `inputs/`, `dryrun.json`,
  `selection.json` content, or any prompt-derived text. At drafting time (2026-10-18T19:28Z) the run
  dir had no `answers/` directory and no `ledger.jsonl`; `spent` is 0.

## Changes

1. **Spend cap.** `SPEND_CAP_USD` is `60.00`. All other budget rules are unchanged: ledger
   `$REQ_T/ledger.jsonl`, the 1.25 safety factor, `$0.30` first-call estimate, N9b reserve,
   Haiku skipped if `spent + reserve + projected > cap`, no partial verdict on a cap stop.
2. **Enforcement (code, at REPO_SHA below).** `CONSTANTS_T` keeps `"40.00"`, because `frozen()`
   compares the frozen prereg's constants block to `CONSTANTS_T` byte for byte and that prereg
   cannot be edited. `frozen()` instead applies an AMEND2 override, in force as soon as
   `PREREG-P20T-techop-AMEND2.sha256` exists. It requires AMEND1 in force; the sidecar matches this
   file; `frozen_utc` is timezone-aware, strictly after AMEND1's and before now; the block
   `P20T-AMEND2-CONSTANTS` has exactly `SPEND_CAP_USD` (value `60.00`) and `REPO_SHA`
   (40 lowercase hex); if `<run-dir>/PREREG-AMEND2.md` exists it is byte-identical. The override
   replaces the cap and `REPO_SHA` in the constants `run`/`judge`/`score` use. The `score`
   `started` line adds `amend2_sha256=<this file's sha256>` (`none` without AMEND2).
3. **REPO_SHA re-pin.** AMEND2's `REPO_SHA` replaces AMEND1's. The H13 check and the code gate
   (`git diff --quiet <REPO_SHA> -- scripts src tests`, plus no uncommitted change there) run
   against it.

## Effect on answers already produced (they stay valid)

From the code: an answer row (`call_model` + `local_call`) carries only `item_id, arm, attempt, ok,
text, truncated, error, wall_s, ts` and, for local rows, `quiet` readings. It carries no REPO_SHA, git
sha, prereg or amendment hash, and no cap. The ledger rows carry cost fields only. The cap is read
only in the N9-RESERVE test in `run()` and never for a local arm (the test is `if not local`).
Local calls cost $0.

What `frozen()` and `run()` compare to existing data: the prereg sha and its constants (unchanged),
the `PREREG.md` / `PREREG-AMEND1.md` run-dir copies (unchanged, still byte-identical), the pool,
manifest and selection hashes (unchanged), arm identity from Ollama (unchanged), and
`require_no_foreign`. `finals()` resumes by `(item_id, arm)` on answer rows, so finals written under
AMEND1's tree are reused as they are. The quiet re-check (`unchecked_local_rows`) uses the AMEND1
basis, which is unchanged. The code gate compares the working tree to the new REPO_SHA, not to
earlier rows. The only semantic code change between the two REPO_SHAs is in `frozen()` and the
`started` line; neither touches how a local call is made, gated or recorded
(`git diff ef6ad378..<REPO_SHA> -- scripts src` shows only `scripts/p20t_requal.py`).

So local finals, their quiet readings and the selection remain exactly what the prereg and AMEND1
define, and are scored under the same rules.

## What does not change

Selection, seeds, n, arms and identity pin, `think: false`, timeouts, retries, Sonnet reference,
Haiku report arm, judge, scoring and decision rules, quiet gate (AMEND1), proxy-gate scope, privacy
rules and the quota guard.

## Known biases

None added to outcomes. A larger cap lets the Sonnet reference arm run, which the $40.00 cap
prevented outright; it adds no selection among items or arms. Haiku (report arm) is still skipped
if its projection plus the reserve breaches the cap.

## Pins

* **Changed by the block below:** `SPEND_CAP_USD` and `REPO_SHA`.
* **REPO_SHA** is the last commit touching `scripts/`, `src/` or `tests/` (branch
  `prereg/p20t-amend2`, parent `063f493b`): `0f5a4253674eab73be08d753d35906482e3068dc`. If it is
  rebased before the freeze, REPO_SHA is recomputed.

## Freeze procedure

1. Independent review of `git diff 063f493b..<REPO_SHA>` and of this doc (verifier is not the
   author).
2. Verify the pin: `git rev-parse` of the code commit equals REPO_SHA.
3. Real-path precondition check (read-only `status` against `requal/t1` from the freeze worktree,
   no model call).
4. Commit `PREREG-P20T-techop-AMEND2.sha256` (`<sha256>  PREREG-P20T-techop-AMEND2.md` plus
   `frozen_utc <date -u>`, after 2026-10-17T23:59:25Z) and a byte-identical 0444 copy to
   `requal/t1/PREREG-AMEND2.md`.
5. Run from a worktree at the freeze commit.

## Constants (override SPEND_CAP_USD and REPO_SHA)

```text P20T-AMEND2-CONSTANTS
SPEND_CAP_USD=60.00
REPO_SHA=0f5a4253674eab73be08d753d35906482e3068dc
```
