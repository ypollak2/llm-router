---
id: BINDAUDIT-2
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-2. The audit never read sub-agent transcripts, so a nested Agent spawn was unknown/no_agent_call_found

- **Symptom.** 9 of the 406 Codex units in `outcome_audit_16.0.0.jsonl` were `unknown / no_agent_call_found`; 8 of
  them were nested spawns (BIND-R1 section 4). All 9 were as void as the 308 labelled `redone`. 10 of the 317
  delegated rows have their hook run and Agent call only in a sub-agent transcript.
- **Cause.** `load_sessions` globbed `projects/*/*.jsonl`. A spawn made by a sub-agent is written, with its hook
  attachment and its tool_result, to `<session>/subagents/agent-*.jsonl`; the ledger row carries the parent session
  id, so the audit looked for the call in the one place it is not.
- **Fix.** `_index_agents` scans the session and every `subagents/agent-*.jsonl` for Agent calls, hook runs and
  results (lines are pre-filtered by substring: the six sessions that hold Codex units have 1,764 sub-agent files, 1.5 GB,
  measured 2026-11-03), and reads each `agent-*.meta.json`, whose `toolUseId` says that a Claude sub-agent was launched for
  that call. A launch is recognised by an "Async agent launched" result, a `toolUseResult` `agentId`/`status`, or the
  meta file, so a result that is a deny is still void when a sub-agent ran. On the 16.0.0 window 0 of 317 delegated
  rows end unpaired and 10 pair inside a sub-agent transcript (`binding.paired_in_sub_agent_transcript`).
- **Test.** `tests/test_bind_audit.py::test_a_nested_spawn_lives_in_the_sub_agent_transcript` and
  `::test_a_sub_agent_launch_is_found_in_meta_json_even_when_the_result_is_a_deny`.
