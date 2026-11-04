DRAFT, not frozen. This file is frozen only when `PREREG-P20-requal-AMEND6.sha256` exists beside it,
after an independent freeze-guard review (the author does not write the sidecar).

# PREREG-P20-requal AMEND6: the local model under test is a per-run parameter (D-374)

When frozen, this file is frozen by the hash in `PREREG-P20-requal-AMEND6.sha256`.

It amends `PREREG-P20-requal.md` (sha256 `59e27eb889601b4eff80cac645b21442333ff850109219ef10b6de68bd2029b2`,
frozen_utc 2026-10-16T15:43:02Z) as already amended by:

| Doc | sha256 | frozen_utc |
|---|---|---|
| `PREREG-P20-requal-AMEND1.md` | `6c5b867ac97198b70109d1b5961de6ec64e2f1aba3b0b38068c090f70cfdeabd` | 2026-10-16T17:22:05Z |
| `PREREG-P20-requal-AMEND2.md` | `03120fc95cbb2ccbc93f33b5b698093712b15a52f6798a9c87f5033f363dbdc7` | 2026-10-17T00:16:43Z |
| `PREREG-P20-requal-AMEND3.md` | `3b3e981a5ebd99f251381f9f94f659d3ca74ab04cb91cef854164e064c5de741` | 2026-10-17T17:45:51Z |
| `PREREG-P20-requal-AMEND4.md` | `3c60a1268b3221be7173479d1afb0784ba986d2517f74ee03ec7e08d3d514362` | 2026-10-17T17:59:05Z |
| `PREREG-P20-requal-AMEND5.md` | `a721dabd2dffde7d7f7e5f1e376e484612d2938f0acd48949420e1a9519e2da7` | 2026-10-17T23:53:15Z |

It changes only the items below. Everything else in the prereg and AMEND1-5 stands word for word.

## Why

Owner decision D-374 (2026-11-04): requalify the `qa`, `code` and `tech_op` lanes for the two best local
models, `qwen3-coder:30b` and `qwen3.6:35b-a3b-coding`, with this frozen script, a new pre-registered
amendment and the script's own spend cap (`SPEND_CAP_USD` 60.00).

The run of 2026-10-19 used one local arm, `qwen3.8-uncensored:latest` (AMEND2), and wrote qa 13/113
`failed`, code 27/74 `inconclusive` and tech_op `not_informative` (n = 0) to
`~/.llm-router/local_eligibility.json`. The harness pins that model in code (`AMEND2_FIXED`, the single
`ID_` pin, and a `frozen()` that refuses any AMEND2 block that differs), so no amendment document alone
can change the model. This amendment changes the model under test and nothing else, and ships the minimal
harness change that lets `frozen()` accept it.

## Changes

