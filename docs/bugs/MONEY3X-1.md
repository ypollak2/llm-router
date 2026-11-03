---
id: MONEY3X-1
status: fixed in `bind/honest-statusline` (deploy: restart the MCP server and every process that imports `llm_router.ledger_view`)
---
## MONEY3X-1. The estimated saving counted one local call three times: `today n=46` was rows over three tables

- **Symptom (2026-11-03).** The status line printed `~$0.35 est. saved` with `today n=46`, while the session mix saw 5 local calls. The
  same 5 calls sat in `usage`, `claude_usage` and `savings_stats` (15 rows), so 10 of the 46 rows and 43% of the dollars
  were repeats (copy of `usage.db`, window today 2026-11-03, n=46 rows, `~/.rsi/research/primary-plan/v16/coord/NS-STATUSLINE-ANALYSIS-20261103.md`). Over 7 d the same defect was 287 of 940 rows.
- **Cause.** `ledger_view.money_window` summed COUNT(*) and the saved dollars of each table, and the router records one local call in
  all three: they share the model and token counts, `usage.correlation_id` equals `savings_stats.session_id`, and the timestamps are
  written a moment apart (13 of 800 id-sharing pairs differed by one second, `src/llm_router/ledger_view.py`).
- **Fix.** `ledger_view.duplicate_calls` names the repeats: the key is the call id where both rows carry one (`usage.correlation_id`
  and `savings_stats.session_id`; `claude_usage` has none), else the model, the input and output tokens and a timestamp within
  `CALL_MATCH_WINDOW_S` (two seconds; 800 id-sharing pairs measured, `src/llm_router/ledger_view.py`). Matching is one-to-one and only across tables (identical calls of one table stay two), the priority is
  `usage`, `savings_stats`, then the platform tables, and the repeats leave `unverified_calls` and `unverified_usd`
  (`MoneyWindow.duplicate_calls` and `duplicate_usd` report them). Every surface reads that window, so they move together. The status line
  prints `n=36 calls` (a call is counted once), not `n=46`.
- **Test.** `tests/test_w8_mix_money.py` (the 5-call triple, id beats clock, no id, a minute apart stays two, identical rows of one
  table stay two, one-to-one, priority, a verified row neither dedupes nor is deduped, a table without a model column).
