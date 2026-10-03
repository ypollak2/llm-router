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
