---
id: STALL-1
status: fixed in `fix/window-budget-stall` (deploy: restart the proxy)
---
## STALL-1. A local runner at a different window stalled every step to the full budget, and the breaker never opened

- **Symptom (2026-11-16).** The first call of 69 of 186 sub-agent conversations (24 h of `~/.llm-router/proxy_calls.jsonl`,
  counts only) timed out at the 120 s step budget and then went to Claude; each such conversation stayed pinned to
  Claude. Diagnosis: `coord/DIAG-S1-PILOT2-LOCAL-20261116.md` section 3, items 2 and 3.
- **Cause 1 (W1, memory guard).** `toolkit/adapters/ollama._verdict` returned "nothing to add" when the model was
  resident and `have >= want_ctx`. Ollama keys a runner on num_ctx (`docs/bugs/CLSCTX-1.md`), so a runner held at 131072
  and a request for 32768 is a reload, not a hit. The guard never counted it and the request waited on the busy runner.
  Counting the load prevents the reload only when memory is short (the guard then refuses and the step goes to
  Claude at once); when memory is enough the reload still happens, and W2 bounds the stall.
- **Cause 2 (W2, breaker).** `proxy/backend_health.classify` put `budget_exceeded` in the NEUTRAL set with
  `hedge_timeout`. A backend that never answered therefore never opened the breaker, and every step paid the whole
  budget before falling back.
- **Fix W1.** `have != want_ctx` is a load, the same path as the existing `have < want_ctx` branch. A resident model at
  exactly the requested window, or one that reports no window, still adds nothing. The window compared is the one the
  caller really sends: `OllamaBackend`, the proxy edit-protocol step (`local_agent/proxy_step.py`) and the calibration
  completer (`resolver/calibrate.py`, when it sends an explicit `num_ctx`) pass the window they send to
  `ensure_fits(num_ctx=...)`. No proxy-path caller sends a window that differs from the one it guards on. Callers that
  still guard on the `local_models` table value: `backends.py:382` (sends the table window via `pack_overflow.py:226`,
  checked: the two match), `react.py:381`, `hooks/agent_loop.py:630`, `warm.py:232`, `hooks/session-start.py:1831`,
  `vision_registry.py:205`, `sealer.py:104`, `local_agent/compact.py:291`, `semantic_cache.py:453`,
  `semantic_classify.py:117` and `check_resident` (`ollama.py:741`). Only `backends.py:382` was checked. The others are
  outside the proxy path and were not checked; `hooks/agent_loop.py:630` guards the overflow-swap model on the table
  value while it sends `pack.num_ctx` (l.636), which can differ. A caller that sends its own window must pass `num_ctx`.
- **Intended behaviour.** A downward reload (resident larger than requested) is refused when free memory is short.
  That protects the shared runner from being reloaded at a smaller window by a side caller.
- **Fix W2.** A `budget_exceeded` step in which the request was sent and neither response headers nor stream lines came
  back is a FAIL: it feeds the existing streak and `fail_n` opens the breaker. The signal is a one-slot progress
  counter (`begin_stream_progress`, installed by the proxy before `backend.complete`): -1 not sent, 0 after
  `mark_request_sent()` (called by `OllamaBackend` just before the HTTP request), then +1 for the response headers and
  +1 per line (`mark_response_progress()`). Only 0 is a FAIL. Not sent (guard, prepare or queue ate the budget), headers
  or lines received, `hedge_timeout`, no counter, and a backend that never calls the markers all stay NEUTRAL. Rows for
  every other reason are unchanged.
- **What "sent" covers (intended).** `mark_request_sent()` runs before connection-pool acquire and connect. So an
  `httpx.PoolTimeout` or `ConnectTimeout`, or a request queued inside the runner (for example behind another client's
  long generation) that returns no response headers within the step budget, also counts as a breaker failure. That is
  intended: a runner that returns nothing within the step budget is the stall this fix addresses, whatever the reason.
- **Test.** `tests/test_mem_guard_window_mismatch.py` (W1: 131072 resident / 32768 asked and 8192 / 32768 count a load;
  equal window and unreported window do not; explicit `num_ctx` beats the table; the proxy edit-protocol step and a
  calibration probe are judged on the window they send) and
  `tests/test_proxy_budget_exceeded_breaker.py` (W2: classify table, `fail_n` silent timeouts open the breaker,
  not-sent, headers-only, lines and `hedge_timeout` do not, the real `OllamaBackend` under `asyncio.wait_for` leaves
  -1, 0 or a positive counter, and `build_app` with a stalled fake backend serves `backend_unhealthy` after `fail_n`).
