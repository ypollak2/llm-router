# PREREG-P20-requal AMEND3: any-time runs under a per-call quiet gate and an exclusive Ollama lock

**DRAFT. Not frozen.** This file becomes binding only when `PREREG-P20-requal-AMEND3.sha256`
exists next to it (the freeze procedure is at the end). Until then the harness ignores it.

It amends `PREREG-P20-requal.md` (sha256 `59e27eb889601b4eff80cac645b21442333ff850109219ef10b6de68bd2029b2`,
frozen_utc 2026-10-16T15:43:02Z) as already amended by `PREREG-P20-requal-AMEND1.md` (sha256
`6c5b867ac97198b70109d1b5961de6ec64e2f1aba3b0b38068c090f70cfdeabd`, frozen_utc 2026-10-16T17:22:05Z)
and `PREREG-P20-requal-AMEND2.md` (sha256
`03120fc95cbb2ccbc93f33b5b698093712b15a52f6798a9c87f5033f363dbdc7`, frozen_utc 2026-10-17T00:16:43Z).
It changes only the items below. Everything else in the prereg, AMEND1 and AMEND2 stands word for
word. When frozen, the harness (`scripts/p20_requal.py`) checks it in `frozen()` the way it checks
AMEND2 (see Changes, item 4).

## Why (owner instruction, D-202)

The owner instructed that the run window of the P2.0 local re-qualification no longer be
night-only, so that the run can start sooner. The rule becomes: the machine must be quiet at the
moment of every local call, at any time of day. This mirrors D-199, which made the same change for
the P2.0-T (tech-op) harness (branch `feat/p20t-techop`).

## Changes

