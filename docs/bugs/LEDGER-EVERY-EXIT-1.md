---
id: LEDGER-EVERY-EXIT-1
status: fixed in `fix/llm-ledger-every-exit` (deploy: restart the MCP server; calls made before keep their gaps)
---
## LEDGER-EVERY-EXIT-1. Unledgered `llm()` exit paths were found one M0-3 run at a time

- **Symptom.** Three M0-3 runs each found a new path where an `llm()` MCP call left no session-attributed row in
  `usage` / `routing_decisions`: cache hits (#381), provider errors / timeouts / cancel (#398 LEDGER-ERR-1),
  quality-breaker refusals (#409 N21). Still uncovered when this was written: a failure before dispatch (empty
  chain, pre-dispatch budget or quota refusal, any exception ahead of the dispatch handlers), `llm_act`
  refusals, and any normal return that touched no writer.
- **Cause.** Each fix patched one branch. Writers lived inside the router's dispatch handlers, so every new
  exit that bypassed them was silent by construction.
- **Fix.** One structural guard, `tools/consolidated._ledger_guard`, wraps the whole body of `llm()` and
  `llm_act()`. `cost.log_usage` and `cost.log_routing_decision` report each committed insert to it
  (`call_identity.note_ledger_write`, a ContextVar, no-op outside a guarded call). On exit (return, raised error,
  timeout, cancel) it writes whichever of the two rows is missing through `cost.log_route_error(only=...)`:
  reason `breaker_open` for a refusal, `_route_error_reason(exc)` for an exception (`error_budget_exceeded`,
  `error_timeout`, `error_cancelled`, `error_routing_denied`, `error_all_models_failed`, `error_exception`),
  `error_unledgered_exit` for a normal return that wrote nothing. A table the path already wrote is never written
  again, so cache hits, served calls and the #398/#409 rows are unchanged. All reasons start `error_` or are
  `breaker_open`, so `SQL_NOT_ERROR_ROW`, `SQL_REAL_DECISION` and `is_non_decision_reason` keep them out of
  metrics, bandit, judge and cost. The writer is fail-open and shielded; the reply or exception is never changed.
  The old `_log_breaker_refusal` helper is gone (the guard replaces it).
- **Not covered (cannot be).** A hard kill (SIGKILL, OOM, power loss, host crash) runs no handler, so that call
  stays unledgered. Other tools (`llm_edit`, `llm_image`, `llm_audio`, `llm_local_task`, `llm_route`) are not
  wrapped. `llm_act` may legitimately write many rows (one per milestone); the guard adds a row only when it
  wrote none. Pre-dispatch failures keep the generic `error_*` reason of their exception type, not a separate
  `pre_dispatch_*` code (a new prefix would need every predicate and hook copy changed).
- **Test that keeps it closed.** `tests/test_ledger_every_exit.py` forces every branch through the real tool
  function, router and writers (served, cache hit, provider error, empty chain, pre-dispatch budget, timeout,
  cancel, unexpected exception, silent return, breaker refusal, half-written, `llm_act` refusal and error) and
  asserts exactly one `usage` and one `routing_decisions` row with the caller's session id and provenance; a
  mutation test disables the exit writer and asserts the silent paths then leave zero rows; an AST test asserts
  the whole body of `llm` and `llm_act` sits inside the guard.
