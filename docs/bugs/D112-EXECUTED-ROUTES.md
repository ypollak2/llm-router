---
id: D112-EXECUTED-ROUTES
status: fixed in fix/dashboard-executed-routes
---
## D112-EXECUTED-ROUTES. Dashboards reported classifier hints as executed routes

- **Symptom.** On 2026-10-15 the every-10-prompts banner said "routes: last 200 · top tier: local (101)"
  while Ollama had executed 0 routes in 24h. Stop said "ROUTING today N decisions";
  `llm-router summary` said "N routing decisions".
- **Cause.** These counted hook classification hints from `model_tracking.jsonl` via
  `LineageStore.recent()` / `_query_routing_logic`, with no execution window.
- **Fix.** `routing_report.executed_routes()` counts `routing_decisions` rows with `provenance='runtime'`
  and `real_decision_sql` over the last 24h, grouped by `final_provider`; reads are read-only with a
  0.25 s timeout and return `None` when missing/unreadable. The three surfaces print
  `routes 24h: N executed · <provider> n · local n` and label classifier counts as hints.
- **Test.** `tests/test_d112_executed_routes_dashboard.py`: window boundary at -23h59m/-24h01m, per-provider grouping, provenance/cache exclusion, missing/unreadable DB, and banner/Stop panel/summary rendering.
