# Dead and suspicious code

v14.1.0 · 2026-09-21. Method: AST-extracted 1,004 public symbols across 186
modules, grepped the tree for real call sites (excluding own file and tests),
then hand-verified the high-signal subset against entry points, decorators and
MCP registration.

**False positives were excluded deliberately** — FastAPI route handlers reached
via decorators, console-script entry points, and MCP-registered tools all look
dead to a naive scan and are not.

---

## Dead, and misleading about it

### The canonical module nobody calls — `attribution.py`
**0 production callers.** Its docstring: *"Canonical routing attribution — one
definition, consumed by every surface… Every consumer re-deciding 'does this row
count?' is the defect; this module decides once."*

It was written to fix a documented bug where two dashboards gave contradictory
numbers. Meanwhile `dashboard_data.py` — itself labelled *"Single source of truth
for dashboard data queries"* — does its own per-platform attribution without
importing it.

**Two modules each claiming to be the single source of truth; the one that
actually runs is not the one designed for the job.** This is the clearest
instance of the audit's central pattern.

### `derive_trace_id` — the feature that didn't ship
Still **0 callers** in v14.1.0. Its sibling `hash_prompt` *is* now wired (commit
`bc0c554`, groundtruth traceability). `derive_trace_id` is the actual G-025
feature — a restart-surviving composite trace ID — and nothing that emits a trace
ID adopted it. It remains in `__all__`, so a reader believes G-025 shipped a
trace-ID scheme. It shipped a hash helper.

### `is_simulated` — a guard over a column nothing writes
Three hits repo-wide: an `ALTER TABLE`, a docstring explaining its semantics, and
a filter `AND is_simulated IS NOT 1` in `get_savings_by_period()`. The single
`INSERT INTO usage` omits the column. **The filter reads as protective and
excludes nothing, ever.**

### `control_plane/api.py` — shipped and unimportable
```
>>> import llm_router.control_plane.api
ImportError: cannot import name 'audit' from 'llm_router.control_plane'
```
`control_plane/` is inside `packages = ["src/llm_router"]`; the enterprise `audit`
module it imports unconditionally is not distributed. Discovered originally
because its quarantined test was hiding the failure.

---

## Dead, benign

| Symbol | Note |
|---|---|
| `judge_cascade.should_cascade` / `should_judge_inline` | The module's own "pure decision function", exported and unit-tested, **0 production callers**. `streaming_judge.py` reimplements the comparison inline with a comment acknowledging the duplication — deliberate, but it orphans the module's centrepiece |
| `storage/service.py::migrate_config` | 0 callers **and** contains `# TODO: Define target schema (mocked here)` — dead and unfinished at once |
| `benchmark_fetcher.fetch_litellm_pricing` | A second pricing source with no consumer; live cost math uses `cost.py::BASELINE_PRICING` |
| 9 public `cost.py` functions | `format_spend_for_display`, `get_usage_summary`, `log_quota_snapshot`, `get_router_efficiency`, `get_classifier_overhead`, `get_cache_hit_stats`, `log_savings`, `refresh_baseline_pricing_from_api`, `log_quality_trend` — no callers outside their own tests. Orphaned reporting scaffolding, or built ahead of a consumer that never landed |
| 5 tested-but-unwired modules | `budget_lineage_reconciliation`, `feedback_handler`, `hook_deadlock_checker`, `oauth_token_rotation`, `service_manager` — each has a dedicated test file and zero production callers |
| `context_signal.py` | Docstring says outright **"NOT the one in production"** (superseded by `operational_signal.py`). A live decoy for anyone reading by filename |

---

## Silent failure paths that matter

Of 66 `except Exception` blocks in `router.py`, most carry an explanatory
`# noqa: BLE001 — <reason>` and are genuine fail-open by design. Two are not:

1. **`router.py:2043` — the North Star ledger emit.** `except Exception:
   log.debug(...)`, return value discarded. Its sibling `_emit_ledger_attempt`
   (`router.py:1821`) counts losses via `failopen.record` after a documented
   incident: *"66 dropped events across 2400 writes produced no error, no log and
   no counter."* **The fix was applied to the execution ledger and not to the
   measurement ledger** — the one Ground Truth depends on.
2. **`router.py:3978` — the bandit reorder.** Silent, uncounted. If the bandit
   breaks (corrupt store), routing degrades to a static chain forever with no
   signal, and self-improvement quietly stops.

Plus `scripts/groundtruth/accumulate_report.py::_runtime_outcomes` — two
`except Exception: return {}` with no justifying comment, so a broken read is
indistinguishable from "no data yet".

---

## Duplication, with its reasons

| Duplication | Verdict |
|---|---|
| **Four provenance mechanisms** (`synthetic`, `is_simulated`, `is_real`, `_is_test_model()`) | Consolidate. Three do not work |
| **Two prompt-hash conventions** (`trace_id.hash_prompt`, `result_cache._prompt_hash`) | Not conflated today — different subsystems, different scopes — but this is the shape CHZ-SEC-01 was written to eliminate. Decide and document, not urgent |
| **Four scrubbers** (`secret_scrubber`, `groundtruth/scrub`, `persist_redaction`, `error_sanitization`) | `groundtruth/scrub` correctly delegates; `persist_redaction` is a legitimate wrapper; **`error_sanitization` is the orphan that should have been deleted by CHZ-SEC-01** |
| **Three caches** (`result_cache`, `semantic_cache`, `prompt_cache`) | **Not duplicates.** BM25 retrieval, embedding dedup, and Anthropic `cache_control` solve three different problems despite similar names. No action |
| `attribution.py` vs `dashboard_data.py` | See above. The real one |

---

## Markers

Unusually clean: only **2 genuine TODOs** in `src/`, both in
`storage/service.py`. No HACK/XXX anywhere. The real "unfinished" signal in this
repo is not comments — it is the dead-symbol clusters above.

---

## The pattern

Almost nothing here is careless. The recurring shape is:

> a correct, well-documented primitive is built to fix a specific incident —
> and is then not adopted by the consumers that caused the incident.

`attribution.py` (0 consumers), `is_evaluable` (1), `sqlite_wal.enable_wal` (3 of
9 sites), `failopen.record` (on one ledger, not the measurement one),
`error_sanitization` (orphaned instead of deleted).

**The remediation is rarely "write the fix". It is "finish adopting the fix that
already exists".**
