---
id: BIND-MCP-STALE-1
status: fixed in `bind/mcp-holds` (deploy: restart the MCP server, i.e. reconnect with `/mcp`)
---
## BIND-MCP-STALE-1. A tool crash in an MCP server older than its package came back as a bare error until the owner reconnected

- **Symptom (BIND-R2 section 5, the 16 tool errors among 158 `routed_mcp` rows; window 2026-10-04 to 2026-11-03; one
  unblinded reader).** 5 of the 16 were a bare `Error executing tool llm` or `llm_route` with no detail, all in session
  5041bdf8 (2026-10-15 12:27, 20:41, 20:41, 20:42; 2026-10-16 06:31), and the calls worked again after the owner
  reconnected the MCP ("the reconnect worked"). The other 11: 4 `task="research"` calls (BIND-R2 E1b), 1 bare error in a
  short test session with no visible cause (E1c), 5 `deadline 90s exceeded` (BIND-MCP-BUSY-1), 1 `Connection closed`.
- **Cause.** The MCP server lives as long as the Claude Code session. A deploy (an upgrade, a pull into an editable
  install) replaces the package on disk under a process that loaded the old modules at start; the next lazy import
  pulls a new module into an old process and the tool crashes. mcp >= 2.2 hides the exception's message, which was the
  D-141 bug; D-141 made the text readable (`llm-router error: ImportError: ...`) but a reader still cannot tell a
  stale server from a broken one, and the fix is a reconnect, not a code change.
- **Fix.** `mcp_door.snapshot_start()` (called by `server.py` at start) records a fingerprint of the installed package:
  the distribution version, the number of `.py` files and the newest mtime. When a tool crashes, `run_door` compares it
  with the package on disk. If it changed, the result starts `llm-router error: this MCP server is running code from before
  the last update (started <time>; <version a -> b | +n python file(s) | files changed on disk>) ... Reconnect it with /mcp
  (or restart Claude Code) and retry.` and the original, secret-scrubbed error follows on the next line
  (`original error: <Type>: <message>`, then `tried: ...`). A crash in a server whose package did not change keeps the plain
  D-141 text. No snapshot (a test server, a custom embedding) is never reported as stale. The check runs only when a
  tool has already crashed (one directory walk of the package, `mcp_door.code_fingerprint` in `src/llm_router/mcp_door.py`: 629 files,
  median 1.8 ms and maximum 13.9 ms over 30 runs on the builder's machine, 2026-11-03).
- **Not fixed here, and why.** Research (4 rows): already fixed before this PR by D-141 (readable error, 2026-10-16) and F25
  (an empty research chain is a refusal naming the empty chains, 2026-10-17; `docs/bugs/F25.md` describes the same
  2026-10-16 12:48 to 16:07 errors from the ledger side); `tests/test_w2_mcp_door_errors.py::test_research_with_no_provider_is_an_answer_that_says_what_to_do` pins
  the call end to end through the real server (it passes on unmodified main, which is the point). The unexplained
  short-session error (1) has no transcript detail. `Connection closed` (1) is the client's text when the server
  process dies under a deploy; the server cannot write it. A stale server whose tools do not crash is not reported
  (nothing failed).
- **Test.** `tests/test_w2_mcp_door_errors.py`: `test_a_crash_in_a_stale_server_says_reconnect_and_keeps_the_original_error`
  (over the MCP protocol; checks `/mcp`, the version change, the scrubbed original error and the `tried:` line, and that
  the bare text is absent), `test_the_real_llm_door_reports_a_stale_server` (the real `llm` door),
  `test_a_crash_in_a_fresh_server_keeps_the_plain_readable_error`, `test_the_reason_says_what_changed` (3 cases),
  `test_no_snapshot_means_no_stale_claim`, `test_an_unchanged_package_is_not_stale`,
  `test_the_fingerprint_moves_when_a_file_is_added_or_touched`, and `test_the_server_takes_its_snapshot_at_start`
  (importing `llm_router.server` in a subprocess starts the clock). Mutation checks (one line changed in `mcp_door.py`, then restored):
  removing the stale branch of `run_door` fails `test_a_crash_in_a_stale_server_says_reconnect_and_keeps_the_original_error`;
  treating a missing snapshot as a changed package fails `test_no_snapshot_means_no_stale_claim`. On unmodified
  origin/main 5c056309 18 of the 19 tests in the file fail (the file also holds the busy-window tests) and the research
  test passes.
