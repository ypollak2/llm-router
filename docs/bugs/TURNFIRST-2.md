---
id: TURNFIRST-2
status: fixed in `fix/turnfirst-subagent-main-thread` (deploy: restart the proxy; rows written before it keep the old label)
---
## TURNFIRST-2. A general-purpose sub-agent holds the `Agent` launcher, so its first call was labelled a main-thread `turn_first`

- **Symptom (2026-10-10).** Live ledger `proxy_calls_2.jsonl`, rows after the TURNFIRST-1 deploy
  (2026-10-10 00:15:07Z to 08:17:31Z, n=100 rows, 4 sessions; counts only, no text field read;
  recompute script and `results.json` in the v16 p09 `n14_recompute_20261010` evidence folder): 4 rows
  `step_class == turn_first`, all `is_main_thread: true`, `is_first_call: true`, `turn_origin: typed`.
  The session transcript shows 1 typed human turn in the window (08:07:07Z). The other 3 rows
  (08:15:10Z, 08:15:16Z, 08:15:51Z) were the first calls of 3 `general-purpose` sub-agents (Claude
  Code Agent tool, `spawnDepth: 1`) that the session started at 08:15Z; they share its session id. No
  row in the window was labelled `subagent_*`. So 3 of 4 rows that feed the D-31 Haiku arm and the
  P0.9-e turn-first decision latency were sub-agent briefs.
- **Cause.** `proxy/steps.is_main_thread` (and its copy `haiku_arm.is_main_thread`) answered "main
  thread" whenever the request's tools held `Agent` or `Task`, on the TURNFIRST-1 assumption that a
  sub-agent cannot spawn another. Claude Code now lets sub-agents spawn sub-agents, and a
  `general-purpose` sub-agent (tools `*`) is sent the `Agent` tool, so its requests carry the launcher.
- **Fix.** A request is main-thread only when it holds the launcher AND carries no sub-agent evidence
  (`steps.is_subagent_request`): one of three lines in its `system` field, or the sub-agent-only tool
  `SubagentHandback`. The lines were read from the shipped Claude Code 2.1.296 binary on 2026-10-10.
  The agent runner appends `Messages from the agent that launched you ...` and the
  `Notes: - Agent threads always have their cwd reset between bash calls ...` block to every spawned
  agent's prompt (built-in or custom) after the agent's own prompt. `You are an agent for Claude Code`
  opens the general-purpose prompt. The main thread's prompt holds none of them. A general-purpose
  sub-agent's own request (the one that found this) carried all three and `SubagentHandback`. Any one
  marker vetoes the launcher, so a doubtful call goes to `subagent_*`, not `turn_first`. Only `system`
  is read, never a message, so marker text typed into a prompt keeps the main thread main.
  `haiku_arm.is_main_thread` now calls `steps.is_main_thread`, so the proxy has one rule. A
  positive main-thread marker was not added. The main prompt's opening line changed between versions
  (`You are an interactive CLI tool` before, `You are an agent working with the user toward their
  goals` or the output-style line in 2.1.296). Requiring it would turn every owner main-loop turn
  into `subagent_*` the next time it changes. Residual: a sub-agent whose prompt holds none of the three lines and
  that has the launcher still reads as main thread (a future Claude Code rewording of all three, or
  a client other than Claude Code). A fork (`subagent_type: fork`) re-sends the parent's system
  prompt and tools, so it reads as main thread too. Its first call is a `continuation` (its newest
  turn holds tool results for the parent's tool calls plus the directive), so it is not `turn_first`.
- **Test.** `tests/proxy/test_turn_subagent_launcher.py`: a general-purpose sub-agent first call with
  `Agent` is `subagent_first`, not main thread (also through `haiku_arm.is_main_thread`). Its resume
  is `subagent_turn`. A custom agent with only the runner's appended lines is not main. Each marker
  alone, as a list block or a string `system`, and `SubagentHandback` alone veto the launcher. A
  main-thread typed turn and first call with `Agent` and a 2.1.296-shaped main prompt stay
  `turn_first`. Marker text in a user message does not demote the main thread. A proxy ledger row
  pair labels the two apart under one session id. Mutation check: on `main` (cf444090), 11 of the 15
  tests fail, and the 4 main-thread tests pass.
- **Gates to recompute after deploy, on rows written after it:** P0.9-e turn-first decision p95 and
  the D-31 Haiku arm counts. A window that spans the deploy mixes both labels.