1. **No night window.** The L13 check (00:00-07:00 local) is removed for `run` and for the code
   runner, for the local arm and for the `sonnet` arm. `NIGHT_WINDOW` becomes `none` in the
   constants (the prereg's value `00:00-07:00` is overridden).
2. **The quiet gate runs before every local call**, not only at the start or resume of an arm. A
   local call is: the warm-up `Reply OK.` chat, each `/api/chat` answer attempt (a retry is a new
   call), and each Pi agent attempt in the code runner. The gate itself is unchanged from the
   prereg: three readings 10 s apart, `load1 < 4.0`, free memory `>= 40%`, and no model resident in
   `/api/ps` other than the arm model (`QUIET_*` constants unchanged).
   * On failure the harness **parks** (exit 3) before the call: no model call, no answer row, no
     attempt row. A code-runner park removes the attempt's worktree as any other abort does, and
     the attempt is redone on resume.
   * **Record.** Every call that passed the gate appends one row to `<run-dir>/quiet.jsonl`: `ts`,
     `arm`, `stage`, `set`, `item_id`, `attempt`, the three `free_mem_pct` readings, the three
     `load1` readings and the resident model list. The last row of an item is its last quiet
     reading. Parked attempts add no row. The file is not an input to scoring.
3. **Exclusive lock.** A local call holds an exclusive `fcntl.flock` (`LOCK_EX | LOCK_NB`) on
   `~/.rsi/research/primary-plan/v16/requal/ollama.lock` from before the quiet gate until the call
   returns. The P2.0-T harness takes the same lock on the same path in the same way. If the lock is
   held, the harness parks (exit 3, `ollama lock held`) without waiting. The lock is released on
   every exit path (success, error row, exception, park); a crash releases it with the process.
   The path is `Cfg.lock_path` (default `OLLAMA_LOCK` in the harness), so tests use a temporary
   file.
   * The end-of-arm unload runs only if this run made a local call, and only if the lock is free.
     It is skipped (not parked) when the lock is held, so the harness cannot evict a model the
     other harness is using.
4. **The amendment is enforced.** If `PREREG-P20-requal-AMEND3.sha256` exists, `frozen()` requires
   all of:
   * the sidecar matches this file;
   * its `frozen_utc` is strictly after AMEND2's `frozen_utc` and before now;
   * the constants block has exactly the three keys below, `NIGHT_WINDOW=none`, `REPO_SHA` 40 hex
     characters, `CODE_RUNNER_SHA256` 64;
   * if `<run-dir>/PREREG-AMEND3.md` exists, it is byte-identical.

   AMEND3's `REPO_SHA` and `CODE_RUNNER_SHA256` replace AMEND2's. AMEND2's other keys
   (`LOCAL_MODELS`, `CODE_LOCAL_MODELS`, `SIZE_ORDER`, `ID_qwen3.8-uncensored:latest`) are still
   checked and still apply. `score`'s `started` line also records `amend3_sha256`. Because the
   code gate compares the worktree to `REPO_SHA`, the pre-AMEND3 code cannot run once this code is
   the worktree, and AMEND3 code cannot run against AMEND2's `REPO_SHA`.

## What does not change

Selection (`selection.json`, seeds, n), the arm list and the identity pin, `think: false`,
`num_ctx`, the timeouts (`LOCAL_TIMEOUT_S` 180, `REF_TIMEOUT_S` 300, Pi 900 s), retries, the
Sonnet reference, the Haiku report arm, the Opus judge and its context caps, plants, rejudging,
scoring and decision rules, the foreign-arm guard, privacy rules, the quota guard, the $60.00
spend cap and the order of §13. Nothing in `answers/*.jsonl`, `code_results*.jsonl` or the ledger
changes shape. `use.log` in the live run dir is not touched by this amendment (one `selected`, one
terminal `tech_op` line, no answers since AMEND2).

## Known biases added to §16 (stated before any AMEND3 data)

20. **Daytime contention and thermal state: unmeasured.** Runs may now overlap with ordinary use
    of the machine. The quiet gate bounds load and memory at the instant of each call only; it
    does not bound what happens during a call (up to 180 s, or 900 s for a Pi attempt), nor heat
    from earlier calls. The effect on answer quality is not measured. The direction is
    **conservative for the candidate**: a slow call is more likely to hit the timeout, and
    timeouts are charged to the arm (§6; §16 item 11 for the code runner), never to the machine.
    No result is adjusted for it.
21. **Latency is report-only and not comparable with night runs.** `wall_s` and any latency
    figure from an AMEND3 run speak for a possibly loaded machine. They are not comparable with
    the night-run latencies of the priors and are not a decision input.

## Pins

* **Changed by the block below:** `NIGHT_WINDOW` (`none`), `REPO_SHA`, `CODE_RUNNER_SHA256`.
* **REPO_SHA** `20d51693b6a353a76bf52451bd7fb268b69ce15d` is the commit that contains the harness
  change (branch `feat/p20-amend3`, code commit "feat(p20): AMEND3 code ..."). It is the last
  commit touching `scripts/`, `src/` or `tests/`; this docs commit touches only `docs/`.
  If the coordinator rebases or squashes before the freeze, REPO_SHA is recomputed from the final
  code commit before the sidecar is written.
* **CODE_RUNNER_SHA256** is **recomputed**, because `scripts/p20_code_runner.py` changed (it no
  longer has its own night/gate function and takes the lock plus gate around each Pi attempt).
  Old value (AMEND2): `7fb952a10d024e3281d3a6615ea38275671fb540e023ac89c20f6e12f8017a4a`.
  Check: `shasum -a 256 scripts/p20_code_runner.py`.
* **Unchanged:** every other constant, the source pins, pool sha256s, the build manifest, the
  Claude model ids and the AMEND1 checklist.

## Freeze procedure

1. Independent review of the code diff `f823a400..<REPO_SHA>` (verifier is not the author).
2. Confirm `git rev-parse <code commit>` equals REPO_SHA above and
   `shasum -a 256 scripts/p20_code_runner.py` equals CODE_RUNNER_SHA256 above; otherwise edit the
   block, nothing else.
3. Confirm the P2.0-T harness takes `fcntl.flock(LOCK_EX)` on the same `ollama.lock` path.
4. Write `PREREG-P20-requal-AMEND3.sha256` as `<sha256>  PREREG-P20-requal-AMEND3.md` plus
   `frozen_utc <date -u>` (after AMEND2's 2026-10-17T00:16:43Z), and a byte-identical 0444 copy to
   `requal/b2/PREREG-AMEND3.md`.
5. Run from a worktree at the freeze commit (the code gate requires `scripts/`, `src/`, `tests/`
   to equal REPO_SHA).

## Constants (override AMEND2's REPO_SHA and CODE_RUNNER_SHA256, and the prereg's NIGHT_WINDOW)

```text P20-AMEND3-CONSTANTS
NIGHT_WINDOW=none
REPO_SHA=20d51693b6a353a76bf52451bd7fb268b69ce15d
CODE_RUNNER_SHA256=defa41e508fa804b5634a72ffa606cc6d6e6d1940e81ac12fb61753b57430b41
```
