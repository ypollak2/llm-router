# Remediation run — the action list

Written 2026-09-14. This is the executable form of `PLAN_GAPS_REMEDIATION.md` and
`PLAN_CONTEXT_EVERYWHERE.md`: one row per action, each with the observable gate
that says it is done. Status is updated as each action completes, so this file —
not anyone's memory — is the state of the run.

Baseline every action is measured against: `scripts/bench_session_replay.py`,
5 real sessions replayed in their real order. Current: **82% drafts / 70%
acceptable** (n=66 of 115 at time of writing).

Rule: a gate never asks a question. On failure, repair twice, then PARK the action
with the exact failure recorded and move to the next. Every action ends done,
repaired or parked — never silently dropped.

## Status

| ID | Action | Gate | Status |
|---|---|---|---|
| A1 | Record per-attempt outcome `{model, ok/timeout/empty, latency_ms}` in `execute_chain` | after one real session the store holds timeout rows; harness and store agree on the same run | todo |
| A2 | Make `success` mean usable, not "a response object exists" | a draft that asks a question logs `success=0`; `success_rate` over a real window is < 1.0 | todo |
| A3 | Demote chronically-timing-out models in `build_chain` | after a window containing timeouts the chain order changes; replay wasted wall-clock falls | todo |
| B1 | `acceptance_check` -> `acceptance_argv: list[str]`, `shell=False` | a check string containing `;` or `$(...)` cannot execute a second command; 2 rewritten tests pass | todo |
| B2 | `apply_writes` default True -> False | default invocation writes no file and runs no state-changing command | todo |
| B3 | Correct README/SECURITY security claims | a test pins `shell=False` and the `guard_command` layer, and fails if the docs drift | todo |
| B4 | Pin "no `await` inside the env-mutation window" | a test fails if `llm_local_task` gains an `await` between env set and restore | todo |
| A4 | Repoint the hook at `classify.classify_signals(policy=HOOK_POLICY)` | hook and router agree on complexity for the 3 prompts that currently disagree | todo |
| A5 | Bound `_snapshot` to git-tracked files + include list + size cap | snapshotting a repo containing a `.venv` completes in bounded time | todo |
| C1 | Register 4 pytest markers; fix `tool_intercept.py:380` `"$\{"` | `pytest --collect-only` emits 0 warnings | todo |
| C2 | Align ruff `target-version` with `requires-python = ">=3.11"`; widen `select` | ruff passes on the widened set | todo |
| C3 | `failopen.record` at the 38 silent sites in `hooks/auto-route.py` | no bare `except: pass` on the routing path without a record; a check forbids new ones | todo |

## Evidence behind each action

**A1** `RoutingDecision` is written before the model is called (`auto-route.py:3653`
-> `:3658`) and carries no latency and no outcome. The only latency emit fires for
the winning candidate. Production telemetry cannot see its own failures, which is
why the 72-of-166 timeout figure had to come from an ad-hoc harness. A1 gates A2
and A3; nothing can adapt on data that is not recorded.

**A2** `telemetry.py:117` averages `CASE WHEN success = 1`. Every write site —
`router.py:2043`, `savings_logger.py:340`, `:359` — passes `success=True`; there is
no `success=False` anywhere on the routing path. So `success_rate / avg_cost`
collapses to `1 / avg_cost`. `savings_logger.py:325` builds content as
`getattr(result, "text", "") or ""`, so an empty response logs success too.
Measured consequence: the 35 unusable drafts of 2026-09-14 each reinforced the
model that produced them. `judge.py` already computes and stores `judge_score`,
and `telemetry.aggregate_stats` never selects it — wire the existing signal back
rather than inventing one.

**A3** `hooks/chain_builder.build_chain` orders by `complexity x zone x task_type`
with zero references to `latency_ms`, `timeout` or history. Measured: first model
timed out on 72 of 166 attempts; 50 of a 99-minute run produced nothing. Demote,
never remove — a slow model is still better than no model.

**B1** `tools/local_task.py:93` `subprocess.run(check, shell=True, ...)`. The
parameter is documented as "a shell command" and reaches the shell unsanitised.
`run_command` in the same loop does it correctly (`agent_loop.py:279`, `shlex.split`
+ `shell=False`) — the safe pattern is two files away.

**B2** `tools/local_task.py:109` `apply_writes: bool = True`, setting
`AGENT_WRITES=apply` and `AGENT_COMMANDS=all`. `agent_writes.guard_command:83`
returns True immediately under `all`, skipping the inspection allowlist; what
remains catches `rm -rf /`, `mkfs`, `dd`, `curl|sh` — not `cp`, `mv`, `tee`, `git`.
Writes ARE confined by `_resolve_path`; command arguments are not.

**B3** `README.md:413` and `SECURITY.md:247` state `run_command` executes through a
shell. It does not, and did not at 13.3.1 either. `SECURITY.md` lists 8 commands as
NOT blocked; 6 are blocked today, and `echo $OPENAI_API_KEY` cannot leak because
there is no shell to expand it. Neither file mentions `llm_local_task`.

**B4** The env mutation at `local_task.py:154-158` is NOT a live race: the MCP
server dispatches cooperative anyio tasks and the function contains no `await`
between set and restore, so the window cannot be preempted. No code change — a
test so a future async refactor fails loudly instead of silently becoming a race.

**A4** `hooks/auto-route.py:1335/1502` carry an AST-extracted copy of the
classifier; `router.py:1039` uses the shared engine, which already defines an
unused `HOOK_POLICY`. Reproduced live, 3 of 3 disagreeing, hook always the more
expensive:

    700-char code, no keywords   hook=complex    router=moderate
    short code prompt            hook=moderate   router=simple
    long prose                   hook=complex    router=moderate

`_quarantined_tests/test_hook_equivalence.py` asserted exactly this and is now
unrestorable (`classify.score_categories` was privatised) — the rot is the evidence.

**A5** `tools/local_task.py:70` `rglob("*")` with a 6-entry noise set, no byte cap,
no file-count cap, no duration budget. It descends into `.venv` and `node_modules`.

## Not doing, and why

* The "one planner" rewrite — A4 gets the same correctness by rewiring to a
  classifier that already exists.
* Merging session buckets — breaks the isolation `CHZ-AUD-024` pins; scope to the
  caller's root instead.
* Centralising all 30 subprocess sites into typed profiles — B1 fixes the one
  genuinely risky call.
* Splitting the 4,889-line modules — real tax, no runtime effect, high cost.
* Multi-tenant Definition-of-Done criteria — one user, one laptop.

## Execution

Order: A1 -> A2 -> A3, then B1 + B2 + B3 + B4, then A4, A5, then C1-C3.
A1 gates A2 and A3. B1-B4 are independent of Track A and of each other.

Each action runs the `bug-triage-and-fix` shape (AGR-002,
`~/Projects/agenticgraphs`): plan -> apply in isolation -> verify, whose exit
contract is `output.exit_before != 0 and output.exit_after == 0` — the test fails
before the patch and passes after. That is the discipline already used for every
fix committed today, so the graph formalises the existing practice rather than
replacing it.

Full suite must be green before each commit, and the plugin bundle rebuilt
(`scripts/build_plugin_bundle.py`) when a hook changes.
