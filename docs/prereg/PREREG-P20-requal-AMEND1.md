# PREREG-P20-requal AMEND1: no Claude transcripts, an enforced code gate, new REPO_SHA

This file is frozen by the hash in `PREREG-P20-requal-AMEND1.sha256`. It amends
`PREREG-P20-requal.md` (sha256 `59e27eb889601b4eff80cac645b21442333ff850109219ef10b6de68bd2029b2`,
frozen_utc 2026-10-16T15:43:02Z). It changes only the items below; everything else in the prereg
stands word for word. The harness (`scripts/p20_requal.py`) refuses every gated subcommand unless
this amendment is frozen, frozen after the prereg, and its constants block is exactly the two keys
below.

## Why (D-149)

The independent harness review of #523 (at 67322960) passed with SHOULDs and no blocker. Two
findings are fixed before any data is consumed:

* **S2.** `claude -p` calls lacked `--no-session-persistence`. Every Sonnet, Haiku and Opus call,
  and every Sonnet code run, would have written a prompt-and-answer transcript under the real
  `~/.claude/projects`. That breaks prereg §14 ("nothing under `~/.claude`"), and llm-router's
  session TTL (B-02) does not cover those copies.
* **S1.** The §13 rule (`git diff REPO_SHA..HEAD -- scripts/ src/ tests/` empty) was an operator
  rule, not enforced in code.

## Changes

1. **No transcripts.** Every `claude -p` argv the harness builds carries `--no-session-persistence`,
   placed right after `--safe-mode`. That covers the Sonnet and Haiku arms, the Opus judge, and the
   code runner's Sonnet arm, which builds its argv from the same function.
   * Verified on Claude Code 2.1.299 (2026-10-16), synthetic prompts only:
     * In a scratch HOME, `claude --help` lists the flag ("only works with --print").
     * A call with `-p --safe-mode --no-session-persistence --settings {"disableAllHooks": true}`
       returned normally and left no transcript file for its session id under `~/.claude/projects`.
   * **Disclosure.** The three model-id probes of the freeze (about 13:35Z, before this flag)
     each left one transcript of the synthetic prompt "Reply OK" under `~/.claude/projects`. They
     hold no pool data and are left in place; removing them is a coordinator or owner call.
2. **Code gate (S1), enforced in code.** Every harness subcommand except `status` and
   `select --dry-run`, and every code-runner invocation, refuses unless all three hold:
   * `git diff --quiet <REPO_SHA> -- scripts src tests` succeeds;
   * `git status --porcelain -- scripts src tests` is empty;
   * `llm_router.__file__` resolves inside the run worktree.

   `status` reports the gate result (`code_gate`).
3. **The amendment is enforced.** `frozen()` checks this file's sidecar and requires its
   `frozen_utc` to fall between the prereg's `frozen_utc` and now. It overrides `REPO_SHA` and
   `CODE_RUNNER_SHA256` with the block below. `score`'s `started` line also records
   `amend1_sha256`.

## No data consumed

At this amendment, nothing has been selected, run, judged, ingested or scored. `requal/b2` holds
only `inputs/`, `pools/`, `PREREG.md` and now `PREREG-AMEND1.md`. There is no `use.log` and no
`selection.json` (harness `status`: `use_kinds {}`, `selection_n {}`). The pools and the build
manifest are unchanged, so every data pin of the prereg stands.

## REPO_SHA and where runs execute

* **New REPO_SHA `932392cf43bef62dc927798d46fbd3c25faab6d7`.** It is the last code commit, holding
  the three changes above. Its `src/` diff against caaf8d28 is still only
  `src/llm_router/resolver/eligibility_store.py` (+64/−10, `record_competence`).
* **Runs execute from the freeze commit of this amendment**, not from a REPO_SHA checkout. The
  REPO_SHA checkout lacks the prereg sidecar, the fill-ins and this amendment, so the harness would
  refuse there.
* The code gate guarantees that `scripts/`, `src/` and `tests/` in the run checkout equal REPO_SHA.
* This corrects D-145's wording ("the benchmark runs from the REPO_SHA checkout"). D-145's
  conclusion stands: a test-only change after the freeze would change REPO_SHA and need a new
  amendment.

## Pins

* **Changed:** `CODE_RUNNER_SHA256` becomes `222ba8a62946d2cfa3994531c30d6dbfe6fc54ab0ca9c271dad1551f45175634`
  (the runner now calls the code gate). `REPO_SHA` is as above.
* **Unchanged and recomputed at this freeze:**
  * the ten source pins, including the fixtures tree `7c191707…` over 80 files;
  * the three pool sha256s, the build manifest and the proxy copy;
  * the build manifest's source hashes, the `ID_*` model pins and the Claude model ids.

## Code-runner probe (S3, synthetic, about $0.09)

Both probes were run with `--no-session-persistence`, and neither left a transcript.

| Probe | modelUsage keys | Single-key served-model read |
|---|---|---|
| Exact runner argv (`--permission-mode acceptEdits --allowedTools Read,Edit,Write,Glob,Grep,Bash`) on a one-line edit in a scratch git directory (the edit was made) | `["claude-sonnet-5-5"]` | succeeds |
| `--tools ""` on "Reply OK" | `["claude-sonnet-5-5"]` | succeeds |

**Run rule.** If a code run returns an empty served model, the code arm stops. The runner already
records that case as `served_model_mismatch`, which is an infrastructure null and is retried once.

## Pre-run and recompute checklist (D-149; procedure, no code change)

* **S4.** The rx_edit hidden tests sit in `code_runs/<arm>/<id>/hidden_tests`, a sibling of the
  agent's work directory, so the agent can read them via `../`. This is disclosed in the results.
* **S5.** At recompute, report `infra_class` per arm, and the overlap between nulls and Sonnet
  passes.
* **S6.** At recompute, require exactly one `ingested` line in `use.log`, and every code row's
  `runner_sha256` equal to the pin above.
* **S7.** Recompute the fixtures tree hash before and after the code runner.

## Constants (override the prereg's values of these two keys)

```text P20-AMEND1-CONSTANTS
REPO_SHA=932392cf43bef62dc927798d46fbd3c25faab6d7
CODE_RUNNER_SHA256=222ba8a62946d2cfa3994531c30d6dbfe6fc54ab0ca9c271dad1551f45175634
```

## Freeze record

* Written by the build driver (Claude) under D-149. The code changes were written by Codex
  (round 1), plus one driver-added test assertion (D-137).
* The sidecar holds the sha256 of these bytes and `frozen_utc` from `date -u`. A byte-identical
  0444 copy is `requal/b2/PREREG-AMEND1.md`.
* The independent freeze-guard review of this amendment is commissioned by the coordinator.
