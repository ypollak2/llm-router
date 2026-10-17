---
id: L2-SE-1
status: fixed in `perf/l2-session-end-latency` (D-171 L2)
---
## L2-SE-1. The Stop re-ran ledger-wide aggregates and imported pydantic on every turn

- **Symptom.** Live session-end p95 was 4,237 ms against the +300 ms sync bar. A synthetic
  ledger reproduced the mechanism (latency investigation of 2026-10-17, n = 20 cold runs per
  size, load1 6 to 13): `router_added_ms` p50 500 ms at 50k rows per table and 2,302 ms at
  250k, against 92 ms on an empty ledger. sqlite `execute` took 2,530 ms of a 2,945 ms profile.
- **Cause.** `session-end.py` called `dashboard_data.query_window` for five windows,
  `summary("today")`, `summary("lifetime")` (each repeating `query_window`,
  `query_primary_metric`, `query_model_savings`), `query_window("lifetime")` again for the
  unverified note and `query_daily(14)`, so "lifetime" ran 3 times and "today" twice. "today"
  used `date(timestamp,'localtime')=...`, which cannot use an index. Separately the module
  imported `llm_router.cost` for two price constants, which pulls `config` and pydantic
  (70 ms of the empty-ledger Stop).
- **Fix.** The detached child computes each aggregate once and writes `ledger_aggregates.json`
  (atomic, versioned, one-hour limit, same-local-day check); the Stop reads it. The figures
  are one turn old, and a first Stop shows the line without them. `cost` is not imported
  (`pricing.savings_baseline_rates()`, `savings.production_only`, `config_lite`).
- **Not fixed.** The Stop still runs bounded per-session and 14-day queries on the ledger
  (`model_breakdown`, `executed_routes`, `_query_session_metrics`, `_query_daily_cache_trend`,
  `_query_session_data`); their cost still grows with the ledger, more slowly. Measured on the
  fixed Stop at 250k rows per table (full mode, one run, load1 about 30): `_query_session_data`
  305 ms, `_query_session_metrics` 15-day part 190 ms, `model_breakdown` 172 ms,
  `_query_daily_cache_trend` 125 ms, `executed_routes` 87 ms.
- **Test.** `tests/test_l2_stop_ledger_cache.py`: `test_stop_runs_no_ledger_wide_aggregate`,
  `test_child_computes_each_aggregate_once`, `test_cached_render_is_byte_identical_to_the_live_computation`,
  `test_stop_path_imports_no_pydantic`, `test_unusable_cache_never_raises_and_never_queries`.
