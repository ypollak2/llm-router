**DRAFT. Not frozen.**

# PREREG-P20-requal AMEND4: AMEND3 constants are merged, not re-amended (harness fix) and REPO_SHA is re-pinned

When frozen, this file is frozen by the hash in `PREREG-P20-requal-AMEND4.sha256`.

It amends `PREREG-P20-requal.md` (sha256 `59e27eb889601b4eff80cac645b21442333ff850109219ef10b6de68bd2029b2`,
frozen_utc 2026-10-16T15:43:02Z) as already amended by:

| Doc | sha256 | frozen_utc |
|---|---|---|
| `PREREG-P20-requal-AMEND1.md` | `6c5b867ac97198b70109d1b5961de6ec64e2f1aba3b0b38068c090f70cfdeabd` | 2026-10-16T17:22:05Z |
| `PREREG-P20-requal-AMEND2.md` | `03120fc95cbb2ccbc93f33b5b698093712b15a52f6798a9c87f5033f363dbdc7` | 2026-10-17T00:16:43Z |
| `PREREG-P20-requal-AMEND3.md` | `3b3e981a5ebd99f251381f9f94f659d3ca74ab04cb91cef854164e064c5de741` | 2026-10-17T17:45:51Z |

It changes only the items below. Everything else in the prereg and AMEND1-3 stands word for word.

## Why

A harness bug, not a design change. `frozen()` in `scripts/p20_requal.py` applied AMEND3's
constants with `amended()`, which first drops every key starting with `ID_` (AMEND2 semantics:
it voids the original prereg's local-arm identity pins). Applied to AMEND3 it also dropped
`ID_qwen3.8-uncensored:latest` (`b3f4c17c6811`), the pin AMEND2 added. `run()` then read
`c[f"ID_{arm}"]`, raised KeyError, and `main()` reported "refusing: invalid input" (rc 2).
No unit test went through the real `frozen()` with all three amendments into `run()`.

Coordinator's report (not independently checked by the author of this draft, who cannot open the
run dir): the first `run --set qa` at 2026-10-17T17:47:14Z refused with rc 2 before any model
call or row; `answers/qa.jsonl` is absent or empty and `quiet.jsonl` has no row. No data was
produced, so no result is affected.

## Changes

1. **Merge fix.** AMEND3's constants are applied as a plain merge over the constants so far. AMEND2
   still uses `amended()`, unchanged. AMEND2's keys (`LOCAL_MODELS`, `CODE_LOCAL_MODELS`,
   `SIZE_ORDER`, `ID_qwen3.8-uncensored:latest`) therefore survive AMEND3 and AMEND4.
2. **AMEND4 is enforced.** If `PREREG-P20-requal-AMEND4.sha256` exists, `frozen()` requires all of:
   * AMEND3 is frozen;
   * the sidecar matches this file;
   * its `frozen_utc` is timezone-aware, strictly after AMEND3's, and before now;
   * the constants block has exactly one key, `REPO_SHA`, 40 lowercase hex characters;
   * if `<run-dir>/PREREG-AMEND4.md` exists, it is byte-identical.

   Without an AMEND4 sidecar behaviour is exactly as before, plus the merge fix. `score`'s
   `started` line also records `amend4_sha256`.
3. **REPO_SHA re-pin.** AMEND4's `REPO_SHA` replaces AMEND3's, because the code changed.

## What does not change

Everything else: selection, seeds, n, the arm list and identity pin, `think: false`, the quiet
gate and exclusive lock (AMEND3), timeouts, retries, the Sonnet reference, Haiku report arm, Opus
judge, scoring and decision rules, privacy rules, quota guard, the spend cap and the order of §13.
`CODE_RUNNER_SHA256` stays `defa41e508fa804b5634a72ffa606cc6d6e6d1940e81ac12fb61753b57430b41`
because `scripts/p20_code_runner.py` is byte-identical to its AMEND3 version
(`git diff c6727ee2 -- scripts/p20_code_runner.py` is empty; `shasum -a 256` gives the value
above). AMEND4 sets no `CODE_RUNNER_SHA256`; AMEND3's value stays in force.

## Known biases

None added. The change does not alter what is run, scored or measured.

## Pins

* **Changed by the block below:** `REPO_SHA` only.
* **REPO_SHA** `e193714364fa2f5a0f4cf3b4adada8b369294126` is the commit with the harness fix and tests (branch `feat/p20-amend4`);
  it is the last commit touching `scripts/`, `src/` or `tests/`. If the coordinator rebases or
  squashes before the freeze, REPO_SHA is recomputed from the final code commit.
* **Unchanged:** every other constant, including `CODE_RUNNER_SHA256`.

## Freeze procedure

1. Independent review of the code diff `c6727ee2..<REPO_SHA>` (verifier is not the author).
2. Verify pins: `git rev-parse <code commit>` equals REPO_SHA above;
   `shasum -a 256 scripts/p20_code_runner.py` equals the CODE_RUNNER_SHA256 above; otherwise edit
   the block, nothing else.
3. Write `PREREG-P20-requal-AMEND4.sha256` as `<sha256>  PREREG-P20-requal-AMEND4.md` plus
   `frozen_utc <date -u>` (after AMEND3's 2026-10-17T17:45:51Z), and a byte-identical 0444 copy to
   `requal/b2/PREREG-AMEND4.md`.
4. Run from a worktree at the freeze commit (the code gate requires `scripts/`, `src/`, `tests/`
   to equal REPO_SHA).

## Constants (override AMEND3's REPO_SHA)

```text P20-AMEND4-CONSTANTS
REPO_SHA=e193714364fa2f5a0f4cf3b4adada8b369294126
```