1. **Model under test.** With `PREREG-P20-requal-AMEND6.sha256` present, every subcommand takes
   `--model` (a top-level option of `scripts/p20_requal.py` and an option of `scripts/p20_code_runner.py`).
   The value must be exactly `qwen3-coder:30b` or `qwen3.6:35b-a3b-coding`; any other value, or none,
   refuses. For the run, `frozen()` sets `LOCAL_MODELS`, `CODE_LOCAL_MODELS` and `SIZE_ORDER` to that one
   model and replaces every `ID_` pin (AMEND2's qwen3.8 pin included) by the one pin of that model from the
   block below. The arms are therefore the model, `sonnet` and `haiku` (qa) or the model and `sonnet`
   (code), as in the prereg. Before the AMEND6 sidecar exists `--model` refuses.
2. **One run per model, one run dir per model.** `select` writes a `model <UTC> model=<model>` line to
   `use.log` before any other line. A run dir whose `use.log` names another model refuses
   ("run dir is bound to another model"), and so does a run dir that has a `selected` line but no `model`
   line, which includes `requal/b2` (the qwen3.8 run dir, which is also consumed: its `use.log` holds a
   `started` line). A fresh run dir is created per model and receives a copy of `b2/pools/` (the three
   pool files and `build_manifest.json`, whose sha256 pins are checked by `select` unchanged).
3. **AMEND6 is enforced.** If `PREREG-P20-requal-AMEND6.sha256` exists, `frozen()` requires all of:
   AMEND3, AMEND4 and AMEND5 are frozen; the sidecar matches this file; its `frozen_utc` is
   timezone-aware, strictly after AMEND5's and before now; the constants block has exactly the keys
   `LOCAL_MODEL_CHOICES`, `ID_qwen3-coder:30b`, `ID_qwen3.6:35b-a3b-coding`, `REPO_SHA` (40 lowercase hex)
   and `CODE_RUNNER_SHA256` (64 lowercase hex), with the first three equal to the code's copy; if
   `<run-dir>/PREREG-AMEND6.md` exists it is byte-identical. A sidecar without a frozen AMEND5 refuses.
   Any other key, any other value, a missing key or a duplicate key refuses. `score`'s `started` line also
   records `amend6_sha256` and `model`.
4. **Re-pin.** AMEND6's `REPO_SHA` and `CODE_RUNNER_SHA256` replace the earlier ones, because `scripts/`
   and `tests/` changed (item 3 and the hardening in "Known biases").

## What does not change

Items and their selection (`SELECT_SEED`, the pools, `N_REALISED_*`, the session cap), the Sonnet reference,
the Haiku report arm, the Opus judge, plants and re-judge, `think: false`, timeouts, retries, answer caps,
the grade rule (`eligibility.grade`, gap <= 3.0 pts at n >= 60, Newcombe LB >= -10), the lane verdicts and
the kill rule, the D-21 table, the order of section 13, the quiet gate and exclusive lock (AMEND3, AMEND5),
privacy rules, the quota guard, and the spend cap of 60.00 USD per run dir ledger. `src/` is byte-identical to
the 2026-10-19 run: `git diff 123e2e44 <REPO_SHA> -- src` is empty, so the context pack, `grade`,
`record_competence` and the local model options are the run's of 2026-10-19.

Spend across the two runs is held to 60.00 USD by an operator rule, because the script's guard reads one run
dir's ledger: the second run starts only if the first run's `status` shows `spend_usd` plus 15.00 is <= 60.00,
and the operator stops at a combined 60.00. The 2026-10-19 run cost 14.84 USD (b2 ledger).

`tech_op` stays out of scope. Its set has n = 1 (NOT INFORMATIVE by design, prereg section 4), the set is
terminal at `select`, `run` and `judge` on it exit 4 and make no model call, and `score` writes
`not_informative` with n = 0 for the model, exactly as for qwen3.8. The P2.0-T harness is not touched.

## Known biases

None added to outcomes: no answer or score has been produced under AMEND6. Disclosed before any data:

* **Agent isolation differs from the run of 2026-10-19.** The code at `REPO_SHA` includes the owner-merged
  hardening F104, F110, F113, F115, F117 and F122 (PRs #642, #643, #647, #651, #653, #659), cherry-picked
  with `-x`; their added and removed lines equal the merged commits. They were written after agents deleted
  the live install twice (F86, F86-2). Agents (Pi and `claude -p` with tools) now run with a scratch HOME, an
  allow-listed PATH without llm-router, and a token scan. This changes the environment a coding agent sees,
  not the task, the grader or any pack. The qwen3.8 code result (27/74) was measured without it. The
  Claude children authenticate through `P20_CLAUDE_CREDENTIALS_FILE` (F104-AGENT-SCRATCH-HOME.md, "Auth").
* **References are re-run per model.** The judge compares all arms in one call, so Sonnet, Haiku and Opus
  calls are made again in each run dir. Each model is graded against its own run's Sonnet, and the two
  Sonnet samples can differ.
* **Prior outcomes.** `qwen3-coder:30b` scored 12/34 on the Haiku-truth [NC] tasks (prereg section 4). No
  outcome for `qwen3.6:35b-a3b-coding` on these items exists.
* The prereg's section 16 biases stand.

## Pins

* **Changed by the block below:** `REPO_SHA` and `CODE_RUNNER_SHA256`; the model pins are the prereg's
  (`ID_qwen3-coder:30b` 06c1097efce0, `ID_qwen3.6:35b-a3b-coding` 017eda0b7c34; `ollama list` equals both on
  2026-11-04).
* **REPO_SHA** `c16d3a7b913e6bb2922cf759af9a780fc0b869b8` is the commit with the AMEND6 harness change and
  its tests (branch `feat/p20-amend6`, parent `dc27f981`), the last commit touching `scripts/`, `src/` or
  `tests/`. If it is rebased before the freeze, REPO_SHA and CODE_RUNNER_SHA256 are recomputed.
* **CODE_RUNNER_SHA256** `1664c697b9ccbfcdbaee549d24c57a34b4fb486df20fc183a0824c251cd31dee` is
  `shasum -a 256 scripts/p20_code_runner.py` at REPO_SHA.
* Branch base `origin/feat/p20-amend5` (123e2e44) plus the six hardening commits above.

## Freeze procedure

1. Independent review (verifier is not the author) of `git diff 123e2e44..<REPO_SHA>` for the AMEND6 commit
   (`git show <REPO_SHA>`), that the six hardening commits equal PRs #642 to #659 line for line, and of this doc.
2. Verify pins: `git rev-parse` of the code commit equals REPO_SHA; the CODE_RUNNER_SHA256 above;
   `git diff 123e2e44 <REPO_SHA> -- src` is empty; the targeted tests pass:
   `tests/test_p20_requal.py tests/test_p20_code_runner.py tests/test_p20_prefreeze_smoke.py tests/test_p20_build_pools.py`.
3. Real-path precondition dry run before committing the sidecar, from a worktree at the freeze commit with the
   sidecar written uncommitted: the real `frozen()` for each model against a fresh run dir (no model call),
   the code gate, and `select --dry-run` (expected qa n 118, code n 75, tech_op n 1). Record counts only. On any
   failure delete the sidecar and do not freeze.
4. Commit `PREREG-P20-requal-AMEND6.sha256` (`<sha256>  PREREG-P20-requal-AMEND6.md` plus `frozen_utc <date -u>`,
   after 2026-10-17T23:53:15Z) and a byte-identical 0444 copy of every prereg file into each run dir.
5. Run from a worktree at the freeze commit, one model at a time, with Ollama otherwise idle.

## Constants (model pins as in the prereg; override AMEND5's REPO_SHA and AMEND3's CODE_RUNNER_SHA256)

```text P20-AMEND6-CONSTANTS
LOCAL_MODEL_CHOICES=qwen3-coder:30b,qwen3.6:35b-a3b-coding
ID_qwen3-coder:30b=06c1097efce0
ID_qwen3.6:35b-a3b-coding=017eda0b7c34
REPO_SHA=c16d3a7b913e6bb2922cf759af9a780fc0b869b8
CODE_RUNNER_SHA256=1664c697b9ccbfcdbaee549d24c57a34b4fb486df20fc183a0824c251cd31dee
```
