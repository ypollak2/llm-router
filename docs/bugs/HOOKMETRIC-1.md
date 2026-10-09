---
id: HOOKMETRIC-1
status: fixed in `fix/hook-budget-model-phase`
---
## HOOKMETRIC-1. The statusline warned "hooks p95 57.8s agent-route" for a Codex job the router did not add

- **Symptom.** The statusline showed `⚠ hooks p95 57.8s agent-route` against the 300 ms hook budget. Live data: agent-route n=183 over
  2 sessions, p95 57.8 s; 20 of the 21 rows over 1 s carry `phases_ms.codex_delegation`; rows without delegation: p50 115 ms, p95 817 ms.
- **Cause.** agent-route runs the delegated Codex sub-agent synchronously inside `with _hl_phase("codex_delegation")` (up to ~300 s), but
  `hook_latency.MODEL_PHASES` omitted `codex_delegation`, so `router_added_ms` was never computed for those rows and G1 / the statusline
  judged whole-process `elapsed_ms`. The reader fallback `router_added_ms(row)` (rows written without the field) also hard-coded
  `draft_chain` + `zce_model`, so the existing rows would not have been corrected on read either.
- **Fix.** `codex_delegation` joins `MODEL_PHASES` (nesting: `cold_wait` inside it is still subtracted once through `_model_depth`);
  the reader fallback now subtracts every outer model phase in `MODEL_PHASES`. Consumers: `kpi._g1_hook` (and so the statusline
  `_hooks_slow`, which reads its `p95_ms`) already judged `router_added_ms`; `hook_wall.judge_live` judged raw `elapsed_ms` and now
  judges `router_added_ms` (else `elapsed_ms`). Raw wall time stays reported (`p95_elapsed_ms`). `direct_subagent` and `cli_delegation`
  are still not model phases (PLAN v16 S6 defers that to 16.1; the G1 note now names only them). No hook file changed.
- **Test.** `tests/test_p09_hook_budgets.py`: `test_codex_delegation_is_model_time_and_does_not_trigger_the_statusline_warning`,
  `test_a_900ms_row_with_no_model_phase_still_triggers_the_statusline_warning`, `test_hook_wall_live_clause_judges_router_added_too`,
  `test_the_recorder_subtracts_codex_delegation_and_a_nested_cold_wait_once`. Removing `codex_delegation` from `MODEL_PHASES` fails three of them.
- **Not fixed (separate design issue).** A multi-minute Codex job inside a PreToolUse hook sits near the host's hook kill timeout
  (max observed 301 s against the 320 s timeout).
