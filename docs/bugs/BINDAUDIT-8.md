---
id: BINDAUDIT-8
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-8. A backgrounded MCP call was scored on its stub, not on its result

- **Symptom.** BIND-R2 section 8: calls over 120 s are moved to the background and the transcript holds
  `MCP tool "..." is still running after 120s. It was moved to the background as task <id>`; the stub was compared
  with the host's reply (3 units: rows 5, 29 and 67; row 67 later reported a failed local run).
- **Cause.** `_result_text` of the tool_result was the unit's result whatever it said.
- **Fix.** `_mcp_call_unit` recognises the stub, looks for the `<task-notification>` user message with that task id
  (`_find_notification`) and scores the notification's result, with the host's reply taken from the notification
  on. A notification that is not in the transcript leaves the unit `unknown / background_result_missing`; a
  failed task is an error result.
- **Test.** `tests/test_bind_audit.py::test_background_stub_is_followed_to_its_notification` and
  `::test_parse_notification`.
