---
id: BIND-STDOUT-1
status: fixed in `bind/mcp-holds` for `enforce-route.py` (latent: no incident observed in this hook); `auto-route.py`'s emit mechanism is unchanged, see Known limits
---
## BIND-STDOUT-1. `enforce-route.py` wrote its decision to a stdout that any library could write to first

- **Symptom.** None observed in this hook. The class was found on 2026-11-03 in `agent-route.py` (builder W1, BIND-R1):
  a log line printed to stdout before the block JSON made Claude Code ignore the decision, and 317 of 317 Codex
  handoffs of the 30-day outcome-audit window were void (`~/.rsi/research/primary-plan/v16/coord/BIND-R1-codex-handoff-20261103.md`). A hold or a local interception (the deny that delivers a
  command's compressed output) in `enforce-route.py` fails the same way: a hold that is not applied, or a command that runs
  both locally and again in the host.
- **Cause.** The hook wrote its decision with `json.dump(..., sys.stdout)` while every module it imports lazily
  (`llm_router.*`, `tool_intercept`, the ledgers) writes to the same stream when it prints or when structlog's default
  `PrintLogger` is active (the hook never configures logging). The host parses stdout as ONE JSON object, so one stray
  line before or after the decision voids it. Nothing enforced the contract.
- **Fix.** `main()` points `sys.stdout` at stderr for the whole run and the decision is written, by `_emit` only, to the
  stdout captured at entry. Anything stray lands on stderr. In-process callers (tests) get the decision on the stdout
  they hold, and `sys.stdout` is handed back on every exit, `sys.exit` included.
- **Known limits.** `auto-route.py` keeps its own mechanism (logging pinned to stderr by `_init_hook_logging`, then
  `json.dump` at about ten sites); this PR leaves that mechanism alone (builder W1 audits every hook for this class) and
  tests the prompt-hook paths it touched with `json.loads(stdout)` in a subprocess
  (`tests/test_w2_auto_route_holds.py`, every test parses stdout whole). A child process that inherits the hook's file
  descriptor 1 would still write to it; the interception path pipes the stdout of the commands it runs.
- **Test.** `tests/test_w2_enforce_route_holds.py`, each a subprocess whose library code prints a stray line to stdout:
  `test_the_block_decision_is_exactly_one_json_object_even_when_a_library_prints`,
  `test_the_local_interception_deny_is_exactly_one_json_object` (the second emit path: `cat` of a 400-line file run
  locally, denied with its compressed output), `test_a_probe_and_a_release_emit_nothing_on_stdout`, and
  `test_the_decision_goes_to_the_stdout_the_caller_holds_when_run_in_process`. Mutation check: with the one line
  `sys.stdout = sys.stderr` removed from a copy of the hook, the first three fail (stdout is
  `STRAY LOG LINE FROM A LIBRARY` or precedes the JSON) and the in-process test passes; with the line restored all four pass.
