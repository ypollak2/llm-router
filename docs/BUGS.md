# Bugs

One entry per bug: symptom, cause, fix, and the test that keeps it closed. A bug that is
found but not fixed yet is listed with its **Status** and the task that fixes it; its test
line then names the test that task must add, not one that exists. Numbers carry their n and
source. Counts from the owner's machine are read from `~/.llm-router` and the 2026-10-06
`llm-router kpi --days 7 --json` run on c2ed278, unless a line says otherwise.

| # | Bug | Status |
|---|---|---|
| 1 | NULL `session_id` on local routing rows | fix open in #288 (M0.4), not merged |
| 2 | O3 counted more turns than the user typed | open, fix is plan task M0.3 |
| 3 | NS and D2 counted a heuristic "used" | open, fix is plan task M0.2 |
| P09-5 | status-bar waited on a locked usage.db on every prompt | fixed in `perf/status-bar-cache` (P0.9 task 4) |
| 4 | `G1_proxy` printed 0 ms | fixed in this change (M0.6) |
| 5 | Haiku 400 on a mid-conversation system message | worked around (flag off); fold is plan task M0.7 |
| P09-3 | A session-start background child wrote its own "session-start" latency row | fixed in `perf/session-start-bg` (P0.9) |
| P09-4 | session-start ran Ollama start, `ollama list`, seats, usage.db and git inline | fixed in `perf/session-start-bg` (P0.9 task 3) |
| P09-6 | Stop (session-end) ran a keychain read + HTTPS usage fetch and three maintenance jobs inline on every turn | fixed in `perf/session-end-bg` (P0.9 task 7) |
| 6 | Research session b9f04425 counted as organic | fixed in #291 (M0.0b) |
| 7 | `edit_outcomes.jsonl` rows with no source | open, fix is plan task M0.3(c) |
| 8 | `DISABLE_LLM_CLASSIFIERS` auto-detect turns the hook's Ollama layer off | fixed in P0.7 (`fix/learning-bugs`): one flag, default off |
| P09-2 | Proxy tier decision took 1.2-2.2 s on rules-only turn-first calls | fixed in `perf/proxy-decision` (P0.9 task 6) |
| 9 | Classifier warm-up loaded `llmr-classifier` at the wrong `num_ctx` | fixed in #298 (M1.4, review 2) |
| 10 | README-advertised `--host pi` / `--host kimi` failed; detected gemini-cli skipped silently | fixed in this change (v16 P0.4) |
| 11 | Four shadow tests raced the clock and failed `main` on a loaded runner | fixed in this change (test-only) |
| 12 | Learned routes keyed by tool name, looked up by task type | fixed in P0.7 (`fix/learning-bugs`) |
| 13 | Retrospective accuracy 100% at 0 corrections | fixed in P0.7 (`fix/learning-bugs`) |
| 14 | Gateway doors: case-sensitive "auto", `stream` dropped, `max_tokens`/`temperature`/system dropped | fixed in this change (v16 P0.6) |
| 15 | MCP routing ran on Claude pressure 0.0 for the life of the process | fixed in this change (v16 P0.2) |
| 16 | Critical-pressure override sent `/model claude-opus-4-6`, a retired id | fixed in this change (v16 P0.2) |
| 17 | Semantic cache never hit, ignored context, and reported a hit rate of 0 | fixed in this change (v16 P0.5) |
| 18 | Session context store deleted after every turn | fixed in #307 (v16 P0.1) |
| 19 | Session context truncation dropped the newest events | fixed in #307 (v16 P0.1) |
| 20 | `build_context_messages` cut the caller's live context first | fixed in #307 (v16 P0.1) |
| 21 | `context_prep` truncated the user prompt | fixed in #307 (v16 P0.1) |
| 22 | DIRECT rows wrote 0.0 / False / "balanced" for values nobody measured | fixed in this change (v16 P0.8) |
| 23 | `usage` rows carried no session id | fixed in this change (v16 P0.8); live coverage pending deploy |
| 24 | The Stop line's north star used the heuristic "used", kpi the strict rule | fixed in this change (v16 P0.8) |
| 25 | Status bar priced its baseline at Opus and labelled it "vs Sonnet" | fixed in this change (v16 P0.8) |
| 26 | `llm-router replay` raises TypeError on a row with NULL confidence | fixed in this change (v16 P0.8) |
| 27 | The quality report and the Stop summary raise TypeError on a NULL task type | fixed in this change (v16 P0.8) |
| 28 | `llm-router northstar` showed the heuristic share as the North Star | fixed in this change (v16 P0.8) |
| 29 | The claw-code Stop hook and the dashboard models panel raise TypeError on a NULL task type | fixed in this change (v16 P0.8 r1) |
| P013-1 | `llm_act` wrote files into the MCP process cwd | fixed for the file tools in this change (P0.13); bash confinement is P2.9 |
| P0.14-a | Proxy ledger wrote 0 rows for 25 h and nothing flagged it | fixed in this change (P0.14) |
| 18 | Hook DIRECT and SDK served Q&A from local providers (D-14 held only in MCP) | fixed in this change (v16 P0.3) |
| P011-1 | Haiku guard re-tripped on audit days older than its window | fixed in `feat/haiku-guard-in-repo` (P0.11, 3f4149b) |
| GE6-1 | Quota-burn coverage kept owner-overridden sessions in the organic denominator | fixed in `feat/quota-samples` (#320, GE6 repair 1) |
| GE6-2 | Branch hook version equal to main's after main moved on | fixed in `feat/quota-samples` (#320, GE6 repair round 1) |
| CODEX-1 | Codex refused to start: `invalid transport in mcp_servers.llm_router` | fixed in this change (#323) |
| P09-1 | G1 called a 16 s auto-route p95 "within budget" | fixed in `perf/hook-budgets` (P0.9 tasks 1-2) |
| P09-7 | Statusline timing rows carried no session id, and needed a python3 that imports llm_router | fixed in `perf/hook-budgets` (P0.9 repair 1) |
| P09-8 | The statusline "wrapper adds < 5 ms" test failed under load | fixed in `perf/hook-budgets` (P0.9 repair round 1, test-only) |
| P09-9 | A session id named by one test leaked onto latency rows of later tests | fixed in `perf/hook-budgets` (P0.9 repair round 1, test-only) |

## 1. NULL `session_id` on local routing rows

- **Symptom.** O3 printed "local share unknown". In `usage.db`, 0 of 544 runtime
  `routing_decisions` rows had a `session_id`, including 444 Ollama-served rows (111 of them
  in the 7 days to 2026-10-06; #288 body, read-only on the live `usage.db`). A local answer
  could not be scoped to an organic session, so no local KPI could count it.
- **Cause.** The router's finalizer wrote `routing_decisions` without a session id. The hook
  payload carried one, and the hook accepted it and then dropped it.
- **Fix.** #288 (`feat/local-session-accepted`): stamp `CLAUDE_CODE_SESSION_ID` from the MCP
  server's own env, only inside an MCP tool call, and the hook payload's id on the hook path.
  A row without a trustworthy id stays NULL; the global `current_session.json` pointer is not
  used, because with two sessions open it names the wrong one. Open at the time of writing.
- **Test.** M0-3: at least 19 of 20 synthetic rows carry the calling session's own id across 2
  concurrent research sessions. #288 carries its own tests (a fresh pointer with no env stores
  NULL). The hook payload's id is pinned on all three agent-route ledger paths (DIRECT, NS3
  Codex, Phase 2 CLI delegation) by `test_every_agent_route_ledger_path_stamps_the_payload_session`;
  a review mutation that passed main()'s own pointer-backed `session_id` to two of the three had
  survived until that test was added. Not on `main` yet.

## 2. O3 counted more turns than the user typed

- **Symptom.** In session b9f04425, 424 proxy turns against 370 typed prompts in the
  transcript, a ratio of 1.146 (research findings, W0). The O3 integrity bar is 0.90-1.10.
- **Cause.** Two kinds of rows were read as human turns: first calls of sub-agents (sidechain
  transcripts), and local MCP answers from `usage.db` (they happen inside a Claude turn, so
  they are not turns).
- **Fix.** Plan task M0.3: sub-agent first calls are excluded and counted in
  `o3.excluded.subagent_first`; MCP local units are reported as `o3.breakdown.local_assist_n`,
  outside both sides of the rate.
- **Test.** `tests/test_offload_share_alignment.py` (to be added by M0.3), plus the
  integrity check `o3_integrity.py --since --until` on W0: pooled and per-session ratio in
  0.90-1.10. Not on `main` yet.

## 3. NS and D2 counted a heuristic "used"

- **Symptom.** The owner's rule is that Q&A never counts toward NS and that "used" needs a
  passing test. The code did not enforce it: seven heuristic setters in `northstar.py`
  (lines 614-1266 on c2ed278) mark units `used` from transcript signals, whatever the task
  type, and NS counts `outcome == "used"`.
- **Cause.** "Used" was inferred from behaviour (a draft accepted, a tool result reused),
  not from a verified result.
- **Fix.** Plan task M0.2: strict rule (served by a non-Claude model, `verify_status` in
  {`pass_f2p`, `pass_f2p_model`}, not Q&A, not redone); the heuristic figure stays as
  `NS_heuristic` / `D2_heuristic`, labelled "not a target".
- **Test.** `tests/test_kpi_strict_used.py`, 11 cases (to be added by M0.2). Not on `main`
  yet. Until then, `docs/repo_goals/KPIS.md` says NS is the heuristic one.

## P09-5. status-bar waited on a locked usage.db on every prompt

- **Symptom.** status-bar p95 4,488 ms (n = 344) against the PRD's 300 ms [HL7]; p50 44 ms.
- **Cause.** The UserPromptSubmit hook computed its line inline: `sqlite3.connect(usage.db,
  timeout=2)` (a writer's lock costs up to 2 s per connect), a second connect for the session
  call counts, and the Gemini quota read.
- **Fix.** A detached refresher (`status-bar.py --refresh-cache`, one per 15 s at most)
  computes the line into `status_bar_cache.json` (TTL 30 s). The hook reads that file, shows
  a line up to 10 minutes old, and prints nothing rather than wait when there is none. The
  refresher's own run writes no status-bar latency row (see P09-3).
- **Test.** `tests/test_p09_status_bar_cache.py::test_the_prompt_path_never_waits_on_a_locked_usage_db`
  (FAILS on da31df7: 2,016 ms), `test_a_stale_cache_returns_fast_shows_the_line_and_spawns_one_refresher`,
  `test_the_refresher_survives_sqlite_raising_and_the_hook_still_shows_a_line`.

## 4. `G1_proxy` printed 0 ms

- **Symptom.** `llm-router kpi` showed the proxy half of G1 as 0 ms (primary plan, source
  [K]), although the ledger's `tier_decision_s` had p50 4 ms and p95 83 ms for turn-first
  calls (W0, organic plus research sessions, n=1,181).
- **Cause.** `_g1_proxy` took the p95 of `added_latency_s`. That field is 0.0 on 25,924 of
  25,963 forwarded rows (all-time copy of the ledger, 2026-10-07): it is set only on a few
  decision paths. The time the tier decision took is in `tier_decision_s`.
- **Fix.** `commands/kpi.py` `_g1_proxy` now reports p50 and p95 of `tier_decision_s`, with
  n, for turn-first and continuation calls apart. Side calls are left out and counted. A
  segment below n=50 prints `too few to tell`.
- **Test.** `tests/test_kpi_command.py::test_g1_proxy_reports_tier_decision_s_split_turn_first_and_continuation`
  (exact p50/p95 on a fixture ledger), `test_g1_proxy_is_never_zero_when_forwarded_rows_added_nothing`
  (the regression itself) and `test_g1_proxy_thin_segment_says_too_few_and_missing_field_is_not_measurable`.
  The pinned goldens `tests/golden/kpi_pre_o3_*` were updated for the one reason string that
  changed (`added_latency_s` to `tier_decision_s`); nothing else in them moved.

## 5. Haiku 400 on a mid-conversation system message

- **Symptom.** A live smoke call to Haiku 4.5 returned `invalid_request_error`: "role
  'system' is not supported on this model".
- **Cause.** Claude Code 2.1.285 sent a `role: "system"` entry inside `messages` on every
  call (docstring of `_has_mid_conversation_system_message`, `proxy/tiers.py`). Haiku 4.5
  rejects the role. Nobody has re-checked the current Claude Code version. The ledger has 0
  `haiku_rewrite` rows, ever.
- **Fix.** The body is excluded from the Haiku rewrite when it carries such a message, so
  today the path cannot fire for those bodies. `haiku_rewrite` is suspended in the live
  tier config until the M2 canary (owner decision D-15). #289 logs `has_mid_system` and
  `tier_haiku_block` per row, so the prevalence is measured. The fold (rewrite the body so
  Haiku accepts it) is plan task M0.7, behind a flag that stays off.
- **Test.** Existing: `tests/test_proxy_tiers.py` (the body-eligibility tests). M0.7 adds a smoke on a separate proxy port: at least 19 of 20
  Haiku-decided calls served with no `tier_retry`.

## P09-3. A session-start background child wrote its own "session-start" latency row

- **Symptom.** A detached child that re-runs `session-start.py` (`--background-usage-refresh`:
  keychain plus up to 3 OAuth attempts) arms the KPI G1 latency stanza at import like the hook
  itself, so its whole runtime would be recorded as one `session-start` invocation. Found by
  reading the code while adding the P0.9 child (`--background-session-work`), which would have
  added one such row per session start. Not separable in [HL7]: rows carry no argv.
- **Cause.** The stanza arms whenever `__name__ == "__main__"`, and a re-run of the file is
  `__main__` too.
- **Fix.** `_entry(argv)` sets `LLM_ROUTER_HOOK_LATENCY=off` for any `--background-*` child
  before it runs; the recorder checks that switch when it writes at exit. The status-bar
  refresher (`perf/status-bar-cache`) does the same.
- **Test.** `tests/test_p09_session_start_bg.py::test_a_background_child_writes_no_session_start_latency_row`,
  `test_the_hook_itself_keeps_the_recorder_on`.

## P09-4. session-start ran Ollama start, `ollama list`, seats, usage.db and git inline

- **Symptom.** session-start p95 16,178 ms (n = 65) against the PRD's 2,000 ms [HL7]. In a
  copy of `hook_latency.jsonl` + `.1` (2026-10-04T22:07Z to 2026-10-07T14:47Z, same 65 rows),
  28 rows fell in one burst (2026-10-06 05:07-05:08Z, 4.6-17.4 s). The other 37 read
  138 ms-11.3 s: 11 of them over 2 s, p95 11,045 ms. So the tail is not only the burst.
  **Correction (P0.9 task 7 re-count, PLAN §1.4 rule 5 episodes = rows over the bar split by
  gaps >= 60 s).** 10 of those 11 ran 2026-10-06 05:06:49-05:06:57Z, seconds before the 05:07
  minute, so they belong to the same burst. Same copy, 66 rows to 2026-10-08T11:02Z: 39 over
  2 s, 38 in one episode (05:06:49-05:08:04Z, share 0.974); outside it 1 of 28 rows is over
  2 s and p95 is 723 ms. The tail before the fix was essentially the one burst
  (`$PP/v16/p09/baseline_predeploy_20261008T1102Z.json`).
- **Cause.** `main()` ran `start-ollama.sh` (waits up to 10 s), `ollama list`, a seats
  re-detect (2 s budget), two usage.db queries, an Ollama co-residency probe, the pxpipe sync,
  a `git` check for the OKF index and five process spawns before returning. Under a burst of
  concurrent session starts each of those contends with the others.
- **Fix.** One detached child (`--background-session-work`) runs all of it. Its hint lines go
  to `session_start_hints.json` and the next session start shows them (dropped after 24 h).
  `main()` keeps the session tag, the stale-state reset, the proxy health line, the banner
  from cached usage and `additionalContext`.
- **Test.** `tests/test_p09_session_start_bg.py::test_main_does_not_run_any_moved_step_inline`
  (FAILS on da31df7: all 17 steps ran inline), `test_main_returns_while_a_5s_background_phase_still_runs`.

## P09-6. Stop (session-end) ran a keychain read + HTTPS usage fetch and three maintenance jobs inline on every turn

- **Symptom.** session-end p95 3,353 ms (n = 252) against the PRD's +300 ms sync bar [HL7].
  The Stop hook fires after every turn, so every turn paid it. The row carried no phases, so
  nothing said where the time went.
- **Cause.** `main()` called `_get_cc_usage()`, which ran `security find-generic-password`
  and an HTTPS call to the usage endpoint (8 s timeout) inline, then rebuilt the learned
  profile, ran the auto-profile rescan check and the model-evaluator check. The per-turn
  line reads quota from `usage.json`; that fetch only refreshes it, and the other three feed
  nothing on the line.
- **Fix.** One detached child (`--background-stop-work`, fork + execv through
  `statusline_tick._spawn_detached`, no new subprocess site) runs the fetch and the three jobs.
  The sync path reads `usage.json`; a reading younger than 120 s counts as live (it still
  becomes the next baseline). A note the child would have added to the full box goes to
  `stop_notes.json` and the next Stop shows it once. The child turns the latency recorder off
  (the P09-3 trap). session-end and agent-route now name their phases (`phases_ms`).
  Found, not fixed: `_maybe_evaluate_models` imports `EVAL_CACHE_PATH`, which
  `model_evaluator` no longer defines, so the 7-day model check is a no-op (it was inline too).
- **Test.** `tests/test_p09_session_end_bg.py::test_stop_runs_none_of_the_moved_steps_inline`
  (FAILS on da31df7: the fetch, the profile rebuild, the rescan check and the evaluator all ran
  inline), `test_stop_returns_while_a_5s_child_still_runs`,
  `test_cached_usage_is_live_only_while_fresh`, `test_the_child_writes_no_session_end_latency_row`.
- **Follow-up (review of #325).** The first version spawned one child on every Stop, so a
  burst of N Stops started N concurrent keychain + HTTPS children. The spawn now takes a 15 s
  claim (`stop_background.claim`, as status-bar's refresher): `test_a_burst_of_stops_starts_one_child`,
  `test_the_claim_expires_after_its_window` (both fail on e5148bc). Suite tests that run
  `main()` in-process stub the spawn. Not claimed: a Stop wall-time gain. With the live fetch
  stubbed in both arms the moved steps cost ~10 ms (reviewer's bench, n = 20 per arm); the
  real saving (keychain + HTTPS, 8 s timeout) is not measured, so no number is given.

## 6. Research session b9f04425 counted as organic

- **Symptom.** One session supplied 1,175 of the 1,183 organic turn-first rows in W0
  (99.3%) and 81.4% of the NS units, while p_eval had excluded it as "this research
  session". Without it, W0 holds 8 organic turn-first rows from 2 sessions.
- **Cause.** The session was never tagged research: 424 of its rows were stamped organic,
  751 had no stamp, and its tag file said organic. The ledgers are append-only, so the
  stamps could not be corrected.
- **Fix.** #291 (M0.0b): `~/.llm-router/session_kind_overrides.json`
  (`{session_id: {kind, reason}}`), read by every reader with precedence override, tag file,
  then the row's own stamp. The live file holds one entry, for b9f04425. With it,
  `llm-router kpi --since 2026-09-29T09:41:51Z --until 2026-10-06T09:41:52Z` counts 59 organic
  NS units (25 backfilled); with `--include research` the count is 5,914 (2026-10-07, copy of
  `~/.llm-router`).
- **Test.** `tests/test_session_kind_override.py` (an override beats the tag file, the
  `KindIndex` stamp and the ledger stamp; a session without an override is unchanged).
  Executor sessions: `exec_sessions_check.py` must print 0 organic.

## 7. `edit_outcomes.jsonl` rows with no source

- **Symptom.** A local edit made by `llm_edit` (inside a Claude turn) and one made by the
  zero-Claude hook (the whole turn served locally) look the same in `edit_outcomes.jsonl`,
  so O3 could not count the second as a local turn without also counting the first.
- **Cause.** `edit_ledger.record_edit_outcome` writes ts, session_id (from the global
  pointer), session_kind, file, model, applied and survived. It has no `source` and no
  `turn_id`, and the session id is a guess from the pointer file.
- **Fix.** Plan task M0.3(c): `source` in {`zero_claude`, `llm_edit`}, the hook payload's own
  session id, and `turn_id`; O3 counts one local turn per (session_id, turn_id) among applied
  `zero_claude` rows, and never counts `llm_edit` rows as turns.
- **Test.** `tests/test_offload_share_alignment.py` (to be added by M0.3): a turn that
  edits 2 files is 1 turn; an `llm_edit` row is no turn; a `zero_claude` row does not match
  or drop an `llm_edit` unit in `northstar._fold_edit_ledger`. Not on `main` yet.

## P09-2. Proxy tier decision took 1.2-2.2 s on rules-only turn-first calls

- **Symptom.** `tier_decision_s` was 1.208, 2.203 and 1.673 s on 3 of 6 live turn-first
  rows (reasons haiku_rewrite, sticky, thinking_floor), against the PRD's 50 ms heuristic
  bar [PL] (`$PP/v16/sources/PL_proxy_calls_since_20261007T1140Z.jsonl`, n = 15 rows).
- **Cause.** Not the quota read (the plan's first hypothesis: `proxy.quota_pressure` parses
  `usage.json` once per mtime and costs one `stat()` per call). The tier decision's
  classifier was `backends.choose_model`, which builds a provider chain
  (`router._build_and_filter_chain`: usage.db queries, dynamic routing, the Ollama model
  list) on each new (task_type, complexity) key, inside the timed span. The decision reads
  only the class. Measured on a copy of the live usage.db (2026-10-07, this machine under
  load): first build 21.6 s, later new keys 67-107 ms, `classify_signals` 0.1-0.2 ms.
- **Fix.** `backends.tier_classify`: the same `classify_signals(GATEWAY_POLICY)` class, chain
  head only from the cache, never a build. The decision now writes `tier_phases_ms`
  (`classify`, `quota_read`, `stickiness`, `haiku_checks`; `okf_attach` and `fold` beside it).
- **Test.** `tests/test_p09_proxy_decision.py`:
  `test_a_turn_first_decision_never_waits_on_a_chain_build` (a 1 s fake chain build; FAILS
  on da31df7), `test_tier_classify_gives_the_same_class_as_choose_model`,
  `test_a_slow_chain_build_does_not_reach_tier_decision_s`.

## 8. `DISABLE_LLM_CLASSIFIERS` auto-detect turns the hook's Ollama layer off

- **Symptom.** With `LLM_ROUTER_CLASSIFY_LOCAL_ONLY` and `LLM_ROUTER_DISABLE_LLM_CLASSIFIERS`
  both unset, the hook's LLM classifier layer is off whenever Ollama is reachable, and on —
  sending the prompt to a cloud API (layer 3) — whenever Ollama is unreachable and a Gemini or
  OpenAI key is set.
- **Cause.** `hooks/auto-route.py` (:387 at da31df7):
  `DISABLE_LLM_CLASSIFIERS = _ollama_reachable or not _has_api_key`. The comment above it says
  local-only means "heuristic + Ollama", but this flag also gates layer 2 (Ollama), so the
  Ollama layer is off exactly when Ollama is reachable. It also cost a 0.5 s `/api/tags` probe
  at every hook start.
- **Fix.** P0.7-c (plan v16, `fix/learning-bugs`, hook version 47). The layer is controlled
  only by `LLM_ROUTER_HOOK_LLM_LAYER` (registered in `env_registry.py`), **default off**.
  `LLM_ROUTER_DISABLE_LLM_CLASSIFIERS` is no longer read; with the layer on, layer 3 (cloud API)
  additionally needs `LLM_ROUTER_CLASSIFY_LOCAL_ONLY=false`. This deliberately departs from the
  gap analysis, which asked to switch the layer on when Ollama is present. Reasons: the hook
  latency NFR, and the round-2 kill of the v7 LLM classifier — Cλ2 10.70 vs rules 9.58,
  under-route 79/91 vs 47/91 (n = 91, `$PP/eval/results/tune_round2_20261007T151100.json`). The
  flag stays off until a D-19 candidate passes its own pre-registration.
- **Test.** `tests/test_p07_learning_bugs.py`: `test_p07c_default_off_with_ollama_reachable`,
  `test_p07c_default_off_without_ollama_and_with_an_api_key` (red on da31df7: Ollama layer
  called), `test_p07c_flag_on_calls_the_ollama_layer` (red on da31df7: not called),
  `test_p07c_old_variables_no_longer_turn_the_layer_on` (red on da31df7),
  `test_p07c_flag_on_keeps_the_cloud_api_layer_opt_in`, `test_p07c_flag_is_registered`.

## 9. Classifier warm-up loaded `llmr-classifier` at the wrong `num_ctx`

- **Symptom.** The first real classification after a warm-up still cost a model reload. In
  `~/.ollama/logs/server.log` (Ollama 0.32.11, `OLLAMA_CONTEXT_LENGTH=131072`), the alias's
  blob `sha256-dec52a44...` started at `-c 32768` at 2026-10-07 09:52:06 (+01:00), then
  restarted at `-c 4096` at 09:52:32 (26 s later, the real call evicting the warm runner); the
  same pair repeated at 09:55:27 and 09:56:03 (n=2 pairs). A reload is about 5 s against a
  2.0 s budget (D-4), so the call that should have been fast timed out.
- **Cause.** `_warm` posted `/api/chat` with no `options`, and Ollama loaded the alias at its
  own default context. The real call (`_payload`) always sends `num_ctx 4096`. Ollama treats a
  different `num_ctx` as a different runner, so the warm runner was evicted, not reused.
  `_is_loaded` also reported any resident `llmr-classifier` as warm, whatever its
  `context_length`, so an alias left at 32768 skipped the warm-up and the first real call paid
  the reload inside its budget.
- **Fix.** `src/llm_router/local_classifier.py`: `_warm` and `_payload` both take
  `_options()`, so the two requests cannot drift apart. `_is_loaded` returns False for a
  model resident at a `context_length` other than `NUM_CTX`, so the call reports `cold` and the
  warm-up reloads it at 4096. A server that does not report `context_length` is taken at its
  word. Not re-run against the live server (the shared Ollama was left undisturbed); the
  loopback tests are the proof.
- **Test.** In `tests/test_local_classifier.py`:
  `test_cold_model_is_reported_and_warmed_once_per_30_seconds` asserts
  `warm[0]["options"]["num_ctx"] == lc.NUM_CTX == 4096`;
  `test_resident_at_the_wrong_context_is_cold_not_a_reload_inside_the_budget` asserts that an
  alias resident at 32768 returns `cold` with no chat call and one warm-up at 4096, and that
  4096 and an omitted `context_length` return `llm`. Mutants run on head 2316f3f: warm-up
  without `options` fails both tests; `_is_loaded` returning True regardless of context fails
  the second.

## 10. README-advertised hosts failed install; a detected host was skipped silently

- **Symptom.** On da31df7 the README "Works With" table listed `llm-router install --host pi`
  and `--host kimi`. Both printed `Unknown host(s): ...` and exited 0. A plain
  `llm-router install` detected gemini-cli and then said nothing about it.
- **Cause.** `_install_host` only knew `_HOST_SNIPPETS`, which never had a `pi` or `kimi`
  entry. The auto-detect block in `_run_install` checked `codex` by name and ignored every
  other detected host.
- **Fix.** v16 P0.4 (`fix/install-honesty`). README rows for Pi and Kimi say "planned (v16
  P2.12)". `--host pi|kimi` prints `unsupported: <reason>; planned in v16 P2.12` and exits 2.
  `_run_install` wires every detected host that has an installer (codex, gemini-cli) and
  prints `<host>: detected, not wired: <reason>` for each other detected host. `host_detect`
  now detects pi and kimi so they can be reported. Wiring Pi is P2.12.
- **Test.** `tests/test_readme_hosts.py`: parses all 15 README rows; runs the real installer
  for the 12 non-planned `--host` rows in an isolated HOME; checks exit 2 and the reason line
  for pi and kimi; fake detection {claude-code, codex, gemini-cli, unknown} gives wired,
  wired, wired, reported. 7 of its 9 tests fail on da31df7. Four mutants (drop gemini-cli
  auto-wire; `exit 2` to `return`; drop the report line; pi not marked unsupported) each turn
  a test red.

## 11. Four shadow tests raced the clock and failed `main` on a loaded runner

- **Symptom.** `main` at 2ae21d9 (the #301 merge) was red: run 37656384812, job `test (3.13)`,
  `tests/test_proxy_local_shadow.py::test_different_tool_disagrees - assert (None is False)`;
  `test (3.11)` on the same commit passed. Two more were reported as load-sensitive:
  `test_big_body_claude_response_is_not_delayed_by_shadow` ("shadow added 97 ms", once, on #281's
  CI, green on rerun) and `tests/proxy/test_llm_classifier_shadow.py::test_assemble_never_holds_the_event_loop`
  ("held for 108 ms", limit 50 ms; also 82 ms and 254 ms on runs 37668795388 and 37666629764, and
  5 of 6 local runs at load average 77-127).
- **Cause.** No product defect, and not #301 (it touches `decide_tier` and adds a no-op seam when the
  classifier mode is off; the failing path is `local_shadow.py`, last changed in #294). All three
  tests compared a wall clock with work that a loaded machine stretches.
  1. `test_different_tool_disagrees`: the fake Claude replied after a fixed 0.3 s. Before local reaches
     its backend the job makes four worker-thread hops (deepcopy, media scan, to_ollama, prompt cap).
     When they took longer than 0.3 s the job saw Claude finish first, which is the documented
     behaviour (`dropped_claude_first`), and wrote `agree=None`, `schema_valid=None`. The record was
     correct and carried its reason code; the test read it as if local had answered. Reproduced on
     a quiet machine by making `local_mode.has_media` sleep 0.5 s: 6 tests in the file failed, among
     them this one with `assert (None is ...)`; with the fix the same 8 selected tests pass. The same
     race sat under every test that expects local to win, not only the one that fired.
  2. `test_big_body_...`: compared the wall time of a 3 MB POST with shadow on and off. Wall time
     includes every moment the OS deschedules the process.
  3. `test_assemble_never_holds_the_event_loop`: a 5 ms ticker and asyncio's slow-callback log, both
     wall clock. With the loop CPU time sampled beside the wall gap, one failing run showed a 129 ms
     wall gap against 33 ms of loop CPU, and the slow callback was the test's own `await _post(...)`.
- **Fix.** Test-only. (1) `Upstream` takes the shadow runner and holds Claude's reply until the job's
  `busy` flag drops, which happens after the job has chosen between "local answered" and "Claude
  answered first"; `_step` does this whenever the fake backend has no gate (the dropped/budget tests
  keep their gates and their real Claude-first order). (2) The guard measures `time.thread_time()` of
  the loop thread: the only way shadow can delay Claude's first byte is work on that thread. (3) The
  fake `assemble` blocks on a `threading.Event` that the test sets only after a continuation posted
  during the block has come back; it asserts the worker thread is not the loop thread and that the
  loop served the request while `assemble` was in flight. The wall-clock ticker is gone; the call-path
  cost stays pinned by `test_a_long_history_is_assembled_off_the_request_path` and the G1_proxy p95
  test, which read `tier_decision_s`.
- **Test.** The three tests above, each 30 times in a row at load average ~80 (results in the PR).
  Mutant for (3): `assemble(snapshot)` inline instead of `asyncio.to_thread(assemble, snapshot)` in
  `llm_shadow._run` fails the new test (timeout; the loop cannot serve while it blocks).
  Rule: a test that depends on a reply arriving "before" another must wait on that event, never on a
  sleep length; a "loop not held" check must read the thread or the blocked work, not a wall gap.
- **Fourth, found by the full-suite run on this change.**
  `test_decision_p95_stays_under_30ms_with_a_2s_classifier` made the classifier slow with
  `sleep(2.0)`. On a loaded machine the 200 sequential POSTs took more than 2 s, the first call
  finished mid-loop, its slot was reused and the counts came out `drops == 195` instead of 196
  (failed in isolation at load average ~70). The fake is now gated: it never answers until the test
  ends. The p95 <= 30 ms bar is the product's own number and is left as it was.

## 12. Learned routes keyed by tool name, looked up by task type

- **Symptom.** No user correction ever overrode a route. Three `llm_reroute` corrections of an
  `llm_code` decision wrote `learned_routes.json` with the key `llm_code`; the hook asks for
  `code` and found nothing.
- **Cause.** `src/llm_router/memory/profiles.py` `build_learned_profile` (:102 at da31df7) keyed
  the profile by `corrections.original_tool` (a tool name). `hooks/auto-route.py`
  `_check_learned_override` (:3953) looks it up by the classified task type.
- **Fix.** P0.7-a (`fix/learning-bugs`). The profile is keyed by task type through
  `TOOL_TO_TASK_TYPE` (`llm_code→code`, `llm_query→query`, `llm_research→research`,
  `llm_generate→generate`, `llm_analyze→analyze`; unknown names kept). For one release both
  readers (`load_learned_profile` and the hook) accept an old tool-keyed file; a task-type key
  wins over a legacy key for the same task.
- **Test.** `tests/test_p07_learning_bugs.py::test_p07a_session_end_profile_feeds_the_hook_override`
  (session-end `_build_and_save_learned_profile` output feeds `_check_learned_override('code', …)`
  and the override fires; red on da31df7), `test_p07a_every_tool_key_maps_to_its_task_type`,
  `test_p07a_reader_accepts_a_legacy_tool_keyed_file`, `test_p07a_task_type_key_wins_over_a_legacy_key`.

## 13. Retrospective accuracy 100% at 0 corrections

- **Symptom.** Every retrospective of a session in which nobody corrected a route printed
  "Accuracy: 100%".
- **Cause.** `src/llm_router/retrospective.py` `analyze_facts` (:234 at da31df7):
  `accuracy = 1.0 - corrections / decisions`. With 0 corrections that is 1.0, a perfect score
  measured from nothing: an uncorrected route is not a verified one.
- **Fix.** P0.7-b (`fix/learning-bugs`). With 0 corrections `classification_accuracy` is
  `None`, rendered "not measurable (no corrections)" by `_format_accuracy_pct`. With at least
  one correction the ratio is unchanged.
- **Test.** `tests/test_p07_learning_bugs.py::test_p07b_zero_corrections_is_not_measurable` and
  `test_p07b_retrospective_file_says_not_measurable` (red on da31df7: 1.0 / no such text),
  `test_p07b_with_corrections_is_still_a_number`.

## 14. Gateway doors: case-sensitive "auto", `stream` dropped, three parameters dropped

- **Symptom.** (a) A gateway request with `model` "Auto", "AUTO" or "llm-router-auto" got
  HTTP 400 `Invalid model_override format: 'Auto'` instead of being routed. (b) A request
  with `stream: true` to `/v1/chat/completions`, `/v1/responses` or `/v1/messages` got one
  JSON body; the OpenAI and Anthropic SDKs expect SSE and fail inside their stream parser.
  (c) A client's `max_tokens` and `temperature` never reached the model, and its system
  prompt arrived as a `system:` line inside the user text. Reproduced at da31df7 by
  `tests/test_p06_gateway_door_bugs.py`: 48 of its 59 tests fail there, 0 on the fix.
- **Cause.** (a) `gateway._AUTO_SENTINELS` matched case-insensitively and passed the name
  through, but `route_server.route_payload_async` compared it to `("auto",
  "llm_router-auto")` exactly, so the rest reached `route_and_call` as a literal
  `model_override`, which rejects a name without `/`. (b) `stream` was not declared on the
  request models, so Pydantic dropped it. (c) `gateway._route` built the payload from
  prompt, task type, complexity, model and project only.
- **Fix.** `route_server.is_auto_model` (case-insensitive, stripped) is the one sentinel
  test; the gateway imports it. `stream: bool = False` is declared on the three SSE request
  models and `true` returns 400 `"streaming not supported yet (v16 A.2)"` before routing.
  Ollama's `/api/chat` and `/api/generate` are not refused: their single `done: true`
  object is a valid one-chunk NDJSON stream, and Ollama clients stream by default.
  `_route` forwards `system`, `max_tokens`, `temperature` from every door (OpenAI
  `system`/`developer` messages and `max_completion_tokens`; Responses `instructions` and
  `max_output_tokens`; Anthropic `system`; Ollama `options.num_predict`/`temperature`).
  Because the semantic cache keys on prompt and task type only, a call that carries a
  caller system prompt now bypasses the cache (`semantic_cache.CALLER_SYSTEM_PROMPT`), so
  callers with different system prompts cannot share an answer; P0.5 owns putting it in the
  key.
- **Test.** `tests/test_p06_gateway_door_bugs.py`: `test_sentinel_is_routed_on_every_door`,
  `test_stream_true_is_an_explicit_400`, `test_every_post_route_is_classified_for_stream`,
  `test_three_parameters_reach_route_payload`,
  `test_semantic_cache_is_bypassed_while_a_caller_system_prompt_is_set`. Mutants: a
  case-sensitive `is_auto_model` fails 17; no stream refusal fails 3; temperature dropped
  from the payload fails 8; no cache bypass fails 1.

## 15. MCP routing ran on Claude pressure 0.0 for the life of the process

- **Symptom.** With `usage.json` at 0.80 (session 53%, weekly 80%; rsync copy taken
  2026-10-07T17:53Z, n = 1 file), `claude_usage.get_claude_pressure()` on da31df7 returned
  0.0. The MCP chain never demoted Claude and never fronted Codex, however tight the quota.
- **Cause.** `get_claude_pressure()` returned an in-process cache that only
  `set_claude_pressure` filled, and only the `llm_update_usage` tool calls that. An MCP
  server that never saw that call kept the initial 0.0. The hooks kept `usage.json` fresh the
  whole time; the MCP side never read it.
- **Fix.** `claude_usage.get_claude_pressure_reading()` returns `(value, state, as_of)`. A
  push from the last 300 s wins; otherwise the value comes from `usage.json` through
  `budget._pressure_from_usage`, cached 60 s. `updated_at` older than 30 min is `stale`, a
  missing or unreadable file is `unknown`, and neither carries a value.
  `get_claude_pressure()` returns the value or 0.0, so stale and unknown keep the old
  default and do not reorder the chain.
- **Test.** `tests/test_mcp_claude_pressure.py`:
  `test_get_claude_pressure_is_no_longer_zero_for_life` (0.0 on da31df7, 0.96 on head),
  `test_stale_file_is_unknown_and_reads_as_zero`,
  `test_high_pressure_puts_codex_before_claude` (`_build_and_filter_chain`, code/moderate,
  `usage.json` at 0.96: Claude led on da31df7, Codex leads on head) and
  `test_stale_pressure_leaves_the_order_unchanged`.

## 16. Critical-pressure override sent `/model claude-opus-4-6`, a retired id

- **Symptom.** At critical pressure, auto-route.py told Claude Code to switch to
  `/model claude-opus-4-6`. The proxy's opus tier (`proxy/claude_tiers.yaml`) is
  `claude-opus-5-5`. subagent-start.py and the hook chain builder named the same old id.
- **Cause.** A literal model id in three hooks, which nothing tied to the tier policy.
- **Fix.** `proxy.tiers.tier_model("opus")` reads the policy the proxy loads (the
  `LLM_ROUTER_PROXY_TIER_POLICY` file, else the bundled YAML) through
  `ClaudeTierPolicy.load`. The three hooks use it and fall back to Claude Code's `opus` alias,
  never to a literal. `pricing.retired_models()` lists ids kept only to price old rows;
  `claude-opus-4-6` is one of them for routing.
- **Test.** `tests/test_no_retired_model_ids.py::test_routing_code_names_no_retired_model_id`
  scans the string literals in `src/llm_router/hooks/*.py` and `router.py` and prints how many
  it checked. On da31df7 it found 3 hits (auto-route.py:4799, chain_builder.py:188,
  subagent-start.py:203); on head it finds 0. To re-prove the baseline from head, run the
  same test with `RETIRED_IDS_SCAN_ROOT=<da31df7 checkout>/src/llm_router`: it fails with 3 hits.

## 17. Semantic cache never hit, ignored context, and reported a hit rate of 0

- **Symptom.** (a) A routed request repeated within 24 h was never served from the semantic
  cache. (b) Had the key matched, "yes, do it" answered in one conversation would have been
  served verbatim in another: the key had no context. (c) With no Ollama the cache did nothing.
  (d) `cost.get_cache_hit_stats` and the session-end hook's `_query_cache_hit_stats` always
  returned zeros / `{}`, so the hit rate (R-CTX-7) could not be measured.
- **Cause.** (a) `route_and_call` called `semantic_cache.check` with the user's raw prompt, but
  `_finalize_successful_route` called `store` with the prompt after OKF / `<repo_state>`
  injection. Different text means a different embedding and, because `<repo_state>` carries
  numbers, a different C-03 discriminator. (b) No column bound an entry to its conversation.
  (c) `check`/`store` returned early when `ollama_base_url` was unset. (d) Both queries named
  columns `semantic_cache` never had (`was_hit`, `accessed_at`; `cache_hit`, `tokens_saved`,
  `timestamp`); the exceptions were swallowed by fail-open paths.
- **Fix.** v16 P0.5 (`fix/semantic-cache-key`): `route_and_call` builds one
  `semantic_cache.CacheKey` before dispatch (raw prompt + `ctx_hash` = sha256 of caller
  `context`, else the last two buffered messages, plus the caller's `system_prompt` if given and
  the caller's project scope) and passes it
  to `check` and, through the dispatch loop, to `store`. Exact-match pass on
  sha256(normalised text) + `ctx_hash` needs no Ollama (rows stored with embedding `''`, since
  the existing column is `NOT NULL`). Additive migration: `ctx_hash`, `text_hash`, `hit_count`,
  `last_hit_at`, and a `semantic_cache_lookups` table (one row per lookup, no prompt text). Both
  stats queries read that table and return `{hits, lookups, n}`.
  Lookup rows older than `LLM_ROUTER_PERSIST_TTL_DAYS` are purged on every store, so the
  stats period "all" covers at most that window.
- **Test.** `tests/test_p05_semantic_cache_key.py` (8 tests):
  `test_same_request_hits_after_context_injection`, `test_context_is_part_of_the_key`,
  `test_key_uses_last_two_conversation_messages_when_no_caller_context`,
  `test_caller_system_prompt_is_part_of_the_key`,
  `test_old_lookups_are_purged_even_when_no_cache_row_expired`,
  `test_exact_hash_fallback_without_ollama`, `test_cost_cache_hit_stats_returns_true_counts_with_n`,
  `test_session_end_cache_hit_stats_returns_true_counts_with_n`. All 8 fail on da31df7, but
  only three fail for the bug itself: the second identical request reached a provider; "yes,
  do it" was served across contexts; the same system prompt never hit. The other five fail
  because the API they call (`make_key`, `_semantic_cache_key`, the lookups table) did not
  exist, so their evidence is the single-flip mutants recorded in the v16 P0.5 gate file,
  each of which turns at least one of these tests red.

## 18. Session context store deleted after every turn

- **Symptom.** The Session Context Accumulator's per-session JSONL
  (`session_context_*.jsonl`) was gone after the first turn of every session, so routed
  models got no durable context from turn 2 on (PLAN-v16 Appendix A, P0.1-a, "deleted every
  turn" on da31df7).
- **Cause.** `session-end.py` is registered on **Stop**, which Claude Code fires at the end of
  every turn, not once per session. Its `main()` called `session_store.archive_session()`
  unconditionally, so each turn deleted the store.
- **Fix.** `main()` archives only when the payload's `hook_event_name` is `SessionEnd`, then
  returns without rendering the summary a second time. The installer registers the same script
  on SessionEnd (`_HOOK_DEFS`), keeping the Stop registration for the per-turn summary; the
  plugin bundles carry the new event. `cleanup_old_sessions` still prunes by age. Existing
  installs need `llm-router install --no-hosts` (there is no `--hooks-only` flag) to add the
  SessionEnd entry to `~/.claude/settings.json`; until then the store is pruned by age only, never deleted per turn.
- **Test.** `tests/test_session_end_context_archive.py`: `test_stop_never_archives`,
  `test_session_end_archives_with_resolved_session_id`,
  `test_session_file_survives_stop_with_its_events` (real store, 5 turns, line count
  non-decreasing, deleted only on SessionEnd), `test_installer_registers_session_end_on_both_events`.

## 19. Session context truncation dropped the newest events

- **Symptom.** When a session's context exceeded `max_tokens`, the block injected into a routed
  call held the oldest events and lost the newest, the ones the current question is about.
- **Cause.** `session_store.build_session_context` orders records oldest to newest and then
  called `token_budget.truncate_to_budget`, which keeps the head.
- **Fix.** `truncate_to_budget(..., keep="tail")` keeps the end behind a
  `[…older context truncated…]` marker and still fits the budget; `build_session_context` uses
  it. The default stays `keep="head"` for every other caller.
- **Test.** `tests/test_p01_context_loss.py::test_newest_event_present_in_200_of_200_over_budget_cases`
  (Hypothesis, 200 generated over-budget sessions, the count is asserted and printed).

## 20. `build_context_messages` cut the caller's live context first

- **Symptom.** With an over-budget history, the `[Additional context]` block the caller passed
  (layer 3, the live request's context) was cut or missing from the injected system message.
- **Cause.** `context.build_context_messages` appended layer 3 last and then applied
  `combined[:max_chars]`, so the hard cut always hit layer 3 first.
- **Fix.** Layer 3 is held apart and never optimized, compacted or cut. Layers 1, 2a and 2b get
  the budget left after it; if they still do not fit, whole layers are dropped lowest priority
  first (2b, then 1) and the lowest remaining one is cut keeping its newest text.
- **Test.** `tests/test_p01_context_loss.py`: four `test_layer3_intact_when_*` cases at 10x the
  budget (summaries, session buffer, durable log, layer 3 itself) and
  `test_lowest_layer_dropped_before_higher_ones`.

## 21. `context_prep` truncated the user prompt

- **Symptom.** `prepare_prompt` returned a `PreparedPrompt.user_prompt` cut to the budget's
  user allocation with a `[truncated]` marker.
- **Cause.** `context_prep.py` passed the user prompt through `truncate_to_budget`.
- **Fix.** The prompt is never truncated. Over its allocation, `calculate_budget` already gives
  system and context less room; when the prompt alone exceeds the model window minus the output
  reserve, `prepare_prompt` raises `local_context_guard.ContextOverflow`. A system prompt
  (the auto one is outside the budget's system allocation) that does not fit next to the
  prompt in that window is dropped. Live impact was limited: `router.py` uses only
  `full_system` from `prepare_prompt` and sends the raw prompt. It catches the exception with
  `except Exception`, logs it at debug level and continues without the system prompt and
  enrichment; it does not escalate. Escalation comes only from the provider preflight
  (`providers.call_llm`, `ollama/` models) and chain failover.
- **Test.** `tests/test_p01_context_loss.py::test_200k_prompt_is_intact_when_it_fits_the_window`,
  `::test_200k_prompt_raises_context_overflow_when_over_the_window`,
  `::test_user_prompt_is_never_shortened` (12 cases, outcome pinned per case: 3 raise, 9
  intact), `::test_prompt_plus_auto_system_prompt_fits_the_window` (4 cases);
  `tests/test_context_prep.py::test_long_user_prompt_never_truncated_for_small_model`
  replaces the test that pinned the bug.

## 22. DIRECT rows wrote 0.0 / False / "balanced" for values nobody measured

- **Symptom.** Every DIRECT row in `routing_decisions` had `classifier_confidence = 0.0`,
  `classifier_latency_ms = 0.0`, `budget_pct_used = 0.0` and `quality_mode = 'balanced'`:
  63 of 63 `reason_code = 'direct'` rows in an rsync copy of `~/.llm-router/usage.db` taken
  2026-10-07 (the plan's 7-day figure was 36/36 [UDB7]). A task type the hook could not map
  was logged as `query`.
- **Cause.** `savings_logger.log_direct_to_db` passed literals for fields the DIRECT path
  never computes, and coerced an unknown task type to `TaskType.QUERY`. `usage.task_type` was
  declared `NOT NULL`, so it could not hold "unknown" either.
- **Fix.** Those five fields are passed as None and stored as NULL (`log_routing_decision`
  now keeps a None `was_downshifted` as NULL). An unknown task type is NULL in both tables,
  with the received label in the new `task_type_raw` column. `cost._relax_usage_task_type_notnull`
  rebuilds `usage` once, inside `BEGIN IMMEDIATE`, keeping every row, id, index and the
  AUTOINCREMENT sequence. On the copy: 1,388 rows kept, sequence 1,428 kept, both indexes
  recreated. Smoke on the same copy (40 DIRECT rows: 30 through `sdk.route` with a fake model
  chain, 10 through agent-route `_log_cli_savings`): 0 rows at 0.0, 40 NULL.
- **Test.** `tests/test_p08_honest_records.py`: `test_direct_row_records_unmeasured_fields_as_null`,
  `test_direct_unknown_task_type_is_null_with_raw_value`,
  `test_legacy_usage_table_accepts_null_task_type_after_migration`. Each fails on da31df7.

## 23. `usage` rows carried no session id

- **Symptom.** No `usage` row could be scoped to a session: the table had no `session_id`
  column (copy of the live `usage.db`, 2026-10-07, 1,388 rows).
- **Cause.** `cost.log_usage` and the second writer, `hooks/cc-usage-track.py` (275 of 669
  `usage` rows since 2026-09-30 on the copy, provider `cc`), never recorded one.
- **Fix.** Additive migration `usage.session_id TEXT`. `log_usage` takes `session_id`; when
  omitted it stamps `call_identity.call_session_id()` (the MCP call's own session, None
  outside a tool call). The DIRECT path passes the hook payload's id. `cc-usage-track.py`
  (hook version 2) adds the column if it is the first writer and stores the PostToolUse
  payload's `session_id`. All writers store ids only; placeholders such as `sdk` are NULL.
- **Test.** `test_usage_session_id_migration_is_idempotent`,
  `test_direct_row_carries_the_payload_session_on_usage`, `test_log_usage_stamps_the_mcp_call_session`,
  `test_cc_usage_track_writes_the_payload_session`, `test_cc_usage_track_session_rule_matches_call_identity`.
  The live bar (session id on at least 99% of at least 100 post-deploy rows) is checked after deploy.

## 24. The Stop line's north star used the heuristic "used", kpi the strict rule

- **Symptom.** For one session the session-end/Stop line and `llm-router kpi` could show two
  different north stars.
- **Cause.** `northstar.current_session_line` divided heuristic `outcome == used` units by all
  units; kpi's NS counts `is_strict_used` (PLAN M0.2, bug 3).
- **Fix.** `report()` adds `strict_used` and `strict_share` per session, counted with
  `is_strict_used` on attempted units, as kpi does. The line now prints
  `north star (strict) N% (n=...)`. The heuristic stays a kpi diagnostic.
- **Test.** `test_stop_line_north_star_uses_the_strict_rule` (50 heuristic-used units, 10 strict:
  prints 20%, was 100%).

## 25. Status bar priced its baseline at Opus and labelled it "vs Sonnet"

- **Symptom.** The full status line read `(vs Sonnet:$58)` for a baseline priced at Opus rates.
- **Cause.** WP-03 moved the price to `pricing.price_for("opus")` and left the label.
- **Fix.** `HOST_BASELINE_TIER = "opus"` prices the baseline and `HOST_BASELINE_LABEL` names it,
  so the two cannot drift (status-bar hook version 6).
- **Test.** `test_status_bar_baseline_label_names_the_priced_model`.

## 26. `llm-router replay` raises TypeError on a row with NULL confidence

- **Symptom.** `commands/replay.format_decision_line` computed
  `decision.get("classifier_confidence", 0) * 100`; on a row whose confidence is NULL it raised
  `TypeError`, and a NULL task type printed as `None`. The copy of `usage.db` (2026-10-07) already
  had 421 such successful rows out of 2,225 (router "unhinted" rows); P0.8 adds every DIRECT row
  to that set.
- **Cause.** `dict.get(key, default)` returns the stored None, not the default, for a NULL column.
- **Fix.** A NULL confidence prints `Confidence: unknown`; a NULL task type, complexity or model
  prints `unknown`. Measured values print as before.
- **Test.** `test_replay_renders_null_confidence_and_task_type_as_unknown` (fails on da31df7 with
  the TypeError; mutant that restores `.get("task_type", "unknown")` fails it too).

## 27. The quality report and the Stop summary raise TypeError on a NULL task type

- **Symptom.** The quality report tool (`tools/admin.py`) raised `TypeError: unsupported format string passed to
  NoneType.__format__` when any `routing_decisions` row in its window had a NULL task type
  (reproduced by the #310 reviewer on a copy of `usage.db`: 5 such DIRECT rows broke the
  7-day report; deleting them fixed it). The session-end routing panels
  (`_format_routing_section`, `_format_cc_model_section`) raised the same error on a NULL
  `usage.task_type`.
- **Cause.** P0.8 stores an unknown DIRECT task type as NULL (bug 22). These readers formatted
  the value with `{task:<16}` / `{tool:<12}`, and `dict.get(key, default)` returns the stored
  None (same class as bug 26). Before P0.8 no writer stored a NULL task type.
- **Fix.** `tools/admin.py` renders NULL as `unknown` in the task-type table and the policy
  event list. `session-end.py` `_aggregate` and the CC model panel use `get(...) or "unknown"`
  (session-end hook version 19; 21 after main's #307 and #315 took 19 and 20). Other `GROUP BY task_type` readers checked: community.py
  filters `task_type IS NOT NULL`; dashboard/tui.py, dashboard/server.py (JS `|| '?'`),
  session-end `_query_savings_by_task_type` and the `commands/northstar` dry run already map
  None. That sweep missed two readers; see bug 29.
- **Rule.** A column that P0.8 may leave NULL is rendered with `value or "unknown"`, never
  `get(key, "unknown")` and never a bare format spec.
- **Test.** `test_quality_report_renders_null_task_type_as_unknown`,
  `test_session_end_routing_panels_render_null_task_type` (both fail on 25a2ece).

## 28. `llm-router northstar` showed the heuristic share as the North Star

- **Symptom.** The CLI printed "North Star — routed-and-used share" per session and its
  aggregate median/p25/max from the heuristic `outcome == used`, after bug 24 moved the Stop
  line to the strict rule. Plan §1.2: the heuristic NS leaves user surfaces (P0.8-b).
- **Cause.** Bug 24's fix changed `current_session_line` only.
- **Fix.** `report()["aggregate"]` adds `strict_median`, `strict_p25`, `strict_max` over the
  same sessions (n >= MIN_UNITS). The CLI shows "verified offload" from the strict rule and
  prints the heuristic only on lines labelled "diagnostic". The JSON keys `median`/`p25`/`max`
  keep their meaning.
- **Test.** `test_northstar_cli_shows_the_strict_rule_as_the_north_star` (50 heuristic-used
  units, 10 strict: shows 20%, was 100%), `test_report_schema_is_pinned`.

## 29. The claw-code Stop hook and the dashboard models panel raise TypeError on a NULL task type

- **Symptom.** `hooks/session-end-clawcode.py` (installed as a Stop hook, `install_hooks.py`)
  exited 1 with `TypeError: unsupported format string passed to NoneType.__format__` when the
  session had a paid `usage` row with a NULL task type. The #310 reviewer reproduced it on an
  rsync copy of `~/.llm-router` with 3 seeded paid DIRECT rows of unknown task type (3 of 3
  NULL): exit 1 and a traceback. `dashboard_enhanced.query_last_prompt_calls` returned the
  NULL as None, and `cyber_grid._build_models_panel` formats it with `{c['task_type']:<10}`.
- **Cause.** Bug 27's reader sweep checked `GROUP BY task_type` readers and the main
  `session-end.py`, but not its claw-code sibling, which still read `r.get("task_type",
  "unknown")` and formatted it with `{tool:<12}`, nor row-level readers of `usage`.
- **Fix.** Both use `value or "unknown"` (claw-code hook version 3). A second sweep
  (`get("task_type", ...)`, `["task_type"]` and every `SELECT ... task_type` under `src/` and
  `hooks/`) found no other reader of `usage` or `routing_decisions` that raises on a NULL;
  `commands/verify.py` prints one as `None` (cosmetic, not changed here).
- **Rule.** A reader sweep greps for the column name across every hook copy and variant
  (`*-clawcode.py`, `hooks/` and `src/llm_router/hooks/`), not only for `GROUP BY`.
- **Test.** `test_clawcode_stop_hook_renders_null_task_type` (runs `main()` on a seeded
  `usage.db`), `test_dashboard_last_prompt_calls_render_null_task_type`. Both fail on 15a1398e.

## P013-1. `llm_act` wrote files into the MCP process cwd

- **Symptom.** A local model's `write_file` from `llm_act` landed in the directory the MCP
  server was started in (`$HOME` in the field, see `mcp_roots.py`), not in the caller's
  project. On da31df7, `tests/test_llm_act_confinement.py` shows it: with the MCP cwd set to a
  scratch directory and `CLAUDE_PROJECT_DIR` set to the project, `marker.txt` was written to
  the MCP cwd; of 10 write targets outside the project, 3 were written (two into the MCP cwd,
  one through a symlink-shaped path) and 7 refused; with no root at all, writes still went
  through.
- **Cause.** `tools/agentic._default_adapters` built `ReActAgent(tier=0)` with no `cwd`, so
  `agentic/react.default_tool_executor` fell back to `Path.cwd()` with writes enabled. The
  Codex tier (`CodexAdapter(tier=1)`) got no `-C` and ran `workspace-write` in the same
  directory.
- **Fix.** `tools/agentic.resolve_project_root` picks the root from the MCP client's roots
  (`mcp_roots.root_from_ctx`, with its per-session hook-recorded cwd fallback), then
  `$CLAUDE_PROJECT_DIR`; `llm_act` and `llm_delegate` take an optional MCP `ctx` for it. Both
  tiers get that root as their cwd. No root means read-only: `default_tool_executor(cwd=None)`
  refuses `write_file` and `bash`, and Codex runs `--sandbox read-only`. Every file path is
  resolved (symlinks followed) and must be `is_relative_to` the root. The result JSON now says
  `project_root` and `read_only`.
- **Remaining gap.** A `bash` command can still redirect output outside the root (`echo x >
  ../out/y.txt`); the regex denylist is not a sandbox. That belongs to P2.9 and is pinned by
  the strict xfail `test_bash_redirect_outside_root_is_refused`.
- **Test.** `tests/test_llm_act_confinement.py`: 6 tests fail on da31df7 and pass on the fix
  (`test_write_lands_in_project_root_not_mcp_cwd`, `test_ten_outside_paths_refused_10_of_10`,
  `test_no_root_is_read_only`, `test_mcp_client_roots_beat_claude_project_dir`,
  `test_codex_tier_is_confined_too`, `test_executor_without_cwd_is_read_only`). Six mutants
  (read-only flag off, containment off, ReAct on the process cwd, Codex without cwd, roots
  ignored, env ignored) each turn at least one of them red.

## P0.14-a. The proxy ledger wrote 0 rows for 25 hours and nothing said so

- **Symptom.** `~/.llm-router/proxy_calls.jsonl` wrote 0 rows from 2026-10-07 12:47 to
  2026-10-08 14:24 while hooks kept recording turns. `llm-router kpi` rendered "not measurable"
  for the proxy KPIs and `doctor` printed the proxy as answering and healthy. The owner found it
  by hand.
- **Cause.** Every session ran from a directory whose `.claude/settings.local.json` set
  `env.ANTHROPIC_BASE_URL` straight to `api.anthropic.com`, overriding the user-level localhost
  proxy default. The proxy was up; no traffic reached it. `doctor` only probed the port, and `kpi`
  had no line that compared the ledger with the turns the hooks saw.
- **Fix.** New `llm_router/proxy_liveness.py`, read-only, used by both commands.
  `kpi` prints `proxy_rows_24h: N (n=N ...)` with the hook turns (`auto-route` /
  `UserPromptSubmit` rows in `hook_latency.jsonl`) and `routing_decisions` rows of the same 24 h,
  and a `WARN` line when N is 0 and either count is above 0; `--json` carries the same fields under
  `proxy_liveness`. A turn count that cannot be read is `null`, never 0, and no recorded turn means
  no WARN. `doctor`, when the user settings make a localhost `ANTHROPIC_BASE_URL` the default,
  lists every `.claude/settings.local.json` / `.claude/settings.json` under the current directory
  (depth 3) whose value differs from it, as path plus host only (no userinfo, path or query), and
  the silent-ledger case; both count as doctor issues. No new env key.
- **Test.** `tests/test_proxy_ledger_liveness.py`, 11 tests, all red on 12038e46 (main) and green
  here: zero rows plus turns warns (text and JSON; also with `routing_decisions` alone); rows
  present, no turns, rows older than 24 h and non-turn hooks give no WARN; an override is detected
  with path and host and without the secret; no override and a non-localhost default report nothing;
  `doctor` prints the override and exits non-zero.

## P0.14-b. P0.14-a printed a pasted key as a "host", and its guards were untested

- **Symptom.** An independent review of #329 planted `SECRETKEY999-bare-key` as the value of
  `ANTHROPIC_BASE_URL` in a project `settings.local.json`; `llm-router doctor` printed it (lowercased)
  as the overriding host. The same review found three guards that no test pinned (an unreadable
  count, future-dated rows, the `sidecar_backfill` exclusion), a `doctor` test whose
  `assert code != 0` passes whatever happens, and a WARN that needed a full 24 h of silence.
- **Cause.** `_host_of` prefixed `//` to any scheme-less string and printed `urlsplit(...).hostname`,
  so any token without a slash was "a host". The guards existed in code but no test failed when they
  were removed (reviewer mutants: 17 of 22 killed, 3 real survivors).
- **Fix.** `_host_of` prints only an IP or an RFC 1123 name with at least one dot (or exactly
  `localhost`), optional port; everything else is `(unparseable)`. `newest_proxy_ts` is clamped to
  now. New `short_silence()`: 0 proxy rows in the last 2 h with at least 3 `auto-route` /
  `UserPromptSubmit` turns in those 2 h; `doctor` reports it (and counts it as an issue) unless the
  24 h WARN already fired. Constants `SHORT_WINDOW_HOURS`, `SHORT_MIN_TURNS`; no new env key.
  An unreadable turn count stays `None` and never warns; 0 turns reports nothing.
- **Test.** `tests/test_proxy_ledger_liveness.py`: bare-key plant through `find_overrides`,
  `doctor_findings` and `_run_doctor`; real hosts still print; 10 non-hostnames are `(unparseable)`;
  unreadable hook / decision count is `None` with no WARN; future-dated proxy and decision rows are
  not counted and `newest_proxy_ts <= now`; `sidecar_backfill` rows are not turns; short silence at
  2 vs 3 turns, with a recent row, with 0 turns, with turns outside 2 h, with an unreadable count,
  not repeated under the 24 h WARN, and as a `_run_doctor` issue. The `doctor` test now asserts
  exactly one `proxy bypass:` issue naming the path and host, not the exit code. 15 mutants, all
  killed (see the PR body).

## 18. Hook DIRECT and SDK served Q&A from local providers (D-14 held only in MCP)

- **Symptom.** D-14 = A says a Q&A task type is never served by a local provider. #297 (M3.0)
  enforced it in MCP `route_and_call` only. `hooks.chain_builder.build_chain`, which builds the
  chain for the hook DIRECT path (auto-route draft, agent-route subagent DIRECT) and for the
  in-process SDK `llm_router.route`, still put Ollama first for every simple and moderate
  Q&A prompt. On da31df7, 16 of the 18 cases (9 `QA_TASK_TYPES` x {simple, moderate}; the 2
  `research` cases already returned `[]`) had a local provider in the chain, and
  `route("what is X", task_type="query")` called Ollama once
  (`tests/test_qa_policy_shared.py`, red run: 21 failed, 5 passed).
- **Cause.** The filter and its provider set lived as private names in `router.py`
  (`_strip_local_for_qa`, `_QA_STRIP_PROVIDERS`). The hook path cannot import `router`
  (cold import ~3.6 s; import time was ~77% of the slow hook tail [M41]), so it had no copy.
- **Fix.** New `src/llm_router/qa_policy.py` holds `QA_TASK_TYPES`, `QA_STRIP_PROVIDERS` and
  `strip_local_for_qa`; it imports only `llm_router.types`, which the hook path already loads.
  `router` and `northstar` import the names back (MCP behaviour unchanged). `build_chain`
  applies the filter with `keep_if_only_local=False`: when only local models are available
  the Q&A chain is empty, so the hook falls through to Claude and the SDK raises
  `RoutingError`. MCP keeps its existing rule (an Ollama-only chain is kept, because an empty
  chain fails the call). `code` and every non-Q&A type are unchanged.
- **Test.** `tests/test_qa_policy_shared.py`: 18 parametrised cases (9 QA types x 2
  complexities, each over all 5 pressure zones) assert no ollama, lm_studio, vllm, llamacpp or
  openai_compat in the chain; `code` keeps local first; the SDK test patches the Ollama call
  with a counter and asserts 0 calls; a subprocess test asserts that importing `qa_policy`
  loads neither `router` nor `northstar`. Mutants (keep-only-local in the hook, no strip in
  `build_chain`, inverted QA check, `openai_compat` dropped, `qa_policy` importing `router`)
  each turn the file red.

## P011-1. Haiku guard re-tripped on audit days older than its window

- **Symptom.** After the owner deleted `~/.llm-router/tier_overrides.json` to turn the Haiku
  rewrite back on, the next guard run wrote the override again. Two low daily audits (7/10
  and 7/10) from weeks earlier still counted as "2 consecutive days below 8/10". Found on
  re-verification of P0.11 at b5f88b5 (unit test, synthetic audits; no live trip happened).
- **Cause.** `run_once` evaluated `audit_daily` on the newest audited date at any age, not on
  the dates inside the guard's 7-day window.
- **Fix.** `audit_daily` in `run_once` looks only at audit days on or after the window start.
  `kpi --haiku-watch` still names its own day. `audit_batch` is unchanged (newest summary at
  any age, as ported from the research guard).
- **Test.** `tests/test_proxy_haiku_guard.py::test_run_once_ignores_daily_audits_older_than_the_window`:
  red on b5f88b5 (`assert 'trip' == 'ok'`), green on 3f4149b; mutant `recent = list(days)`
  turns it red.

## GE6-1. Quota-burn coverage kept owner-overridden sessions in the organic denominator

- **Symptom.** `kpi --quota-burn` coverage on a copy of `~/.llm-router` for
  2026-10-01T10:28Z..2026-10-08T10:28Z reported 7 organic sessions; the owner's override file
  moves 1 of those 7 to research, so the organic population is 6. Repro on a temp home:
  sessions A and B tagged organic, both with start and stop samples, A overridden to
  research: coverage 1/2 (rate 0.5), right answer 1/1. One overridden session a week caps
  the GE6-a point estimate at 6/7 = 85.7%, below the 95% bar, whatever the sampling.
- **Cause.** `quota_samples.quota_burn` (:314 at d287ef4) built the denominator from the raw
  tag kind, `{sid for sid, k in tagged.items() if k in allowed}`, while the per-session loop
  resolved kind through `_kind_for` (override, then tag). The overridden session counted as
  "other kind" in the loop, so it was never covered, but stayed in the denominator.
- **Fix.** The denominator uses `_kind_for(sid, [], tagged)`, the same resolution as the loop.
  Rule: every count in one KPI resolves session kind through one function.
- **Test.** `tests/test_quota_samples.py::test_coverage_denominator_applies_the_owner_override`
  and `test_coverage_denominator_override_with_no_samples` (both red on d287ef4). Mutant:
  restoring the raw-tag denominator fails both.

## GE6-2. Branch hook version equal to main's after main moved on

- **Symptom.** PR #320 changed `session-end.py` and stamped it `llm_router-hook-version: 20`
  (main + 1 when the branch was cut). Main then reached 20 through 2020f374 (#315). Merging
  the branch would have left session-end at 20, the same stamp as main for different code, so
  an installed v20 could not say which of the two it was (plan v16 §5 risk 15: version =
  main + 1).
- **Cause.** The stamp was chosen once, at branch time, and never re-checked when
  origin/main was merged in.
- **Fix.** On the merge of origin/main (7d857641) both copies moved to 21. Rule: every merge
  of origin/main into a branch that changes a hook re-checks each changed hook's stamp
  against main's and sets it to main + 1.
- **Check.** No test can know main's stamp at test time. The check is a command, run after
  each merge of origin/main:
  `for f in session-start session-end; do git show origin/main:src/llm_router/hooks/$f.py | sed -n 2p; sed -n 2p src/llm_router/hooks/$f.py; done`

## CODEX-1. Codex refused to start: `invalid transport in mcp_servers.llm_router`

- **Symptom.** Codex exited at startup with `~/.codex/config.toml:443:14 invalid transport
  in mcp_servers.llm_router` (owner's machine, reported in #323). Codex raises this when a
  `[mcp_servers.<name>]` table has neither `command` nor `url`.
- **Cause.** Codex writes `[mcp_servers.llm_router.tools.<x>]` itself when the user picks
  "always allow" on a tool the installer did not pre-approve. Manifest replay
  (`install_manifest.apply_uninstall`) removed only the tables it had recorded, so Codex's own
  tool table outlived the server table and left an `llm_router` server with no transport.
  A binary-less `install` and the legacy `uninstall_host_integrations` fallback did not look
  for that state either.
- **Fix.** `codex_host.remove_toml_subtree` removes the whole `mcp_servers.llm_router`
  subtree (any quoting or spacing in the header, and dotted keys under `[mcp_servers]` or at
  the root); comment lines right above the next kept table stay. `has_orphan_mcp_tables`
  detects the no-transport state for manifest replay, binary-less install, legacy uninstall
  and `doctor`. Install and uninstall re-check after removal: `✓ Removed orphaned ...` is
  printed only when the server is gone and the file parses; otherwise a `⚠ ... delete them by
  hand` line names what is left (a dotted key whose value spans lines is not cut).
- **Test.** `tests/test_codex_install.py::test_uninstall_takes_tool_tables_codex_wrote_itself`
  and `::test_install_without_binary_clears_orphaned_tool_tables` (both red on main 75700df4:
  the orphan table stays). Also `::test_legacy_uninstall_clears_orphaned_tool_tables`,
  `::test_*_reports_orphans_it_could_not_remove`,
  `tests/commands/test_doctor.py::TestRunDoctorHost::test_run_doctor_host_codex_orphan_tool_tables_say_invalid_transport`
  and the `remove_toml_subtree` tests in `tests/test_codex_host.py`. Each of 10 mutants
  (one per code path, e.g. the legacy cleanup or the doctor branch disabled) turns one of
  them red.

## P09-1. G1 called a 16 s auto-route p95 "within budget"

- **Symptom.** `llm-router kpi` G1 held each hook to `HOOK_BUDGETS_MS`, which held the host
  timeouts (auto-route 60 s, agent-route 320 s) and declared 2-10 s budgets. Live p95s in
  [HL7] (`$PP/v16/sources/HL7_hook_latency_to_20261007T1449Z.jsonl`, 2026-10-04T21:59Z to
  2026-10-07T14:49Z): auto-route 16,040 ms (n = 345), session-start 16,178 ms (n = 65),
  status-bar 4,488 ms (n = 344). Against the PRD (+300 ms sync p95, session-start 2 s,
  statusline 100 ms) all three fail; the scorecard passed auto-route and session-start.
  The statusline was not timed at all.
- **Cause.** The table was written before any hook was timed and mixed two meanings: the
  host's kill timeout (what `timed_out` means) and the latency bar the scorecard judges.
- **Fix.** `HOOK_BUDGETS_MS` = the PRD bars (300 ms per sync hook, 2,000 ms session-start,
  100 ms statusline); the host timeouts moved to `HOOK_TIMEOUTS_MS` and still decide
  `timed_out`. G1 judges `router_added_ms` = elapsed minus the model phases (`draft_chain`,
  `zce_model`, `cold_wait`; a `cold_wait` inside another model phase is subtracted once).
  The statusline records a sampled row (`LLM_ROUTER_STATUSLINE_TIMING=1`, 1 call in 20)
  through `python -m llm_router.hook_latency record-raw`.
- **Test.** `tests/test_p09_hook_budgets.py`: `test_budgets_are_the_prd_bars`,
  `test_kpi_fails_status_bar_at_the_measured_live_p95`,
  `test_kpi_judges_router_added_and_reports_it_for_a_10s_draft`,
  `test_the_row_carries_router_added_with_a_nested_cold_wait_subtracted_once`,
  `test_statusline_timing_all_writes_a_statusline_row`.

## P09-7. Statusline timing rows carried no session id, and needed a python3 that imports llm_router

- **Symptom.** The statusline wrote its row through
  `record-raw statusline Statusline <ms>`, and `record-raw` took exactly 4 arguments, so no
  row could carry `session_id`, although the statusline gets the session JSON on stdin.
  With 0 rows carrying a session id, P0.9-c (`statusline p95 <= 100 ms over >= 200*`) cannot
  count sessions (PLAN v16 §1.4 rule 4) or drop research and executor sessions (rule 8), so
  it could never be judged. Separately, the writer ran `${_chz_py:-python3}`; `_chz_py` is
  set only in the full layout when `usage.db` exists, so in the fast layout, or wherever bare
  `python3` cannot import llm_router, the backgrounded write failed silently. The test hid
  this with a `python3` shim that can import the checkout.
- **Cause.** The raw writer was designed for the elapsed time only, and the interpreter
  search lived inside the money segment.
- **Fix.** `record-raw <hook> <event> <elapsed_ms> [<session_id>]`. The statusline matches
  `"session_id"` in its stdin JSON in bash (no process) and passes it; the timed fast path
  reads stdin itself and pipes it to the tick. The interpreter search is `_chz_find_py`
  (one list, shared with the money segment) and runs inside the backgrounded child, after
  the clock has stopped.
- **Test.** `tests/test_p09_hook_budgets.py`:
  `test_record_raw_puts_the_session_id_on_the_row`,
  `test_record_raw_leaves_an_empty_session_id_off_the_row`,
  `test_statusline_row_carries_the_session_id_from_stdin[full|fast]`,
  `test_statusline_row_is_written_when_bare_python3_cannot_import_llm_router` (all 5 test ids
  fail on 38516fc8 and pass on head).
- **Same gap on auto-route.** auto-route never called `set_session`, so its rows (P0.9-d)
  had no session id either. Fixed in `perf/auto-route-imports` (#326): `main()` calls it
  right after the stdin JSON parses. Test:
  `tests/test_p09_auto_route_imports.py::test_main_names_the_session_on_the_latency_row`
  (fails on 21234081, passes on 45fe0104).

## P09-8. The statusline "wrapper adds < 5 ms" test failed under load

- **Symptom.** `tests/test_p09_hook_budgets.py::test_the_timing_wrapper_adds_under_5ms_per_call`
  failed on CI (#312 at a28e7df5, test 3.11: added unsampled 1.83 ms, sampled 92.15 ms) and
  6 of 6 times in review with 6 copies in parallel (load ~65; sampled 94-123 ms).
- **Cause.** The test compared medians of three separate blocks (40 off, 40 at 1-in-20,
  10 at every call). Under load a perl start-up costs tens of ms and the load drifts between
  blocks, so the medians measured the machine, not the wrapper.
- **Fix (test only).** The arms run interleaved and each is judged on its minimum (load only
  adds time). The structural half is a separate, load-independent test: with a perl that
  logs its starts, an unsampled call starts 0 perls and a sampled call exactly 2. The
  statusline comment now says the t1 read includes one perl start-up (an upward bias
  against the 100 ms bar).
- **Test.** `test_the_timing_wrapper_adds_under_5ms_per_call` and
  `test_the_unsampled_path_starts_no_process_and_a_sampled_call_two_clock_reads`: 12 of 12
  passes with 6 pytest runs in parallel plus 4-12 `yes` CPU burners (load1 38-65). Mutants:
  a perl start on the unsampled path turns the structural test red; a 200 ms sleep in the
  sampled path turns the timing test red.

## P09-9. A session id named by one test leaked onto latency rows of later tests

- **Symptom.** With all five P0.9 branches merged on main 56732137, the CI suite command
  failed `tests/test_kpi_hook_latency.py::test_a_run_that_names_no_phase_writes_the_row_it_always_did`:
  the row had an extra `session_id`. Deterministic in one process:
  `pytest -p no:xdist -p no:randomly tests/test_p09_session_start_bg.py tests/test_kpi_hook_latency.py`.
- **Cause.** `hook_latency.set_session` keeps the id in a module global, which is right for a
  hook process (one invocation, one session). session-start's `main()` names its session
  (#317), and its tests run `main()` in-process, so the id stayed set for every later test in
  that worker. Each branch alone passed; the leak needs #317's caller and this branch's
  `set_session` together.
- **Fix (test only).** `tests/conftest.py::_reset_hook_latency_session` (autouse) clears the
  id before and after each test, without importing the module when no test did.
- **Test.** The two-file command above: 1 failed before, 34 passed after. Removing the
  fixture turns it red again.
