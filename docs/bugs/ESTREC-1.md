---
id: ESTREC-1
status: fixed in `fix/estimate-recursion`
---
## ESTREC-1. A deeply nested payload raised RecursionError out of the token estimates and the proxy

- **Symptom.** Found by code reading and reproduced on `0ca251b4` (n=1 run per site; payloads nested 5000 and 500 levels):
  `estimate_payload_tokens` raised `RecursionError`. Through the proxy (ASGI test client, n=1 per depth): a 5000-deep
  request body made `json.loads` raise in `handle` (500, no row); a 500-deep tool schema parsed, then failed in the
  estimators and `deepcopy` and was forwarded with the misleading reason `backend_error`. No live ledger row is cited;
  no live frequency is claimed.
- **Cause.** `src/llm_router/local_context_guard.py:124-127` caught only `(TypeError, ValueError)` around
  `json.dumps` (RecursionError is a RuntimeError), and the `str()` fallback recurses too. The same uncaught
  `json.dumps` sat in `token_estimate.est_tokens` (line 31, used by admission) and `okf_ceiling` in `proxy/server.py`.
  `handle` caught only ValueError from `json.loads`.
- **Fix.** No number is invented for an unmeasurable payload. `token_estimate.Unmeasurable` (a ValueError) is raised by
  `est_tokens`, `estimate_payload_tokens` and `okf_ceiling`. `check_overflow` turns it into `ContextOverflow`
  ("unmeasurable", failopen detail `estimated=unmeasurable`). In the proxy, `handle` forwards a body that json.loads
  cannot nest-parse untouched; `local_step` and `try_serve` catch `Unmeasurable`/`RecursionError` and forward to Claude
  with the new reason `prompt_unmeasurable` (a forced `local:` step is refused with that reason). `est_prompt_tokens`,
  size_bucket, savings and cost never see a placeholder number.
  Residual: other direct callers of `estimate_payload_tokens` (providers.py, hooks) now see `Unmeasurable` instead of
  `RecursionError`; both are exceptions, and neither was a number.
- **Test.** `tests/test_local_context_guard.py::test_estimate_payload_tokens_raises_unmeasurable_on_a_too_deep_payload`,
  `::test_check_overflow_refuses_a_too_deep_payload_and_never_prints_a_number`;
  `tests/test_proxy_local_mode.py::test_a_deeply_nested_request_goes_to_claude_and_never_crashes` (levels 100000, 5000 and 500).
  On the previous head the proxy test fails for 5000 (status 500) and for 500 (reason `backend_error`).
- **Python version note (2026-11-10).** The depth at which `json.dumps` and `str()` raise `RecursionError` depends on the interpreter. Measured locally, n=1 run per version: CPython 3.11.15 raises at 5000; 3.13.14 and 3.14.6 do not raise at 5000 but do at 100000. The first CI run on 3.13 failed the guard tests at 5000 for that reason, so the guard tests use 100000 levels. The proxy test runs 100000, 5000 and 500 levels.
