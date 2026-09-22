# Ultimate Adversarial Audit — index

Subject frozen at `audit/FROZEN_STATE.md`. Read that first.

## Method

Ten specialists ran INDEPENDENTLY, none seeing another's findings, none having
seen the session that wrote the code under audit. The orchestrator did not
perform discovery — it froze the subject, reconciles disagreements with fresh
evidence, attacks the surviving findings (Phase 48), and reproduces the
criticals from clean (Phase 50).

### Why the orchestrator did not audit

The 15 commits between `8c7366b` and HEAD were written by the same model
orchestrating this audit, in the immediately preceding session. That is the
correlated-judge failure this audit's own Phase 20 exists to detect: the same
model interpreting the task, writing the fix, and judging the result produces
agreement, not independence.

Any finding that exists only because the orchestrator asserted it is marked
`ORCHESTRATOR-ONLY` and carries reduced confidence by construction.

## Artifacts

| File | Phase(s) | Specialist |
|---|---|---|
| `FROZEN_STATE.md` | 0 | orchestrator |
| `00_EXECUTIVE_SUMMARY.md` | — | reconciliation |
| `01_ACTUAL_ARCHITECTURE.md` | 1 | Runtime Architect |
| `02_PRODUCT_CLAIM_MATRIX.md` | 2, 42-45 | Adversarial User |
| `03_FINDINGS.md` | — | reconciliation |
| `04_RUNTIME_TRACE.md` | 3 | Runtime Architect |
| `05_ROUTING_QUALITY_AUDIT.md` | 4-6, 38-39 | Routing Scientist |
| `06_PROVIDER_MATRIX.md` | 7-8, 11-12 | Provider Specialist |
| `07_COST_AND_QUOTA_AUDIT.md` | 13-14 | Cost Auditor |
| `08_TELEMETRY_TRUST_AUDIT.md` | 15-16, 40 | Telemetry Auditor |
| `09_GROUND_TRUTH_AUDIT.md` | 17-18, 21-24 | GT Specialist |
| `10_VERIFIER_TRUST_AUDIT.md` | 19-20 | GT Specialist |
| `11_SECURITY_AUDIT.md` | 25 | Security Red Teamer |
| `12_PRIVACY_AUDIT.md` | 26 | Privacy Auditor |
| `13_CONCURRENCY_AND_DURABILITY.md` | 27-28, 34, 37 | Reliability Engineer |
| `14_TEST_GAP_ANALYSIS.md` | 29-30 | Test Skeptic |
| `15_DEAD_DUPLICATED_SUSPICIOUS_CODE.md` | 31-32 | Historian |
| `16_CONFIGURATION_AUDIT.md` | 33 | wave 2 |
| `17_PERFORMANCE_AUDIT.md` | 35-36 | wave 2 |
| `18_GIT_HISTORY_FINDINGS.md` | 41 | Historian |
| `19_FAILURE_MODE_MATRIX.md` | 46 | reconciliation |
| `20_SIMPLICITY_AUDIT.md` | 47 | wave 2 |
| `21_INVALIDATED_FINDINGS.md` | 48 | reconciliation |
| `21_INVALIDATED_FINDINGS.md` | 48 | reconciliation |
| `22_REMEDIATION_PLAN.md` | — | reconciliation |
| `23_UNKNOWN_UNKNOWNS.md` | 49 | reconciliation (post-remediation) |
| `24_CLEAN_ROOM_REPRODUCTION.md` | 50 | orchestrator, on PUBLISHED 15.0.0 |
| `25_REMEDIATION_PLAN_2.md` | — | reconciliation |

Phases still unassigned after wave 1: 9 (fallback attribution), 10 (agentic
trajectory), 33, 35, 36, 46, 47. These go to wave 2, informed by what wave 1
finds — deliberately, so the second wave can chase what the first wave could
not see.

## Phases 48-50, completed after the v15.0.0 release

Deliberately run AFTER remediation, which changes what they can do: Phase 48
can attack the remediation as well as the findings, and Phase 50 can reproduce
against the PUBLISHED ARTIFACT rather than the tree — the audit's own
completion criterion 4, and the only form of "fixed" that reaches users.

Phase 50 found three defects that reading the repository did not, one of which
(`doctor --audit` inflating the counter it reports) invalidates figures the
remediation itself published. Phases 42-45 and Phase 50 are the two phases that
INSTALLED the product; between them they produced the finding that caused the
release and the finding that most damages its claims.

## Standing rules given to every specialist

1. Do not fix. Do not delete. Discovery only.
2. Every probe under an isolated `LLM_ROUTER_HOME`. Never write to
   `~/.llm-router` or `~/.claude`. (A probe contaminated the operator's real
   ledger on 2026-09-22; see FROZEN_STATE.md.)
3. Evidence first, classified CONFIRMED / STRONGLY SUPPORTED / DESIGN RISK /
   HYPOTHESIS / INVALIDATED.
4. Anti-vacuity: a check that passes proves nothing until shown it can fail.
5. Do not trust README, docstrings, comments, test names, green tests, or the
   prior audits in `2026-09-21/` and `2026-09-22/`. Those audits were wrong at
   least twice about their own numbers — record where they are wrong.
