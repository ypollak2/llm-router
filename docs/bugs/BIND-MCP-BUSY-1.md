---
id: BIND-MCP-BUSY-1
status: fixed in `bind/mcp-holds` (deploy: restart the MCP server, i.e. reconnect with `/mcp`)
---
## BIND-MCP-BUSY-1. Every call on a busy Ollama waited the full 90 s deadline again

- **Symptom (BIND-R2 section 5, window 2026-10-04 to 2026-11-03).** 5 of the 16 MCP tool errors were
  `deadline 90s exceeded; tried: ollama/...; local models may be busy (Ollama queue)`, all in session dc495378
  (2026-10-17 17:27, 17:31, 17:49, 18:47 and 2026-10-18 16:43), while a local benchmark held Ollama: 5 x 90 s = 7.5
  minutes of waiting. 3 of the 5 show hold evidence (a block in the turn or a hold notice; the other 2 show none), and the
  model noted that a hold-release call reached the busy model and saved a rule not to send hold-release calls to Ollama
  during a benchmark (BIND-TOLL-1).
- **Cause.** The 90 s overall deadline and the half-deadline cap on a local attempt (D-141 / D-154) bound one call. Nothing
  remembered that the last call had just waited out its whole deadline on local models, so the next call did the same.
  `AttemptCapExceeded` deliberately sets no LOCAL-TIMEOUT-1 cooldown (a cap is not a provider fault), and a full deadline
  expiry set nothing either.
- **Fix.** `mcp_door`: after a deadline that expired with only local (`ollama/`) models tried, a busy window opens for
  `BUSY_WINDOW_S` (300 s). In the window, a call that is still on local models only `BUSY_PROBE_S` (20 s) after it
  started is given up at once with `llm-router error: local models are busy: still waiting on <models> after 20s, and the
  last call waited <n>s for them; gave up early (limit 90s). Retry in a minute, use tier=fast, or handle it yourself.`
  A call that has reached a cloud model by then is never cut (cloud attempts legitimately take 45 to 120 s, D-154), a call
  that answers before the probe is untouched, and outside the window the full deadline applies. The primary fix for the
  rows that were hold-release probes is BIND-TOLL-1 (nobody needs such a call now); this bounds the wait of a real call.
- **Known limits.** The first call after Ollama gets busy still waits its full deadline; only the following ones in the
  window fail fast. The window is per MCP server process. A call that was going to succeed in 25 s on a slow-but-alive local
  model is cut at 20 s while the window is open.
- **Test.** `tests/test_w2_mcp_door_errors.py`: `test_a_local_only_deadline_opens_the_busy_window`,
  `test_a_cloud_attempt_in_the_trace_does_not_open_it`, `test_in_the_window_a_still_local_call_is_given_up_early` (exact
  text, under 2 s against a 3 s deadline), `test_in_the_window_a_call_that_reached_a_cloud_model_is_left_alone`,
  `test_an_answer_before_the_probe_is_untouched`, `test_outside_the_window_the_full_deadline_applies`,
  `test_a_deadline_opens_the_window_for_its_whole_length`, `test_the_busy_window_closes`. The existing D-141 deadline tests (`tests/test_d141_mcp_readable_errors.py`, 32 tests)
  still pass. Mutation checks (one line changed in `mcp_door.py`, then restored): not scheduling the watchdog fails
  `test_in_the_window_a_still_local_call_is_given_up_early`; cutting a call that reached a cloud model fails
  `test_in_the_window_a_call_that_reached_a_cloud_model_is_left_alone`; never opening the window fails
  `test_a_local_only_deadline_opens_the_busy_window`. On unmodified origin/main 5c056309 all 8 fail, because the
  busy-window API does not exist there.
