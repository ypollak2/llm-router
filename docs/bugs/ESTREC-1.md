---
id: ESTREC-1
status: fixed in `fix/estimate-recursion`
---
## ESTREC-1. A deeply nested payload raised RecursionError out of the local token estimate

- **Symptom.** Found by code reading and reproduced on `0ca251b4` (n=1 run, payload nested 5000 levels):
  `estimate_payload_tokens` raised `RecursionError` instead of returning an estimate, so every caller of the
  context guard (`check_overflow`, the proxy backend, the MCP `llm()` path) failed on the request instead of escalating it.
  No live ledger row is cited; no live frequency is claimed.
- **Cause.** `src/llm_router/local_context_guard.py:124-127`: `json.dumps(payload, default=str)` raises
  `RecursionError` (a `RuntimeError`, not `TypeError`/`ValueError`) on deep nesting, and the legacy path caught only
  `(TypeError, ValueError)`. The `sum(len(str(v)) ...)` fallback recurses through `str()` on the same structure. The
  calibrated path below it already caught `RuntimeError`.
- **Fix.** Both the `json.dumps` call and the `str()` fallback catch `RecursionError` and return
  `UNMEASURABLE_TOKENS` (10**9), larger than any context window. Choice: an unmeasurable request is treated as over
  every window, so `check_overflow` refuses it and it goes to Claude rather than to a local model that would truncate it.
- **Test.** `tests/test_local_context_guard.py::test_estimate_payload_tokens_survives_a_5000_deep_payload` and
  `::test_a_5000_deep_payload_is_an_overflow_not_a_crash`. Mutation check (src change reverted, n=1 run): both fail
  with `RecursionError`.
