DRAFT, not frozen. This file is frozen only when `PREREG-P20-requal-AMEND7.sha256` exists beside it,
after an independent freeze-guard review (the author does not write the sidecar).

# PREREG-P20-requal AMEND7: the quiet gate's load limit is 6.0 (D-375)

When frozen, this file is frozen by the hash in `PREREG-P20-requal-AMEND7.sha256`.

It amends `PREREG-P20-requal.md` (sha256 `59e27eb889601b4eff80cac645b21442333ff850109219ef10b6de68bd2029b2`,
frozen_utc 2026-10-16T15:43:02Z) as already amended by:

| Doc | sha256 | frozen_utc |
|---|---|---|
| `PREREG-P20-requal-AMEND1.md` | `6c5b867ac97198b70109d1b5961de6ec64e2f1aba3b0b38068c090f70cfdeabd` | 2026-10-16T17:22:05Z |
| `PREREG-P20-requal-AMEND2.md` | `03120fc95cbb2ccbc93f33b5b698093712b15a52f6798a9c87f5033f363dbdc7` | 2026-10-17T00:16:43Z |
| `PREREG-P20-requal-AMEND3.md` | `3b3e981a5ebd99f251381f9f94f659d3ca74ab04cb91cef854164e064c5de741` | 2026-10-17T17:45:51Z |
| `PREREG-P20-requal-AMEND4.md` | `3c60a1268b3221be7173479d1afb0784ba986d2517f74ee03ec7e08d3d514362` | 2026-10-17T17:59:05Z |
| `PREREG-P20-requal-AMEND5.md` | `a721dabd2dffde7d7f7e5f1e376e484612d2938f0acd48949420e1a9519e2da7` | 2026-10-17T23:53:15Z |
| `PREREG-P20-requal-AMEND6.md` | `91941022b82c41283406c00f4c85a221de862865f184a92f1f4c89a3d8a2c648` | 2026-11-04T16:00:10Z |

It changes only the item below: the number `QUIET_LOAD1_MAX`. Everything else in the prereg and AMEND1-6
stands word for word.

## Why

Owner decision D-375 (2026-11-04T21:29Z; the choice, verbatim: "Amend: load limit 6.0").

The quiet gate (AMEND3) runs before every local model call: `QUIET_READINGS` = 3 readings,
`QUIET_INTERVAL_S` = 10 s apart; each needs load1 < `QUIET_LOAD1_MAX` (4.0 in the prereg) and free memory,
counting the arm's own resident model (AMEND5), >= `QUIET_FREE_MEM_MIN_PCT` (40); otherwise the call parks
(exit 3). Evidence that the 4.0 gate did not pass on the machine the run has to use:

* **AMEND6 made no local call in 4 h 50 min.** `run-qa` of `a6-qwen3-coder-30b` exited 3 ("machine not
  quiet") 145 times between 2026-11-04T16:37:13Z and 21:27:03Z, one attempt every 2 min: 90 times from
  16:37:13Z to 19:35:39Z, then 55 times from 19:38:49Z to 21:27:03Z after the operator raised the park cap
  (n = 145 `end run-qa rc=3` lines; source
  `/Users/yaliandrona/.rsi/research/primary-plan/v16/requal-amend6/drive-qwen3-coder-30b.log`). The run was
  stopped at 21:29:03Z by its STOP file. That run dir has no `answers/`, `ledger.jsonl` or `quiet.jsonl`
  (0 local calls), and every `combined_usd` sample of `requal-amend6/watch.log` is 0.0 (n = 35 samples,
  2026-11-04T16:02:12Z to 21:42:20Z): 0 USD spent.
* **Load1 the machine shows.** The operator's restart note of 19:38:49Z records "load1 5.4-5.8 from root
  fseventsd, root PerfPowerServices and the owner's Virtualization VM" (source `requal-amend6/pipeline.log`;
  a note, not a log of readings). After the owner restarted fseventsd and PerfPowerServices, the coordinator
  read `sysctl vm.loadavg` 8 times between 21:24:05Z and 21:27:06Z (roughly 30 s apart): 3.94, 3.82, 4.53,
  4.96, 5.00, 4.38, 3.73, 3.85. Four of the eight are under 4.0 and no three consecutive ones are (longest run
  2); all eight are under 6.0 (max 5.00). Parked gates write no row, so the harness holds none of these
  readings; they are the coordinator's.
