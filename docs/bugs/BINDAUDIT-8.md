---
id: BINDAUDIT-8
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-8. A backgrounded MCP call was scored on its stub, not on its result

- **Symptom.** `~/.rsi/research/primary-plan/v16/coord/BIND-R2-mcp-unused-20261103.md` section 8 (n=3 units: rows 5, 29 and 67): a call that runs past the harness's two-minute limit
  is moved to the background and the transcript holds `MCP tool "..." is still running ... It was moved to the
  background as task <id>`; the stub was compared with the host's reply (row 67 later reported a failed local run).
- **Cause.** `_mcp_and_edit_units` in `scripts/release/outcome_audit.py` took the tool_result of the call as the
  unit's result whatever it said.
- **Fix.** `_mcp_call_unit` recognises the stub, looks for the `<task-notification>` user message with that task id
  (`_find_notification`) and scores the notification's result, with the host's reply taken from the notification
  on. A notification that is not in the transcript leaves the unit `unknown / background_result_missing`; a
  failed task is an error result.
- **Test.** `tests/test_bind_audit.py::test_background_stub_is_followed_to_its_notification` and
  `::test_parse_notification`.
