# Metrics trust audit — 2026-09-22

For each published number: definition, source, numerator, denominator,
exclusions, failure behaviour, and whether it can be reproduced.

The governing question is not "is this number right?" but **"can it be wrong in a
way nothing would notice?"**

---

## `llm-router demo` savings

| | |
|---|---|
| **Claimed** | what routing saves versus always using the premium model |
| **Numerator** | `total_opus - total_routed` |
| **Denominator** | `total_opus`, computed as `0.015 × row_count` — **a constant unrelated to any row** |
| **Exclusions** | none |
| **Failure behaviour** | prints a negative number in green, labelled "cheaper" |
| **Reproduced** | `Always-Opus $0.0450 / Smart Routing $0.09001 / Savings $-0.0450 (-100% cheaper)` |
| **Trust** | **NONE** |

The baseline is fabricated. Because one example row *is* a $0.075 Opus call, the
"routed" total exceeds the "baseline" total, so the arithmetic is not merely
imprecise — it has the wrong sign and the wrong sign is presented as a win.

---

## `get_savings_by_period` (the `llm-router status` figure)

| | |
|---|---|
| **Numerator** | `Σ saved_usd` over `usage` rows |
| **Denominator** | rows with `success = 1 AND is_simulated = 0` |
| **Exclusions** | synthetic rows (stamped at write), pre-cutover rows (NULL) |
| **Failure behaviour** | provenance lookup failure → row marked synthetic (fails closed) |
| **Reproduced** | 10 rows on disk, 5 counted; synthetic run → $0.00 |
| **Trust** | **MEDIUM** |

Correct as far as it goes. Two caveats: `calls` is incremented before subscription
rows are skipped, so it has a different denominator from the dollars beside it
(T-26); and the cutover silently removed all pre-upgrade history from `all_time`
with no in-product disclosure (T-21).

---

## `get_team_savings` — broadcast to Slack/Discord

| | |
|---|---|
| **Denominator** | `usage` rows, **no provenance clause** |
| **Reproduced** | one synthetic row → `get_savings_by_period` $0.00, `get_team_savings` **$3.00** |
| **Trust** | **NONE** |

The one surface that publishes to other people is the one with no filter.

---

## `get_realized_savings` / `get_lifetime_savings_summary`

| | |
|---|---|
| **Source** | `claude_usage`, `codex_usage`, `gemini_usage`, `savings_stats` |
| **Exclusions** | **impossible — no provenance column exists** (`PRAGMA table_info`) |
| **Guard** | only "is this a pytest process writing to the real DB"; blind to `LLM_ROUTER_SYNTHETIC=1` and to benchmark sandboxes |
| **Reproduced** | 3 synthetic `log_claude_usage` calls → `gross_saved_usd 0.24`, fully counted |
| **Trust** | **NONE** |

Surfaced through the `llm_router_admin` / `llm_router_status` MCP tools.

---

## `get_quality_report`, `get_routing_savings_vs_sonnet`, `get_router_efficiency`

| | |
|---|---|
| **Source** | `routing_decisions` |
| **Exclusions** | **none.** `cost.py:622` claims *"All downstream analytics queries use WHERE is_real = 1"*; `is_real` appears in **no** WHERE clause anywhere |
| **Trust** | **NONE** |

A provenance-aware reader (`attribution.py`) exists in the same repository and is
not used by these three.

---

## Daily / monthly spend (gates real budget caps)

| | |
|---|---|
| **Exclusions** | none |
| **Reproduced** | one $25 synthetic row → `get_daily_spend()` and `get_monthly_spend()` both return $25.00 |
| **Trust** | **LOW** |

Fails in the opposite direction from the others: a benchmark run can trip a real
cap and throttle legitimate routing.

---

## Routing success / escalation rate (`summarize()`)

| | |
|---|---|
| **Numerator** | rows by `route_outcome` |
| **Denominator** | evaluable rows only; `excluded_unevaluable_rows` reported |
| **Failure behaviour** | unknown provenance excluded, not assumed real |
| **Trust** | **MEDIUM — with a known hole** |

The arithmetic is now honest. The population is not complete: the
idempotency-dedupe and exhaustion-floor terminals write **no row at all** (T-08),
so served turns are undercounted, and the floor — the case these fields exist to
measure — is entirely invisible.

---

## Fail-open counts

| | |
|---|---|
| **Numerator** | events at 58 `record()` sites |
| **Readers in production** | **0** |
| **Failure behaviour** | store unwritable → swallowed; fallback is `debug` under a `WARNING` logger |
| **Trust** | **NONE** |

Not wrong — unobservable. The counter fails in exactly the conditions that
generate things to count.

---

## Routing overhead

| | |
|---|---|
| **Source** | `auto-route-debug.log`, 40,775 lines |
| **Denominator** | n=1536, after excluding 3179 of 4715 pairs (**67.4%** test/unknown traffic) |
| **Measured** | p50 0.0s · p90 5.0s · **p95 43.0s** · p99 55.0s · max 136s |
| **Trust** | **MEDIUM** |

p50 reads 0.0s because the log has 1-second resolution — a floor artefact, not
sub-second latency. The p95 of 43s against an advertised ~4s timeout is the real
signal, and it corroborates `doctor`'s own live warning.

---

## README savings claims

"60–80%", "35–80%", "87%" — **self-disclosed as single-user anecdotes** at
`README.md:394` ("read as anecdotes rather than as a range you can expect").
Correctly hedged. UNVERIFIABLE, and honest about it.

---

## Summary

| Metric | Trust | Failure mode |
|---|---|---|
| `demo` savings | **NONE** | fabricated baseline, wrong sign shown as a win |
| `get_team_savings` | **NONE** | no provenance, broadcast publicly |
| realized / lifetime savings | **NONE** | no provenance column exists |
| quality report / efficiency | **NONE** | a false comment claims a filter that does not exist |
| fail-open counts | **NONE** | no reader; loses its own losses |
| daily/monthly spend | **LOW** | synthetic inflates a real cap |
| `get_savings_by_period` | **MEDIUM** | correct; `calls` denominator differs |
| `summarize()` rates | **MEDIUM** | honest arithmetic, incomplete population |
| routing overhead | **MEDIUM** | resolution floor; n and exclusions stated |
| README figures | **UNVERIFIABLE** | disclosed as anecdote |

**One sentence:** yesterday's provenance work made one reader honest out of six,
and the five that were left are the ones that publish, gate spending, and feed
the MCP status tool.
