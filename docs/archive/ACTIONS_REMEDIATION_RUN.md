# Remediation run — the action list

Written 2026-09-14. This is the executable form of `PLAN_GAPS_REMEDIATION.md` and
`PLAN_CONTEXT_EVERYWHERE.md`: one row per action, each with the observable gate
that says it is done. Status is updated as each action completes, so this file —
not anyone's memory — is the state of the run.

Baseline every action is measured against: `scripts/bench_session_replay.py`,
5 real sessions replayed in their real order. Baseline, complete run:
**76% drafts / 66% acceptable** (n=115, 2026-09-14, saved as
session_replay_BASELINE.json).

An earlier partial read of the same run said 82%/70% at n=66. That was the
running total of a run still in progress, not a result — the last 49 prompts
brought it down. A partial rate is not a rate.

Rule: a gate never asks a question. On failure, repair twice, then PARK the action
with the exact failure recorded and move to the next. Every action ends done,
repaired or parked — never silently dropped.

## Status

| ID | Action | Gate | Status |
|---|---|---|---|
| A1 | DONE — Record per-attempt outcome `{model, ok/timeout/empty, latency_ms}` in `execute_chain` | after one real session the store holds timeout rows; harness and store agree on the same run | **done** |
| A2 | DONE — Make `success` mean usable, not "a response object exists" | a draft that asks a question logs `success=0`; `success_rate` over a real window is < 1.0 | **done** |
| A3 | DONE — Demote chronically-timing-out models in `build_chain` | after a window containing timeouts the chain order changes; replay wasted wall-clock falls | **done** |
| B1 | DONE — `acceptance_check` -> `acceptance_argv: list[str]`, `shell=False` | a check string containing `;` or `$(...)` cannot execute a second command; 2 rewritten tests pass | **done** |
| B2 | DONE — `apply_writes` default True -> False | default invocation writes no file and runs no state-changing command | **done** |
| B3 | DONE — Correct README/SECURITY security claims | a test pins `shell=False` and the `guard_command` layer, and fails if the docs drift | **done** |
| B4 | DONE — Pin "no `await` inside the env-mutation window" | a test fails if `llm_local_task` gains an `await` between env set and restore | **done** |
| A4 | **PARKED** — Repoint the hook at the shared classifier | see below | parked |
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

## PARKED: A4

Parked 2026-09-14 before writing any code, on evidence.

The plan assumed the hook and the router SHOULD agree and that repointing the hook
at `llm_router.classify` would be a behaviour-preserving rewire. Both halves are
wrong.

The divergence is deliberate. `classify.py:424` defines ROUTER_POLICY with
`complex_min=2000`, `simple_max=599`, `keyword_complexity=False` and the comment
"Preserves the documented cost fix exactly". The two paths are MEANT to classify
differently; only the duplicated implementation is the defect.

And the shared engine no longer reproduces the hook. Measured over 150 real
prompts x 5 task types:

    agree 448   disagree 302   -> 59.7% identical

The disagreement runs in the expensive direction — `hook=moderate` where
`classify(HOOK_POLICY)=complex` on analyze and research prompts. Rewiring blind
would change routing on ~40% of prompts toward pricier models, which is the
opposite of what this whole effort is for.

What unparks it: decide which engine is CORRECT for the hook (a measurement on the
replay corpus, not a code preference), reconcile HOOK_POLICY to that answer, and
only then delete the duplicate. `_quarantined_tests/test_hook_equivalence.py`
asserted byte-identity and is now unrestorable because `classify.score_categories`
was privatised — that rot is the same finding, and any fix should restore an
equivalence test in whatever form survives.

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
