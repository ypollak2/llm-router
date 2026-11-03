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
  and `savings_stats.session_id`; `claude_usage` has none) together with the same model and token counts, else the model, the input and output tokens and a timestamp within
  `CALL_MATCH_WINDOW_S` (two seconds; 800 id-sharing pairs measured, `src/llm_router/ledger_view.py`). Matching is one-to-one and only across tables (identical calls of one table stay two), the priority is
  `usage`, `savings_stats`, then the platform tables, and the repeats leave `unverified_calls` and `unverified_usd`
  (`MoneyWindow.duplicate_calls` and `duplicate_usd` report them). **Surfaces that move together** (they read `money_window`): the status line
  money segment (`n=N calls`, a call counted once), the status-bar hook, the Stop hook's ledger aggregates (`query_window`, `window_summary`),
  the `savings-report` headline, `observability.summary` and the `kpi` ledger line. The daily series (`dashboard_data.query_daily`: the Stop
  hook's 14-day series, the TUI chart) takes the repeats out of its day's unverified dollars too (`ledger_view.repeated_calls`), so it sums to
  the window's figure; its `calls` and `tokens` are the raw rows per table, as `WindowTotals.calls`, `tokens` and `by_source` are.
  **Surfaces that still sum raw rows per table**: the per-model block of `savings-report` (`query_model_savings`, the `savings_stats` table
  alone, which holds rows that `usage` also holds), `query_by_platform` (one row per source table) and `query_model_distribution`.
  **Known limit** (the review's simulation of `duplicate_calls`, 30 trials per row, `~/.rsi/research/primary-plan/v16/coord/rv-PR821-20261103.md`):
  distinct calls with the same model and token counts inside two seconds, recorded unevenly across tables, can be merged and so
  under-counted; none was found in the real ledger.
- **Test.** `tests/test_w8_money_surfaces.py` (the daily series sums to the window; no repeat, no change), `tests/test_w8_mix_money.py` (the 5-call triple, id beats clock, no id, a minute apart stays two, identical rows of one
  table stay two, one-to-one, priority, a verified row neither dedupes nor is deduped, a table without a model column).
