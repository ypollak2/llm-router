# Dead and suspicious code — 2026-09-22

Method: AST census across `src` + `scripts`, plus call-site analysis. False
positives (decorator-reached routes, console entry points, MCP registration)
excluded by hand.

---

## Write-only abstractions

### `failopen` — 58 writers, 0 production readers
The module exists to make silent degradation visible after an incident where
"66 dropped events produced no error, no log and no counter". It is imported by
`router`, `cost`, `execution_ledger`, `dashboard_data`, `grounding`,
`observability/summary`, `hooks/auto-route`, `savings_logger`,
`quota_envelope_routing` — **every one of them to write**.

`snapshot()` has **0 call sites in `src/`** and 19 in `tests/`. No CLI, dashboard
or summary surfaces the counts.

Probed: with the store unwritable — the condition most likely to *cause*
fail-opens — `record()` swallows its own write (`failopen.py:112`) and the only
fallback is `structlog.debug` beneath a `WARNING` logger. The event is recorded
nowhere and printed nowhere.

**The counter fails in exactly the circumstances that generate things to count.**

### `is_real` — a column with a false comment
`cost.py:622` states *"All downstream analytics queries use `WHERE is_real = 1`
to filter them out."* `is_real` is written once by the migration and appears in
**no WHERE clause anywhere in the codebase**. Three analytics functions read
`routing_decisions` completely unfiltered.

A correct provenance-aware reader (`attribution.py`) exists in the same repo and
none of them use it.

---

## Structurally unreachable

### The Ground Truth verifier pipeline
`propose.py` → `mutants.py` → `verifier_registry.py` cannot contribute a label:
pool candidates are `gtc-<hash>`, frozen tasks are `gt-<seq>`, and
`run_matrix`'s only bridge matches on task id. See T-06.

An ACTIVE, human-approved, mutation-validated verifier grades nothing, silently.

### `sampling.py`
Explicitly "ready now, deliberately unused" per its own docstring. Nothing
imports it outside its tests. Honest, and listed for completeness rather than as
a defect.

---

## Shipped but broken

| Module | Failure |
|---|---|
| `commands/profile.py` | `ImportError: cannot import name 'PROFILE_PATH'` — 100% broken, reachable, undocumented |
| `commands/dev_refresh.py` | shells out to `llm_router-install-hooks`; the registered script is `llm-router-install-hooks`. Deterministic `FileNotFoundError` |
| `budget_lineage_reconciliation.reconcile_budget_lineage_audited` | function-level `from llm_router.control_plane import audit`; raises on every installed call. Not excluded from the wheel, not in `NOT_SHIPPED`, and `conftest.py` auto-skips the one test that would catch it |
| `commands/sse.py` → `main_sse_secured` | imports `llm_router.enterprise.*`, which does not exist in the repository. Fails closed, but `gateway.py` cites it as the precedent for its own auth design |

---

## Duplicate implementations, with their reasons

| Responsibility | Copies | Verdict |
|---|---|---|
| **Secret-pattern tables** | **7** | 2 are legitimately-scoped *detectors* (`org_policy`, `signals/pii`) that flag and never persist. 2 are documented deliberate fallbacks (`session_store` belt-and-braces, `hooks/auto-route` for early boot) — but the auto-route one's "kept in sync" claim is **false**. **2 are undocumented drifted scrubbers at live call sites** (`library/store`, `hooks/agent-route`) — see T-04 |
| **Classifiers** | 13 files define a `classify` | The hook-vs-router divergence is measured (59.7%, n=750), documented and parked with reasoning. Not hidden |
| **Pricing sources** | 8 files reference a rate table | `pricing.py` is the genuine single source; the rest read it. Lint passes |
| **Telemetry write surfaces** | 31 | Four distinct stores with different schemas and different provenance schemes |
| **Persistence stores** | 52 under `paths.state_path` | Large surface; no single inventory exists in code |

---

## Silent failure paths

AST census of `src` + `scripts`:

```
broad `except Exception`            1046
…with a bare `pass` body             276
    of those, wrapping a MUTATION     84
    mutation + telemetry              12
    telemetry only                    49
    other                            131
```

**84 sites where a write can fail and report nothing**, including `attempt_log`,
`session_store` (×3), `receipt_store`, `session_spend` (×2), `budget` (×2) — all
persistence stores.

This is the structure that hid a real `NameError` at `server.py:115`: the block
sat inside `except Exception: pass`, so startup silently skipped its cleanup on
every boot. **Found by ruff. Invisible to 723 test files.**

---

## Markers

Unusually clean: 9 TODO, 2 FIXME, 1 XXX, 1 "temporary" across 571 files. 171
"legacy" mentions, nearly all in explanatory comments about why a compatibility
path exists.

Three files cannot be parsed by the project's own Python (f-string backslash,
invalid before 3.12): `scripts/gen_cast.py`, `scripts/dev/gen_cast.py`,
`scripts/bench_session_replay.py`. They cannot run.

---

## The pattern

The previous audit's formulation was: *a correct primitive is built to fix an
incident and is then not adopted by the consumers that caused the incident.*

This audit finds the same thing, plus a second-order version of it: **the
mechanisms built to detect non-adoption are themselves unadopted.** `failopen`
has no reader. `is_real` has no filter. 84 swallows have no counter. The
verifier registry has no bridge.

Every one of them would have caught something in this report.
