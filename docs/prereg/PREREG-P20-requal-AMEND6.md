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

The 2026-10-19 run cost 14.84 USD (b2 ledger). The script's spend guard reads one run dir's ledger, so
the 60.00 USD total approved by D-374 is held by operator rules R2 and R3 below, not by the script.

`tech_op` stays out of scope. Its set has n = 1 (NOT INFORMATIVE by design, prereg section 4), the set is
terminal at `select`, `run` and `judge` on it exit 4 and make no model call, and `score` writes
`not_informative` with n = 0 for the model, exactly as for qwen3.8. The P2.0-T harness is not touched.

## Known biases

None added to outcomes: no answer or score has been produced under AMEND6. Disclosed before any data:

* **The agent, Claude-call and grader environment differs from the run of 2026-10-19.** The code at
  `REPO_SHA` includes the owner-merged hardening F104, F110, F113, F115, F117 and F122 (PRs #642, #643, #647,
  #651, #653, #659), cherry-picked with `-x`; their added and removed lines equal the merged commits. They were
  written after agents deleted the live install twice (F86, F86-2). The base code (123e2e44) had none of them.
  They change where and how things run, not the items, the packs or the grade rule, but three of the changes
  can reach outcomes, and the qwen3.8 results (qa 13/113, code 27/74) were measured without any of them:
  1. **Code agents.** Pi and `claude -p` with tools run with a scratch HOME, an allow-listed PATH without
     llm-router, and a token scan, so a coding agent sees a different environment.
  2. **Every Claude call.** The Sonnet reference, the Haiku report arm, the Opus judge and the Sonnet code arm
     now run under a scratch HOME with token authentication (`CLAUDE_CODE_OAUTH_TOKEN` read from
     `P20_CLAUDE_CREDENTIALS_FILE`; the Keychain is not visible there), and `claude` is looked up on the
     allow-listed PATH, which excludes `~/.local/bin`, where `claude` is installed on this machine (checked
     2026-11-04: `shutil.which("claude", path=agent_path(<default PATH>))` is `None`; every Claude call would
     error). Rule R1 puts a byte copy of the Claude Code CLI on a PATH directory the allow-list keeps.
  3. **The hidden-test grader.** For the `rx_edit` tasks, F113 and F115 replaced the harness's own interpreter
     (`sys.executable`) by the first python on the allow-listed PATH that has pytest and cannot import
     llm_router. The harness does not pin or record it, and with the default PATH none exists (only
     `/usr/bin/python3` 3.9.6 without pytest: the preflight refuses, and the code runner would exit 2). Rule R1
     pins it, and rule R1 records it in each run's `use.log`. The interpreter and pytest version the
     2026-10-19 grader used were not recorded; the harness venv on 2026-11-04 reports Python 3.14.6 and
     pytest 9.0.2, which are the versions R1 pins, but that is today's reading, not a record of the run.
* **References are re-run per model.** The judge compares all arms in one call, so Sonnet, Haiku and Opus
  calls are made again in each run dir. Each model is graded against its own run's Sonnet, and the two
  Sonnet samples can differ.
* **Prior outcomes.** `qwen3-coder:30b` scored 12/34 on the Haiku-truth [NC] tasks (prereg section 4). No
  outcome for `qwen3.6:35b-a3b-coding` on these items exists.
* The prereg's section 16 biases stand.

## Run rules (operator, text only: no code in the harness enforces them)

Paths (durable; the scratchpad is not used): `REQ6=/Users/yaliandrona/.rsi/research/primary-plan/v16/requal-amend6`
holds `wt/` (a git worktree of branch `feat/p20-amend6`, checked out at the freeze commit), `agentbin/` (a venv,
below) and one run dir per model, `a6-qwen3-coder-30b` and `a6-qwen3.6-35b-a3b-coding`.

* **R1. One environment for both runs.** Both runs use the same `PATH`, exported before every command:
  `$REQ6/agentbin/bin:$HOME/.local/node/current/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin`.
  `agentbin` is a venv of CPython 3.14.6 with `pytest==9.0.2` and no `llm_router`, plus a byte copy of the
  Claude Code CLI 2.1.317 (`agentbin/bin/claude`, sha256
  `09f153b46b5df9e8b3454716d682a91901de020cbca630f61dec1a015b32b5e9`, equal to
  `~/.local/share/claude/versions/2.1.317`); the allow-list keeps that directory because it holds a python.
  Before each run `python scripts/p20_prefreeze_smoke.py` (no `--claude-call`) must print four PASS lines; the
  owner runs `--claude-call` once before run 1 (one real call under the scratch HOME). After `select`, the
  operator appends two lines to the run's `use.log` with the harness's own `append_use` (snippet in the PR
  body): `grader <UTC> path=<python> python=<version> pytest=<version>` and
  `claude <UTC> path=<claude> version=<version> sha256=<sha256>`. Run 2 does not start unless its two lines
  equal run 1's in path, versions and sha256.
* **R2. Spend across the two runs.** Run 2 starts only if run 1's `status` shows `spend_usd` + 15.00 <= 60.00.
  The operator checks `status` of the running run dir at least every 30 minutes, in both runs, and stops the
  harness (SIGINT, which keeps what exists, prereg section 13) when run 1 + run 2 `spend_usd` reaches 59.00:
  60.00 less 1.00, the most a run spends in 30 minutes at the 2026-10-19 rate (14.84 USD in about 7.9 hours of
  call time). The script's own guard (60.00 per run dir) stays as it is.
* **R3. Install.** `install-eligibility --confirm` checks `--confirm` and a finished `score`, not the
  independent recompute (prereg section 9 and 13). The operator never runs it; the coordinator runs it after
  the independent recompute of each `result.json`.

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
   failure delete the sidecar and do not freeze. Also run `scripts/p20_prefreeze_smoke.py` under the R1 PATH
   (four PASS lines; 2026-11-04 result: PASS import_guard, grader_python, node_on_agent_path, claude_token).
4. Commit `PREREG-P20-requal-AMEND6.sha256` (`<sha256>  PREREG-P20-requal-AMEND6.md` plus `frozen_utc <date -u>`,
   after 2026-10-17T23:53:15Z) and a byte-identical 0444 copy of every prereg file into each run dir.
5. Run from `$REQ6/wt` at the freeze commit, one model at a time, with Ollama otherwise idle, under rules R1 to R3.

## Constants (model pins as in the prereg; override AMEND5's REPO_SHA and AMEND3's CODE_RUNNER_SHA256)

```text P20-AMEND6-CONSTANTS
LOCAL_MODEL_CHOICES=qwen3-coder:30b,qwen3.6:35b-a3b-coding
ID_qwen3-coder:30b=06c1097efce0
ID_qwen3.6:35b-a3b-coding=017eda0b7c34
REPO_SHA=c16d3a7b913e6bb2922cf759af9a780fc0b869b8
CODE_RUNNER_SHA256=1664c697b9ccbfcdbaee549d24c57a34b4fb486df20fc183a0824c251cd31dee
```