* **The load is mostly the owner's VM, which stays.** The coordinator reports the Virtualization VM process
  at about 2.2-2.3 CPU cores continuously (`ps`); one reading of mine at about 2026-11-04T21:34Z shows 200.5 %
  (`ps -Ao pcpu`, PID 10505, n = 1). The machine has 15 logical CPUs (`sysctl -n hw.ncpu`, 2026-11-04). The
  owner keeps the VM running.
* **For scale, the run of 2026-10-17/19 (`requal/b2`, qwen3.8) passed the 4.0 gate easily:** its 209 gate
  passes (`b2/quiet.jsonl`, 2026-10-17T21:20:09Z to 2026-10-18T20:59:08Z) hold 627 load1 readings, all under
  4.0 by construction: min 0.42, p50 1.63, p90 2.73, max 3.96.

## Change (one item)

1. **Load limit.** With `PREREG-P20-requal-AMEND7.sha256` present, `QUIET_LOAD1_MAX` is `6.0`, not `4.0`: a gate
   reading passes its load condition when load1 < 6.0. Without that sidecar it is 4.0 and this file is inert.
   `quiet()` is unchanged; it reads the limit from the frozen constants.
2. **AMEND7 is enforced.** If `PREREG-P20-requal-AMEND7.sha256` exists, `frozen()` requires all of: AMEND6 is
   frozen (hence AMEND3 to AMEND5); the sidecar matches this file; its `frozen_utc` is timezone-aware, strictly
   after AMEND6's (2026-11-04T16:00:10Z) and before now; the constants block has exactly the keys
   `QUIET_LOAD1_MAX` (value `6.0`, as that string), `REPO_SHA` (40 lowercase hex) and `CODE_RUNNER_SHA256`
   (64 lowercase hex); if `<run-dir>/PREREG-AMEND7.md` exists it is byte-identical. A sidecar without a frozen
   AMEND6 refuses. Any other key, any other value, a missing key or a duplicate key refuses. `score`'s
   `started` line also records `amend7_sha256`.
3. **Re-pin.** AMEND7's `REPO_SHA` replaces AMEND6's because `scripts/` and `tests/` changed.
   `CODE_RUNNER_SHA256` has the same value as AMEND6's: `scripts/p20_code_runner.py` is byte-identical.

## What does not change

