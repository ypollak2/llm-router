---
id: BINDAUDIT-7
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-7. Replayed transcript history counted the same tool call twice

- **Symptom.** `~/.rsi/research/primary-plan/v16/coord/BIND-R2-mcp-unused-20261103.md` section 1: session `dc495378` replays part of `5041bdf8`'s history (8 tool_use ids with the
  same timestamps: 4 `llm`, 4 `llm_local_task`), so n=158 routed_mcp rows were 150 calls. Re-measured on the
  16.0.0 window with `scripts/release/outcome_audit.py`: 8 replayed tool_use ids.
- **Cause.** Units were built per session with no memory of which tool_use ids an earlier session already produced.
- **Fix.** `_mcp_and_edit_units` walks the sessions oldest first and keeps each tool_use id once; the summary reports
  `dedup.replayed_tool_uses`. A duplicate is dropped, not relabelled, so it leaves `attempted` too. Not covered: a
  replay whose original is in a session the audit does not load (a non-organic one) still counts in the replaying
  session.
- **Test.** `tests/test_bind_audit.py::test_replayed_tool_uses_count_once_and_belong_to_the_older_session`.
