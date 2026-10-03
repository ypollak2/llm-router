# KPI ledger

One row per change, per the change rule in the owner's KPI spec (adopted 2026-10-03,
`~/.rsi/research/kpis/KPI-SPEC.md`): every PR names its primary KPI, the expected
direction and size, and the guardrails it touches. The verdict column is filled in
only after the live measure (7 days before vs after, organic sessions only) and the
offline measure (frozen sets, CI, n). Until then it reads `pending`.

KPI codes: NS non-Claude-and-used share; O1 quota avoided (est.); O2 quality held;
D1 offered off Claude; D2 success when tried; D3 redo rate; D4 tier mix;
D5 classifier accuracy; G1 added latency; G2 silent failures; G3 ledger
completeness; G4 wrongly benched providers.

| PR | Change | Primary KPI | Expected direction and size | Guardrails touched | Offline measure | Live measure | Verdict |
|----|--------|-------------|-----------------------------|--------------------|-----------------|--------------|---------|
| telemetry: record what the KPIs need (`feat/kpi-instrumentation`) | Session tag (organic / research / harness / headless) on proxy, edit, agent-call and north-star ledgers; `tier_proposed`, `tier_policy_version`, `tier_retry` on every proxy row; per-event used / redone / unknown verdict (`usage_outcomes.jsonl`) | G3 ledger completeness (enables NS and D3, which are uncomputable without it) | G3: the four new keys present on 100% of proxy rows written after deploy (before: `tier_retry` on 57 of 11,968 rows, 0.5%; no policy version; no pre-override tier). NS and D3: from "not computable" to computable. No change to routing behaviour. | G1 proxy decision latency (a cached tag lookup per request (one small file read while a session is untagged) plus one hash at start-up); G2 (every new write is fail-open and goes through the same failopen path) | Full test suite, env-registry and ledger ratchets; synthetic-transcript tests for the used / redone / unknown definition | pending: compare ledger completeness on rows written 7 days after deploy against the 7 days before | pending |
| cli: `llm-router kpi` prints the North Star scorecard (`feat/kpi-command`, stacked on `feat/kpi-instrumentation`) | New `llm-router kpi [--days N] [--include research] [--json] [--write-weekly DIR]`: NS, O1 (est.), D1-D4, G1-G3 from the ledgers, G4 from provider_reset state; O2 and D5 from a frozen benchmark file named by the new `LLM_ROUTER_KPI_BENCHMARK_PATH`. Every value carries its n and window; unknowns print `not measurable: <reason>`, never 0. Read-only: no routing change | NS (makes it, with D1-D4 and G1-G3, readable in one place; it was uncomputable as a single number) | No change to any KPI value: a reporting surface only. Before: no command printed NS or D3; after: one command, organic sessions only | None at runtime (the command is not on any hot path; the proxy and hooks are untouched). G3 is now itself reported | Test suite incl. empty-data "not measurable" cases, exact-value cases on synthetic ledgers, env-registry and ledger ratchets | pending: run weekly (`--write-weekly`, cron/launchd example in the module docstring) and compare the scorecard against the spec's targets | pending |
