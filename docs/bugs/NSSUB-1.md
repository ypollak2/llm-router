---
id: NSSUB-1
status: fixed in `bind/honest-statusline` (deploy: restart the MCP server; the status line refresher reads the new inputs by itself)
---
## NSSUB-1. NS never read sub-agent transcripts, counted records instead of calls, and gave MCP units no model

- **Symptom (2026-11-03).** Of this week's 7,112 Claude calls (7 d, organic, unique API messages), 6,180 were sub-agent calls (all
  Sonnet) and none was among NS's 2,088 units; the main thread was over-counted about 2x (2.15 transcript records per API message; one
  session had 12,368 sub-agent records for 6,124 message ids). The two routed MCP units in the window had `model None`, so no reader could
  say who answered them (`~/.rsi/research/primary-plan/v16/coord/NS-STATUSLINE-ANALYSIS-20261103.md`, n=7,112 calls, 7 d to 2026-11-03T19:07Z).
- **Cause.** `northstar._session_files` globbed only `<project>/*.jsonl`, but Claude Code writes a sub-agent's work to
  `<project>/<session>/subagents/**/agent-*.jsonl`. A `claude_main_call` unit was one assistant RECORD, and Claude Code writes one record
  per content block, all sharing `message.id`. `Unit.model` was set only for proxy-served turns, never for a Claude call or an MCP call.
- **Fix.** `northstar.units(subagents=True)` (opt-in: the Stop hook and the quality breaker call `units()` on hot paths and a busy session
  holds hundreds of MB of sub-agent transcripts; `llm-router kpi` and so the status line pass it) folds the sub-agent files into the
  parent session, which is the directory name. A unit is one unique `message.id` per session across its main and sub-agent files, a
  `<synthetic>` record is no call, and a record with no id stays one unit. Every Claude call carries `message.model`, a proxy-served
  message carries the served model, and an MCP unit takes the model of the `routing_decisions` row whose `tool_use_id` is its
  `tool_use` id (the last served row, `real_decision_sql`).
- **Test.** `tests/test_w8_ns_inputs.py` (16 cases: record dedupe, synthetic, tool_use in a later record, nested workflow agents, the
  window pre-filter, synthetic and sandbox parents, an MCP retry with two rows, no database); a recompute of the same window on a copy of
  `~/.llm-router` and of the organic transcripts gave 7,227 calls, 4 of them outside Claude.
