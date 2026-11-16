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
- **Cause 2 (W2, breaker).** `proxy/backend_health.classify` put `budget_exceeded` in the NEUTRAL set with
  `hedge_timeout`. A backend that never answered therefore never opened the breaker, and every step paid the whole
  budget before falling back.
- **Fix W1.** `have != want_ctx` is a load, the same path as the existing `have < want_ctx` branch. A resident model at
  exactly the requested window, or one that reports no window, still adds nothing.
- **Fix W2.** A `budget_exceeded` step in which zero stream lines arrived from the backend is a FAIL: it feeds the
  existing streak and `fail_n` opens the breaker. The signal is a one-slot counter (`begin_stream_progress`, set by the
  proxy before `backend.complete`; `OllamaBackend` calls `mark_stream_line` on every line). A step that received any
  line stays NEUTRAL, as does `hedge_timeout`, and as does a `budget_exceeded` raised before the backend was called
  (no counter installed). Rows for every other reason are unchanged.
- **Test.** `tests/test_mem_guard_window_mismatch.py` (W1: 131072 resident / 32768 asked and 8192 / 32768 count a load;
  equal window and unreported window do not) and `tests/test_proxy_budget_exceeded_breaker.py` (W2: classify table,
  `fail_n` zero-line timeouts open the breaker, lines and `hedge_timeout` do not, and the real `OllamaBackend` under
  `asyncio.wait_for` leaves a zero or non-zero counter).
