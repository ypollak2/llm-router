---
id: D112-EXECUTED-ROUTES
status: fixed in fix/dashboard-executed-routes
---
## D112-EXECUTED-ROUTES. Dashboards reported classifier hints as executed routes

- **Symptom.** On 2026-10-15 the every-10-prompts banner said "routes: last 200 · top tier: local (101)"
  while usage.db routing_decisions showed 16 executed routes, 0 of them Ollama
  (cost-sentinel read, window 2026-10-14T20:34Z..2026-10-15T20:34Z).
  Stop said "ROUTING  today  N decisions";
  `llm-router summary` said "N routing decisions".
- **Cause.** These counted hook classification hints from `model_tracking.jsonl` via
  `LineageStore.recent()` / `_query_routing_logic`, with no execution window.
- **Fix.** `routing_report.executed_routes()` counts `routing_decisions` rows with `provenance='runtime'`
  and `real_decision_sql` over the last 24h, grouped by `final_provider`; reads are read-only with a
  0.25 s timeout and return `None` when missing/unreadable. The three surfaces print
  executed counts and label classifier counts as hints. Stop now says
  `ROUTING  24h  N executed` with per-provider rows and `hints today: N classified`;
  its default condensed line shows `N executed (24h)` instead of `N routed`.
  `llm-router summary` uses its `--since-hours` window (default 24h).
  `llm_model_usage` labels classifier hints separately from executions;
  `explain-dashboard` documents both sources and their windows.
- **Test.** `tests/test_d112_executed_routes_dashboard.py`: non-UTC window boundary at
  -23h59m/-24h01m, per-provider grouping, provenance/cache exclusion, missing/unreadable DB,
  Stop main wiring, summary windows (including missing DB), classifier labels and explainer.
  `tests/test_stop_hook_verbosity.py`: captured and live panels preserve executed counts,
  omit counts without execution data, and never substitute classifier hints.
