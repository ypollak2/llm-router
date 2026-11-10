---
id: ECHO-1
status: fixed in `fix/codex-handoff-file-echo` (deploy: reinstall hooks so hook-version 35 reaches ~/.claude/hooks)
---
## ECHO-1. A successful Codex sub-agent handoff showed as a red "Error:" in Claude Code

- **Symptom (2026-11-10, owner).** A sub-agent task that `agent-route.py` ran on Codex CLI (NS1/NS3
  `agent_route_codex`) appeared in Claude Code as `Error: ...` although the task succeeded.
- **Cause.** The hook delivered the Codex result as a `permissionDecision: "deny"` (`_deny`, BIND F6). Claude
  Code labels every deny "Error:" whatever the reason says. No hook decision substitutes a tool result:
  PostToolUse `updatedOutput` is ignored for built-in tools (tested live 2026-09-12).
- **Fix.** On a successful delegation the result is written to
  `~/.llm-router/agent-results/<utc-ts>-<uuid8>.md` (file 0600, directory 0700) and the Agent call is
  ALLOWED with `updatedInput` = the original input with `model: haiku`, `subagent_type: general-purpose`,
  `run_in_background: false`, no `isolation`/`worktree`, and an echo prompt telling the sub-agent to reply
  with one line naming the file. The same allow+updatedInput mechanism already pins models. The deny remains
  only when the file cannot be written. The ledger row keeps lever and outcome and gains
  `delivery: "file_echo" | "deny_fallback"`. The handoff state records the delivery under a distinct id
  (`echo:<tool_use_id>`) so PostToolUse can check the actual input: a foreground Haiku echo is not counted as void. A non-echo input on an echo id is counted as `echo_unconfirmed` and is NOT charged as a void: whether PostToolUse reports the rewritten or the pre-rewrite input is not yet measured on a real host, and a false void would open the breaker and stop Codex delegation. Charge it only after a live handoff shows real echoes arrive with the rewritten input (`echo_unconfirmed` stays 0). Echo re-entry passes through before routing or ledger accounting. The result
  text is in the file only (D-281).
- **Retention.** Each save deletes only files matching `[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.md` in the directory older than `LLM_ROUTER_AGENT_RESULTS_DAYS` days
  (default 7; 0 keeps all), never the file just written, silent on OSError. The count is the `pruned` field of
  the `delegated` ledger row.
- **Not measured.** No token or cost effect is claimed; only the echo prompt length was measured.
- **Test.** `tests/test_codex_handoff_file_echo.py` (allow+updatedInput shape, file mode and exact content,
  path in the echo prompt, background and isolation keys, deny fallback on an unwritable directory, ledger
  `delivery` field, no void count for an allowed echo). The deny-shape tests in
  `tests/test_bind_agent_route_handoff.py` now force the fallback with `break_results_dir`.
