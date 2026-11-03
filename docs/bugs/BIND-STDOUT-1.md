---
id: BIND-STDOUT-1
status: fixed in `bind/mcp-holds` for `enforce-route.py` (reproduced with an unwritable latency store, see Symptom; no live incident observed); `auto-route.py`'s emit mechanism is unchanged and was checked on the same path, see Known limits
---
## BIND-STDOUT-1. `enforce-route.py` wrote its decision to a stdout that any library could write to first, or after

- **Symptom.** No live incident observed in this hook. The class was found on 2026-11-03 in `agent-route.py` (builder W1, BIND-R1):
  a log line printed to stdout before the block JSON made Claude Code ignore the decision, and 317 of 317 Codex
  handoffs of the 30-day outcome-audit window were void (`~/.rsi/research/primary-plan/v16/coord/BIND-R1-codex-handoff-20261103.md`). A hold or a local interception (the deny that delivers a
  command's compressed output) in `enforce-route.py` fails the same way: a hold that is not applied, or a command that runs
  both locally and again in the host. Reproduced on 2026-11-03 for the exit-time write (review of PR #817), with a
  directory where `hook_latency.jsonl` goes and a temp `HOME`: the block path printed the JSON and then a structlog line
  (`fail_open ... code=CHZ-FO-HOOK-LATENCY-WRITE`) on stdout, and the probe path printed only that line, so the host read a
  hold that was not there to apply. The unmodified hook at the merge base behaves the same way; it is the same
  defect, and it needs only a full disk or an unwritable state directory.
- **Cause.** The hook wrote its decision with `json.dump(..., sys.stdout)` while every module it imports lazily
  (`llm_router.*`, `tool_intercept`, the ledgers) writes to the same stream when it prints or when structlog's default
  `PrintLogger` is active (the hook never configures logging). The host parses stdout as ONE JSON object, so one stray
  line before or after the decision voids it. Nothing enforced the contract. The first version of the fix put stdout back
  when `main()` returned, but `hook_latency` writes its row from an `atexit` handler, which runs after `main()` has
  returned and logs a failed write through structlog to `sys.stdout`.
- **Cause.** The hook wrote its decision with `json.dump(..., sys.stdout)` while every module it imports lazily
  (`llm_router.*`, `tool_intercept`, the ledgers) writes to the same stream when it prints or when structlog's default
  `PrintLogger` is active (the hook never configures logging). The host parses stdout as ONE JSON object, so one stray
  line before or after the decision voids it. Nothing enforced the contract.
- **Fix.** `main()` points `sys.stdout` at stderr for the whole run and the decision is written, by `_emit` only, to the
  stdout captured at entry. Anything stray lands on stderr. In-process callers (tests) get the decision on the stdout
  they hold, and `sys.stdout` is handed back on every exit, `sys.exit` included. The script entry calls
  `main(restore_stdout=False)`: `sys.stdout` stays on stderr until the process is gone, so the `atexit` latency write
  cannot put a line behind the JSON either.
- **Known limits.** `auto-route.py` keeps its own mechanism (logging pinned to stderr by `_init_hook_logging`, then
  `json.dump` at about ten sites); this PR leaves that mechanism alone (builder W1 audits every hook for this class) and
  tests the prompt-hook paths it touched with `json.loads(stdout)` in a subprocess
  (`tests/test_w2_auto_route_holds.py`, every test parses stdout whole). The same unwritable-latency-store run against
  `auto-route.py` (a hold, an exempt short prompt, the `claude:` bypass) leaves stdout as one JSON object or empty, with
  the log line on stderr, because that hook pins logging to stderr; the test below keeps it that way. A child process
  that inherits the hook's file descriptor 1 would still write to it; the interception path pipes the stdout of the
  commands it runs.
- **Test.** `tests/test_w2_enforce_route_holds.py`, each a subprocess whose library code prints a stray line to stdout:
  `test_the_block_decision_is_exactly_one_json_object_even_when_a_library_prints`,
  `test_the_local_interception_deny_is_exactly_one_json_object` (the second emit path: `cat` of a 400-line file run
  locally, denied with its compressed output), `test_a_probe_and_a_release_emit_nothing_on_stdout`, and
  `test_the_decision_goes_to_the_stdout_the_caller_holds_when_run_in_process`. Mutation check: with the one line
  `sys.stdout = sys.stderr` removed from a copy of the hook, the first three fail (stdout is
  `STRAY LOG LINE FROM A LIBRARY` or precedes the JSON) and the in-process test passes; with the line restored all four pass.
  For the exit-time write, `test_a_failing_exit_time_write_puts_no_line_behind_the_decision` runs the real script
  (block, probe, release, local interception, each with the latency store alone and with the fail-open store also
  unwritable: 8 cases) and asserts both that stdout parses as one object (or is empty) and that the failure was real
  (`CHZ-FO-HOOK-LATENCY-WRITE` on stderr). Run against the first version of this fix (commit 549d02a9) all 8 fail with
  `JSONDecodeError: Extra data`. `tests/test_w2_auto_route_holds.py::test_a_failing_exit_time_write_puts_no_line_behind_the_prompt_hooks_json`
  (3 prompts) pins the prompt hook.
