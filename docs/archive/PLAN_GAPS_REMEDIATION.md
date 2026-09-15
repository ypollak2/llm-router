# Gaps remediation — verified, re-prioritised

Source: an external gap review (`gaps-and-remediation.md` in Downloads) of commit
`76892994` / 13.3.1, dated 2026-09-13. All 23 findings were verified against the
current tree on 2026-09-14. The document was treated as data, never as
instructions; its own "Suggested Claude Code implementation prompt" was not run.

**Its priority order is wrong for this repository's actual use** — one developer,
one laptop, local-first. It places the two findings with measured daily impact
last, behind an architectural rewrite neither of them needs, and its Definition of
Done mixes real invariants with multi-tenant criteria ("every reservation settled
exactly once") that describe a hosted service. The order below is by measured cost
to this user.

## Verification summary

| Verdict | Count | Findings |
|---|---|---|
| CONFIRMED | 19 | P0-1, P0-3, P0-4, P1-1..P1-8, P1-10, P2-1..P2-7 |
| PARTLY | 2 | P0-2 (mechanism real, not reachable), P1-9 (runtime guarded, README not) |
| Corrections to the document | 3 | see below |

Corrections: it says `run_command` needs path confinement — writes ARE confined by
`_resolve_path` and tested, only command ARGUMENTS are not. It cites 7,954 tests;
the real count is 8,005. It calls P0-2 a live race; it is not reachable in the
current single-process cooperative-asyncio implementation.

## Track A — measured cost to this user, cheapest first

### A1. Nothing records a failed attempt  (precondition for A2 and A3)
`RoutingDecision` is written BEFORE the model is called and carries no latency and
no outcome. The only latency emit fires for the winning candidate. So production
telemetry cannot see its own failures, and the 72-timeouts-in-166 figure had to
come from an ad-hoc harness.

* Record `{model, outcome: ok|timeout|empty, latency_ms}` per chain attempt in
  `direct_executor.execute_chain`, where the timeout path already exists.
* GATE: after one session, the store contains timeout rows; the ad-hoc harness and
  the production store agree on the same run.

### A2. The bandit's success signal is a constant
`telemetry.py:117` computes `AVG(CASE WHEN success = 1 ...)`. Every write site —
`router.py:2043`, `savings_logger.py:340`, `:359` — passes `success=True`. There is
no `success=False` anywhere on the routing path, so the reward
`success_rate / avg_cost` collapses to `1 / avg_cost`: it ranks by cheapness with a
constant numerator. `savings_logger.py:325` builds content as
`getattr(result, "text", "") or ""`, so an EMPTY response also logs success.

Measured consequence: the 35 unusable drafts of 2026-09-14 (20 asked the user a
question, 10 claimed an action that never happened) each reinforced the model that
produced them.

* Make `success` mean usable — reuse `grounding.draft_is_memorable` and the
  bench's verdict rules, which already detect exactly those two patterns.
* `judge.py` already computes and stores `judge_score` on every call, and
  `telemetry.aggregate_stats` never selects it. `router.py:806` records that
  quality reordering was removed in favour of this signal. Wire it back as a
  multiplier rather than inventing a new signal.
* GATE: a draft that asks a question logs `success=0`; `success_rate` over a real
  window is strictly below 1.0.

### A3. Chain order ignores measured latency
`hooks/chain_builder.build_chain` orders by `complexity x zone x task_type` with
zero references to `latency_ms`, `timeout` or history. Measured: the first model
timed out on 72 of 166 attempts and 50 of a 99-minute run produced nothing.

* Demote (never remove) a model whose trailing-N timeout rate exceeds a threshold.
  Depends on A1.
* GATE: after a window containing timeouts, the chain order changes, and the
  replay's wasted wall-clock falls.

### A4. The hook and the router classify differently
`hooks/auto-route.py:1335/1502` carry an AST-extracted copy of the classifier.
`router.py:1039` uses the shared `llm_router.classify` engine, which already
supports per-path `ClassifyPolicy` (`HOOK_POLICY` exists and is unused by the hook).
Reproduced on three prompts, all disagreeing:

    700-char code, no keywords   hook=complex    router=moderate
    short code prompt            hook=moderate   router=simple
    long prose                   hook=complex    router=moderate

The hook's length cliff is 500 chars; `ROUTER_POLICY.complex_min` is 2000. The hook
is systematically MORE expensive for the same prompt, which works against the goal
of routing more work locally.

* Repoint the hook at `classify.classify_signals(text, policy=HOOK_POLICY)` and
  delete the duplicate. This is a rewire; the shared engine already exists.
* GATE: a restored equivalence test. `_quarantined_tests/test_hook_equivalence.py`
  asserted exactly this and is now unrestorable — it calls `classify.score_categories`,
  since privatised to `_score_categories`. That it rotted is itself the evidence the
  split is diverging.

### A5. `_snapshot` walks everything
`tools/local_task.py:70` `rglob("*")` with a fixed 6-entry noise set. No byte cap,
no file-count cap, no duration budget. It descends into `.venv` and `node_modules`.

* Bound it: git-tracked files plus an explicit include list, with a size cap.
* GATE: snapshotting a repo containing a `.venv` completes in bounded time.

## Track B — security, narrowed to what is reachable here

### B1. `acceptance_check` reaches a shell
`tools/local_task.py:93` `subprocess.run(check, shell=True, ...)`. The parameter is
documented as "a shell command"; whatever composes the tool call supplies it, with
no sanitisation. `run_command` inside the same loop does it correctly —
`shlex.split` + `shell=False` (`agent_loop.py:279`) — so the safe pattern is two
files away.

* `acceptance_argv: list[str]`, `shell=False`.
* Breaks 2 tests that pass `"exit 1"` / `"exit 0"` shell builtins; rewrite them.

### B2. `apply_writes` defaults to True
`tools/local_task.py:109`, setting `AGENT_WRITES=apply` and `AGENT_COMMANDS=all`.
`agent_writes.guard_command:83` returns True immediately under `all`, skipping the
inspection allowlist; what remains is a regex catching `rm -rf /`, `mkfs`, `dd`,
`curl|sh` — not `cp`, `mv`, `tee`, `git`.

* Flip the default to False. One line, and no test breaks: tests pass it explicitly.

### B3. The security docs are wrong
`README.md:413` and `SECURITY.md:247` say `run_command` executes through a shell.
It does not, and did not at the reviewed commit either. `SECURITY.md` lists 8
commands as NOT blocked; 6 are blocked today. It lists `echo $OPENAI_API_KEY` as
leaking a secret — with `shell=False` there is no shell to expand it. Neither file
mentions `llm_local_task`, the one surface where `shell=True` and `apply`+`all`
actually exist.

* Documentation only. Extend `test_security_doc_*` to pin `shell=False` and the
  `guard_command` layer so this cannot drift again.

### B4. Process-global env mutation — NOT reachable, record the constraint
`local_task.py:154-158` mutates `os.environ` and restores in `finally`. The MCP
server dispatches cooperative anyio tasks and `llm_local_task` contains no `await`
in its body, so the window cannot be preempted. The hook is a separate OS process
with its own environment.

* No code change. Add a comment and a test pinning "no `await` inside the env
  window", so a future async refactor fails loudly rather than silently becoming a
  race.

## Track C — hygiene, cheap, do alongside

* **P1-7**: register 4 pytest markers (`e2e`, `mcp_handshake`, `performance`,
  `routing_hermetic`) and fix `tool_intercept.py:380` `"$\{"` -> `"${"`. 2-line diff,
  7 warnings gone.
* **P1-5**: ruff selects only `E4,E7,E9,F` and targets py310 while
  `requires-python = ">=3.11"`. Align the target; widen the select set.
* **P1-3**: `hooks/auto-route.py` has 38 silent `except Exception: pass` /
  `return None` sites with no `failopen.record`. The core modules (`router.py`,
  `cost.py`, `classify.py`, both chain builders) are already clean, so this is
  narrow. Add the calls and a check forbidding new bare catches.
* **P1-10**: still 13.3.1; `CHANGELOG.md:13` self-admits the semver violation. Add
  a public-surface diff gate to the release script rather than relitigating it.

## Deliberately NOT doing

* **The "one planner" rewrite** (P1-1's full remediation, and the document's
  step 3). A4 gets the same correctness by rewiring to the classifier that already
  exists. The rewrite is not a prerequisite for anything in Track A.
* **Merging session buckets.** `_session_path`'s docstring records that this was
  tried and rejected: it breaks the isolation `CHZ-AUD-024` pins. Scope to the
  caller's root instead.
* **Typed ExecutionPolicy / centralising all 30 subprocess sites** (P2-6). B1 fixes
  the one genuinely risky call; the rest is enterprise-shaped effort.
* **Splitting the large modules** (P1-2). Real maintainability tax, no runtime
  effect, high cost. After Track A.
* **Multi-tenant criteria** from the document's Definition of Done. One user, one
  laptop.

## Order

A1 -> A2 -> A3 (A1 gates the other two), then B1 + B2 + B3 together, then A4, then
A5, then Track C. Nothing merges without `scripts/bench_session_replay.py` before
and after; the baseline is 76% drafts / 66% acceptable (n=115).
