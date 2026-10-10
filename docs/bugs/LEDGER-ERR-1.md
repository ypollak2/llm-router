---
id: LEDGER-ERR-1
status: fixed in `fix/llm-error-path-ledger-row`
---
## LEDGER-ERR-1. A call that reaches dispatch and fails leaves no per-session ledger row

- **Symptom.** Gate M0-3 rerun 2026-10-10 (two concurrent sessions, 20 `llm(task="code", tier="fast")` calls, T0 08:59:12Z): calls A5, B7, B10
  ended in "Error executing tool llm" after 129 s, 3 s and 244 s and wrote no `routing_decisions` and no `usage` row, only a
  `semantic_cache_lookups` miss. Literal bar 17/20. The Claude MCP log shows the call killed by the tool itself ("failed after 129s"),
  not by the host (successful calls ran 2m14s and longer on the same connection).
- **Cause.** Every ledger writer ran on success. Evidence in `routing_quality.jsonl`: A5 (09:17:58) `failed`, chain nimble:9b (400 "does not support generate"),
  qwen3-coder:30b (litellm Timeout 120 s), codex/gpt-4o-mini (exit 1) -> `RuntimeError: All models failed`; B10 (09:30:14) `failed`, qwen3.6:35b-a3b-coding and
  qwen3-coder:30b both litellm Timeout 120 s (244 s); B7 (09:25:03) `failed` with an empty chain_attempts: every candidate skipped before any
  attempt, so the exhaustion `RuntimeError` came after 3 s. `route_and_call` only released budget in its Cancelled / Timeout / Exception handlers.
- **Fix.** `cost.log_route_error` writes one `usage` row (success=0, 0 tokens, $0, elapsed latency, attempted model, caller's session via `log_usage`,
  reason `error_all_models_failed | error_timeout | error_cancelled | error_budget_exceeded | error_routing_denied | error_exception`).
  `route_and_call` calls it from the three handlers around dispatch, shielded and fail-open; it skips when a row with the call's correlation id exists,
  so there is never a second row for one call. The dispatch loop shares its attempt list so the row names the model actually being tried.
  Pre-dispatch failures (empty chain, budget refusal before dispatch) are not covered.
  Consumers that count usage rows without a `success = 1` filter now exclude error rows through one shared predicate
  (`provider_classes.SQL_NOT_ERROR_ROW`, like the cache-row precedent): statusline mix, routing_report (counts and latency
  percentiles), routing_health, dashboard TUI/server today and month call counts. Share card, digest, session hooks and
  test_delta already filter `success = 1`. Other dashboard queries (per-task/profile breakdowns) still count them.
- **Gate scope (read this).** The M0-3 gate query (`P0.0-m0.json` M0-3 sql) reads `routing_decisions` where `provenance='runtime'` only.
  This change writes a `usage` row, not a `routing_decisions` row, so it does NOT change the literal gate count (15/20 on the rerun).
  It closes the gap under an "any per-session ledger row" measure (17/20 + 3 error rows = 20/20 in that run, by construction, not re-measured).
- **Test.** `tests/test_ledger_err1_error_row.py`: provider exception, no healthy candidate, wall-clock timeout and cancellation each write exactly one row with
  the caller's session id; success writes no error row; retry is two rows; no double row per correlation id; no prompt/exception text; NULL session outside an MCP call.
  `tests/test_ledger_err1_consumers.py` covers the consumer exclusion (4 of 5 fail without it; the fifth checks the helper). With `router.py` reverted to origin/main, 6 of the 9 error-row tests fail (3 pass vacuously: success path, direct no-double-row, no text); with all of `src/` reverted, 7 fail.