Everything else, in particular: the two models and their pins (`qwen3-coder:30b` 06c1097efce0,
`qwen3.6:35b-a3b-coding` 017eda0b7c34) and the `--model` rule of AMEND6; items, their selection and the pools;
the Sonnet reference, the Haiku report arm, the Opus judge, plants and re-judge; `think: false`; the timeouts
(`LOCAL_TIMEOUT_S` 180, `REF_TIMEOUT_S` 300); retries, answer caps; the grade rule (`eligibility.grade`, gap
<= 3.0 pts at n >= 60, Newcombe LB >= -10); lane verdicts and the kill rule; the D-21 table; the order of
section 13; the free-memory floor (40, with AMEND5's own-model basis), `QUIET_READINGS` (3), `QUIET_INTERVAL_S`
(10) and the foreign-resident-model check; the exclusive lock; privacy rules; the quota guard; and the spend
cap of 60.00 USD per run dir ledger. `src/` is byte-identical to AMEND6's: `git diff 123e2e44 <REPO_SHA> --
src` is empty. A test compares the whole constants dict before and after a frozen AMEND7 for both models and
finds only `QUIET_LOAD1_MAX`, `REPO_SHA` and `CODE_RUNNER_SHA256` changed (it uses stand-in pins; with the real
pins `CODE_RUNNER_SHA256` keeps its value, and the dry run of 2026-11-04 on a scratch sidecar shows exactly
`QUIET_LOAD1_MAX` and `REPO_SHA` changed in the 80 constants, for both models).

The gate still reads load only **before** a call. Neither 4.0 nor 6.0 bounds the load during a call.

`a6-qwen3-coder-30b` is not reused. Each AMEND7 run uses a fresh run dir `a7-<slug>`, so every row of a run
was made under one set of constants. The 60.00 USD total approved by D-374 covers the `a6-*` and `a7-*` run
dirs together (a6 spent 0.00, above); rule R2 holds it, the script's own guard stays per run dir.

## What a higher load limit can do to the measurement (read before using any result)

The code reads load in one place (`quiet()`, before a call). It never uses load to change a request, a
prompt, an option or a scoring rule, and `wall_s` is written to rows and ledgers but never read by `score`.
As far as the code shows, load therefore acts through wall-clock time. Every outcome that depends on local wall-clock time, with
the direction of its effect on the local arm (checked against `scripts/p20_requal.py` and
`scripts/p20_code_runner.py` at `REPO_SHA`):

| Outcome | What happens | Direction for the local arm under load |
|---|---|---|
| qa local call gets no answer in `LOCAL_TIMEOUT_S` = 180 s | exception, `ok` false, one retry; a final failure stays in the paired set and scores as not acceptable (`outcomes` needs `ok` and a true verdict) | **hurts**: more fails, never a removal |
| code local agent exceeds 900 s (`timeout_s=900`) | `timed_out`, `hidden_pass` = false | **hurts** |
| code local agent exits non-zero or emits no events (not a timeout) | `infra_class` `agent_error`; one retry; then `hidden_pass` = None, and the task leaves the lane (`lane_items`), up to `MAX_REF_MISSING_SHARE` 0.10, above that the lane is `not_informative` | **can help**: a failure counted as a removal. Whether load turns a slow request into such an exit depends on Pi's own request handling, which is outside this repository and was not read. b2: 0 `agent_error` in 76 qwen3.8 attempts (2 other infra rows: `cleanup_error` 1, `runner_error` 1) |
| `rx_edit` hidden-test grader (20 of the 75 code tasks) exceeds 120 s | `hidden_pass` = false, for either arm; it runs on this machine right after the agent. The 55 `noncloud` tasks are graded by the research module `run_codex.grade_all`, whose limits are outside this repository and were not read | **either**: hurts the arm whose tests slow down; if the Sonnet reference's tests are slowed more, the gap narrows. Not measured |
| Sonnet code arm (`claude -p` with tools, 900 s) | runs on this machine, **not gated** by the quiet gate, under AMEND6 and AMEND7 alike | **can help** if load pushes the reference into its cap (it fails more, the gap narrows). b2: 0 of 75 final reference rows timed out |
| Sonnet, Haiku, Opus calls in qa (`REF_TIMEOUT_S` 300) | Claude CLI over the network, not gated; a reference failure or a judge error drops the item from the paired set (>10 % dropped: qa `not_informative`) | **no direction** known: changes n. b2 failures are not attributable to load from the data |
| harness deadlines (token scan 120 s, git 120 s) | Refuse, the run stops | none: stops, no changed outcome |

So the code does **not** show that every load effect makes the local arm fail more, and this amendment does
not call the change conservative. The local qa path (timeouts only add fails) and the local code agent's
timeout path do only lose. Three paths can help the local arm in the code lane: failures turned into
removals, a slowed reference, and an asymmetric grader slowdown. Their size is unmeasured.

Size of the paths that hurt, from the only data this harness has, `requal/b2` (qwen3.8, a different model,
every gate pass under 4.0; the numbers are descriptive and say nothing about 4.0 to 6.0):

* qa, 2026-10-17T23:59Z to 2026-10-18T03:09Z: 13 of 125 local attempts were not ok (11 `exception`, all with
  `wall_s` 200.0-200.1, which is the 180 s cap plus the gate's two 10 s sleeps; 2 `empty`); 6 of the 118 final
  rows were not ok. The 112 ok attempts had `wall_s` p50 35.4, p90 146.3, max 191.3 s
  (`b2/answers/qa.jsonl`). `wall_s` includes about 20 s of gate sleeping. The p90 call sat about 54 s under the
  cap, so a modest slowdown would move calls over it. The slowdown factor from load 4-6 is unknown.
* code, 2026-10-18T05:42Z to 20:58Z: 32 of the 75 final qwen3.8 rows ended at the 900 s cap (`timed_out`),
  that is 32 of its 47 fails (`b2/code_results.raw.jsonl`); `wall_s` p50 874 s over 76 attempts
  (`b2/code_attempts.jsonl`). The Sonnet reference, 2026-10-19T07:43Z to 10:00Z: 0 of 75 timed out, `wall_s`
  p50 35.7 s, p90 242.0 s. For a slow local model the code lane sits at its time cap, so it is the lane most
  sensitive to load.
* Load against call time, qa, n = 125 joined (gate row to answer row): rank correlation about +0.15 between
  the highest passed load1 reading of a call and its `wall_s`; the 9 calls whose highest passed reading was
  >= 2.5 had median `wall_s` 107.1 s, the other 116 had 36.0 s. This is confounded (a busy machine is partly
  the previous call's own tail) and is not a dose-response estimate. No passed reading in b2 was above 3.96.

Not measured: whether the local model's text at `temperature` 0 changes with CPU load (no code path makes
it depend on load; it was not tested); how many calls cross a cap at load1 4-6; the load during a call.

Effect on a verdict: a `failed` or `inconclusive` lane may owe part of its misses to load. An `eligible` lane
was reached despite the paths that hurt, but the help paths above could have contributed in the code lane.
Neither is separated by this amendment.

## How the report discloses load (text only: no code enforces it)

`quiet.jsonl` has one row per **passed** gate: the call's tag (`stage`, `set`, `item_id`, `attempt`),
`free_mem_pct`, `load1`, `own_resident_pct` (each three readings) and `resident`. It records the readings each
call passed on. A parked gate writes no row, so the file never holds the readings that parked. The load of a
call after the gate is not recorded by the harness. Each report of an AMEND7 run (the coordinator's recompute
of `result.json` and any claim built on it) must give, per run dir and per stage (`warmup`, `run`, `code`):

1. n gate rows and the distribution of their passed load1 readings: n readings, min, p50, p90, max;
2. how many rows had at least one passed reading >= 4.0 (the calls AMEND6's limit would not have allowed);
3. for the local arm per set, the share of final rows that are not ok (qa) or `timed_out` and `agent_error`
   (code), and the same split by whether the call's highest passed load1 was < 4.0 or in [4.0, 6.0). The split
   is descriptive; it changes no verdict, and a small cell is reported with its n and not interpreted;
4. the `loadavg=` samples of `watch.log` (rule R4) as the coarse load series during the run.

No result may be called conservative with respect to load, and the report states the table of the section
above in one line per row.

Known risk, not a change: the readings 5.4-5.8 noted at 19:38:49Z are under 6.0 by only 0.2-0.6, so the gate
can still park often. `drive.sh` keeps retrying a parked stage every 2 min for up to 24 h (720 tries).

## Run rules (operator, text only: no code in the harness enforces them)

Paths (durable; the scratchpad is not used): `REQ7=/Users/yaliandrona/.rsi/research/primary-plan/v16/requal-amend7`
holds `wt/` (a git worktree of branch `prereg/p20-requal-amend7`, checked out at the freeze commit, created at
the freeze), `agentbin/` (below; built before the freeze), the scripts `env.sh`, `drive.sh`, `pipeline.sh`,
`watch.sh`, `record-env.sh` (copied from `requal-amend6/` and adapted; `requal-amend6/` is history and is not
edited), the read-only `report-load.py` (prints items 1 to 3 of the load disclosure for a run dir) and one run dir
per model, `a7-qwen3-coder-30b` and `a7-qwen3.6-35b-a3b-coding`.

* **R1. One environment for both runs (AMEND6's R1, with REQ7).** Both runs use the same `PATH`, exported
  before every command: `$REQ7/agentbin/bin:$HOME/.local/node/current/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin`.
  `agentbin` is a venv of CPython 3.14.6 with `pytest==9.0.2` and no `llm_router`, plus a byte copy of the
  Claude Code CLI 2.1.317 (`agentbin/bin/claude`, sha256
  `09f153b46b5df9e8b3454716d682a91901de020cbca630f61dec1a015b32b5e9`, equal to
  `~/.local/share/claude/versions/2.1.317`). Before each run `python scripts/p20_prefreeze_smoke.py` (no
  `--claude-call`) must print four PASS lines; the owner runs `--claude-call` once before run 1 (one real call
  under the scratch HOME), as in AMEND6. `pipeline.sh` sets up each run dir with the same commands (pools
  copied from `requal/b2/pools/`, the prereg and AMEND1 to AMEND7 installed 0444, `status`, `select`,
  `record-env.sh`) and `record-env.sh` appends `grader <UTC> path=... python=... pytest=...` and
  `claude <UTC> path=... version=... sha256=...` to `use.log` with the harness's own `append_use`. Run 2 does
  not start unless its two lines equal run 1's in path, versions and sha256.
* **R2. Spend.** Run 2 starts only if the combined `spend_usd` of every `a6-*` and `a7-*` run dir after run 1
  plus 15.00 is <= 60.00. `watch.sh` sums `spend_usd` over **both** `requal-amend6/a6-*` and
  `requal-amend7/a7-*` every 10 min and, at a combined 55.00 USD (stricter than 59.00), writes `STOP` and sends
  SIGTERM to `requal-amend7/pipeline.sh` and `drive.sh` and SIGINT (which keeps what exists, prereg section 13) to
  the harness and code runner processes of `requal-amend7/wt`. The script's own guard (60.00 per run dir)
  stays as it is.
* **R3. Install.** `install-eligibility --confirm` checks `--confirm` and a finished `score`, not the
  independent recompute (prereg section 9 and 13). The operator never runs it; the coordinator runs it after
  the independent recompute of each `result.json`.
* **R4. Load record.** `watch.sh` appends `loadavg=<sysctl -n vm.loadavg>` to each sample of
  `requal-amend7/watch.log`. It reads one sysctl; it generates no load.

## Pins

* **Changed by the block below:** `QUIET_LOAD1_MAX` (4.0 to 6.0), `REPO_SHA` and `CODE_RUNNER_SHA256`; the
  model pins are the prereg's and AMEND6's, unchanged.
* **REPO_SHA** `91ce2391c72375efd9d6150966cd5e0603e7162d` is the commit with the AMEND7 harness change and its
  tests (branch `prereg/p20-requal-amend7`, parent `351ac415`, the AMEND6 freeze commit), the last commit
  touching `scripts/`, `src/` or `tests/`. If it is rebased before the freeze, REPO_SHA is recomputed.
* **CODE_RUNNER_SHA256** `1664c697b9ccbfcdbaee549d24c57a34b4fb486df20fc183a0824c251cd31dee` is
  `shasum -a 256 scripts/p20_code_runner.py` at REPO_SHA, and equals AMEND6's value.
* Branch base: `feat/p20-amend6` at `351ac415583c18e5a9f8c3f111bc8290862220fc`. PR #826 (AMEND6) was open and
  not merged when this was written (checked with `gh pr view 826`, 2026-11-04), so AMEND7 stacks on it.

## Freeze procedure

1. Independent review (verifier is not the author) of `git diff 351ac415..<REPO_SHA>` (it touches
   `scripts/p20_requal.py` and `tests/test_p20_requal.py` only) and of this doc, including the table of
   wall-clock paths against the code.
2. Verify pins: `git rev-parse` of the code commit equals REPO_SHA; the CODE_RUNNER_SHA256 above;
   `git diff 123e2e44 <REPO_SHA> -- src` is empty; the targeted tests pass:
   `tests/test_p20_requal.py tests/test_p20_code_runner.py tests/test_p20_prefreeze_smoke.py tests/test_p20_build_pools.py`
   (2026-11-04 at REPO_SHA: 535 tests, 0 failed, 0 errors, 0 skipped, counted from the junit file; 138 of them
   are the AMEND7 cases).
3. Real-path precondition dry run before committing the sidecar, from a worktree at the freeze commit with the
   sidecar written uncommitted: the real `frozen()` for each model against a fresh run dir (no model call;
   expect `QUIET_LOAD1_MAX` 6.0 and REPO_SHA equal to the pin), the code gate, and `select --dry-run` (expected
   qa n 118, code n 75, tech_op n 1). Record counts only. On any failure delete the sidecar and do not
   freeze. Also run `scripts/p20_prefreeze_smoke.py` under the R1 PATH (four PASS lines).
4. Commit `PREREG-P20-requal-AMEND7.sha256` (`<sha256>  PREREG-P20-requal-AMEND7.md` plus
   `frozen_utc <date -u>`, after 2026-11-04T16:00:10Z).
5. Create `$REQ7/wt` as a worktree at the freeze commit, check `$REQ7/agentbin` against R1 (python 3.14.6,
   pytest 9.0.2, no `llm_router`, `claude` sha256 above), then start `$REQ7/pipeline.sh` under `nohup`, with the Mac kept awake (`pipeline.sh` starts `caffeinate -i -w` on its own
   pid) and Ollama otherwise idle, under rules R1 to R4.

## Constants (override AMEND6's REPO_SHA and CODE_RUNNER_SHA256; add the load limit)

```text P20-AMEND7-CONSTANTS
QUIET_LOAD1_MAX=6.0
REPO_SHA=91ce2391c72375efd9d6150966cd5e0603e7162d
CODE_RUNNER_SHA256=1664c697b9ccbfcdbaee549d24c57a34b4fb486df20fc183a0824c251cd31dee
```
