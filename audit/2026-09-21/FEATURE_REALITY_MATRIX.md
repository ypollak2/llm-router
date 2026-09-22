# Feature reality matrix

v14.1.0 · 2026-09-21. Every row was executed or traced, not read about.

**Legend**
`REAL` — works as documented, verified by execution.
`PARTIAL` — works under conditions the documentation does not state.
`BROKEN` — documented and does not work.
`DECORATIVE` — the code runs, but its output changes nothing.
`ABSENT` — documented and does not exist.

---

## Routing

| Feature | Status | Evidence |
|---|---|---|
| Prompt classification → tier → model chain | **REAL** | Traced end to end through `route_and_call`; 5 production entry points reach it |
| Fallback chain on provider error | **REAL** | 19.3% of real rows show `chosen_model ≠ final_model`; 99% carry an explaining reason |
| Fallback *attribution* | **PARTIAL** | 34 rows diverge with `fallback_occurred=False` and `fallback_reason=None` — no persisted explanation (M-03) |
| Local-first preference | **REAL** | Measurable in the ledger |
| Bandit reorder | **PARTIAL** | On by default and functioning, but fails silently and uncounted (`router.py:3978`). A corrupt store degrades routing to a static chain forever with no signal (L-07) |
| Profile resolution | **PARTIAL** | `_resolve_profile()` is called, but **only `complexity` is consumed** downstream |
| `classification_method` recorded | **DECORATIVE** | Computed, written to a key nobody reads. 0 of 23,773 rows populated (H-04) |

## Hooks and enforcement

| Feature | Status | Evidence |
|---|---|---|
| UserPromptSubmit classification hook | **REAL** | Fires and emits a decision |
| Hook → MCP tool handoff | **PARTIAL** | Hooks **never call the router**. The sidecar bridging classification to the MCP tool has a 120s TTL and fails silently |
| PreToolUse enforcement (`smart`/`hard`) | **REAL, with a documented ceiling** | Gates tool calls. Cannot intercept a prose-only answer — already documented honestly in the global rules |
| Terminal-outcome logging invariant | **REAL** | The CLAUDE.md invariant holds; the test that guards it is present |

## Gateway

| Feature | Status | Evidence |
|---|---|---|
| `/v1/chat/completions` OpenAI shape | **PARTIAL** | Returns a valid response envelope |
| Tool / function calling | **BROKEN** | `tools` is discarded by the Pydantic model before the handler body runs. Clients get prose instead of a tool call, with no error (H-03) |
| `finish_reason` | **BROKEN** | Hardcoded `"stop"`. A client can never observe `tool_use` |
| Multi-turn message fidelity | **PARTIAL** | `_flatten()` collapses `messages` to a single text blob |
| Per-request authentication | **ABSENT** | One grep hit: a comment acknowledging the gap. `commands/sse.py` requires Bearer — the pattern exists and was not applied (M-08) |
| Public-bind refusal | **REAL** | `net_bind.refuse_public_bind_or_exit` wired; defaults to `127.0.0.1` |

## Caching

| Feature | Status | Evidence |
|---|---|---|
| `result_cache` (BM25 retrieval) | **REAL** | Plus reference-quality hygiene: `secure_delete=ON`, VACUUM after purge, 0600 on db + WAL sidecars |
| `result_cache` concurrency | **PARTIAL** | 2/12 cold starts `database is locked` — `busy_timeout` is set *after* WAL, the opposite of this project's own documented fix (M-06) |
| `semantic_cache` correctness | **BROKEN** | At the shipped 0.95 threshold, "retry 3"/"retry 30" = 0.9925 cosine. Serves the wrong answer with the model never called (C-03) |
| Per-request cache bypass | **ABSENT** | No mechanism to skip the cache or evict one poisoned entry |
| `prompt_cache` (Anthropic `cache_control`) | **REAL** | A genuinely different mechanism despite the similar name |

## Cost and savings

| Feature | Status | Evidence |
|---|---|---|
| Per-call cost computation | **REAL** | From `cost.py::BASELINE_PRICING` |
| Session-end savings panel | **BROKEN** | Reports **+$83.49** where the fixture-free truth is **−$1.15** (C-02) |
| `_is_test_model()` filter | **DECORATIVE — worse, anti-protective** | Applying it moves the figure *away* from truth (+$83.49 → +$87.96). Cannot see 1,813 stub rows wearing real model names |
| `is_simulated` filter in `get_savings_by_period()` | **DECORATIVE** | The column is never written by the single `INSERT INTO usage`. Excludes nothing, ever |
| `llm-router status` | **BROKEN on a clean install** | `ModuleNotFoundError: rich`. The flagship savings command. Flagged three weeks ago (M-11) |
| `benchmark_fetcher.fetch_litellm_pricing` | **DECORATIVE** | A second pricing source with no consumer |
| 9 public `cost.py` reporting functions | **DECORATIVE** | No callers outside their own tests (L-03) |

## Quality measurement

| Feature | Status | Evidence |
|---|---|---|
| Routing-quality ledger (write) | **PARTIAL** | Writes successes only. 0 of 16,869 rows with `route_succeeded=False` (C-01) |
| Routing-quality ledger (failure path) | **ABSENT** | `record_route()` has one call site, inside `_finalize_successful_route` |
| Cache-hit representation in the ledger | **ABSENT** | Excluded by the gate at `router.py:1958` |
| `summarize()` provenance filtering | **DECORATIVE** | 0 references to `synthetic`/`is_evaluable`. One synthetic row → `quality_escalation_rate: 1.0` (H-01) |
| `attribution.py` | **DECORATIVE** | "Canonical… consumed by every surface" — 0 production callers |
| Verification of completion routes | **ABSENT** | `verification_attempted` false on 0 of 23,323 — 97% of traffic (H-05) |
| Silent-loss counting on the ledger emit | **ABSENT** | `router.py:2043` discards the return value; the fix exists on the sibling at `:1821` (H-09) |

## Ground Truth

| Feature | Status | Evidence |
|---|---|---|
| capture → scrub → eligibility → envelope → pool | **REAL** | Verified end to end this session. `capture()` called from `router.py:1989` |
| Scrub-at-write, fail-closed | **REAL** | Delegates to the canonical scrubber |
| Three-state PASS/FAIL/AMBIGUOUS | **REAL** | Never collapsed; label withheld when an ambiguous cell sits below the cheapest pass |
| HARD/SOFT split at the label | **REAL** | `label.py:81-84` never emits `cheapest_acceptable_model` for a subjective task |
| HARD/SOFT split at `discriminate.policy_score` | **PARTIAL** | Zero verification-type filtering. Harmless today only because `generate_snippet()` has no judge branch — an accidental barrier, not a designed one |
| Mutation validation | **REAL** | Mutates the target with an independent hand-authored library; genuinely discriminates |
| Verifier authoring "assistant" | **PARTIAL** | It is a template engine, not a generative assistant. The docstring oversells it — though the absence of a generative step is itself the subsystem's strongest property |
| Envelope → **replay** | **ABSENT** | 0 `git checkout`/`git apply`/worktree call sites. The gate admits EDIT tasks the harness cannot grade (H-08) |
| Human approval gate | **PARTIAL** | `actor == "assistant"` string check; any other value passes (H-10) |
| `Pool.admit()` under concurrency | **BROKEN** | 19 recorded where 21 occurred (H-07) |
| `completeness()` vs `reconstructable` | **BROKEN** | Disagree on the same object; masked by a redundant correct check elsewhere (M-01) |
| `detect_synthetic()` coverage | **PARTIAL** | Two env signals only; no `bench_*.py` sets the flag (M-02) |
| Production candidate pool | **EMPTY** | Accumulation enabled this session; no row yet |

## Privacy and secrets

| Feature | Status | Evidence |
|---|---|---|
| Canonical scrubber on `result_cache`, `semantic_cache`, `session_store`, `context`, `envelope`, `prompt_capture` | **REAL** | All route through `persist_redact()`/`scrub_text()` |
| Scrubber on `trace.py` | **ABSENT** | 0 references. Six injected secret types survived in full plaintext (C-04) |
| Scrubber on `tool_intercept.py` | **ABSENT** | 0 references. `intercepts.jsonl` mode 644, 177 live rows, no TTL |
| `error_sanitization.py` | **DECORATIVE and dangerous** | 0 callers; misses Anthropic/OpenAI/GitHub/JWT/PEM. Orphaned rather than deleted by the unification it claims (M-07) |
| structlog field redaction | **REAL** | Wired as the *first* processor, so it applies before any renderer |
| No `shell=True` in production | **REAL** | A prior injection primitive in `tools/local_task.py` was found and fixed 2026-09-14 |

## Storage and state

| Feature | Status | Evidence |
|---|---|---|
| JSONL append atomicity | **REAL** | 2,000 concurrent appends, zero corruption |
| Truncated-file survival | **REAL** | All three readers handle a mid-write kill |
| `budget_backend` cross-process safety | **REAL** | The correct pattern; the reference for the broken stores |
| `quota_tracker` concurrency | **BROKEN** | 32–38% read failure under load; 10 hooks consume it (H-06) |
| `usage.db` migrations | **PARTIAL** | 5/12 cold starts swallowed a migration failure without trace (M-05) |
| `LLM_ROUTER_HOME` redirection | **PARTIAL** | 120 sites compose `~/.llm-router` directly; five modules honour five different override variables (M-04) |
| `~/.llm-router` as "router state" | **MISLEADING** | 5.0 GB, of which 4.8 GB is a vendored RouterArena checkout (M-09) |

## Packaging and docs

| Feature | Status | Evidence |
|---|---|---|
| PyPI + npm publication of v14.1.0 | **REAL** | Both live; CI green |
| `llm_router.control_plane.api` import | **BROKEN** | `ImportError: cannot import name 'audit'`. Shipped in the wheel; the module it needs is not (M-10) |
| `llm-router health`, `llm-router gain` | **ABSENT** | Documented, do not exist |
| Two documented host integrations | **BROKEN** | Rejected by the installer |
| README "105 prompts / 76% / 72%" | **BROKEN** | Spliced from three cells of `docs/MEASUREMENT.md`; no run produced it (H-02) |
| `docs/MEASUREMENT.md` source table | **REAL** | Honest, with N and conditions. It is the summary above it that is not |
| `env_registry.py` | **REAL** | Hand-maintained, with a non-circular validation test |
| `derive_trace_id` (G-025) | **DECORATIVE** | 0 callers, still in `__all__`. The hash helper shipped; the trace-ID scheme did not (L-01) |

---

## Count

| Status | Rows |
|---|---|
| REAL | 26 |
| PARTIAL | 17 |
| BROKEN | 13 |
| DECORATIVE | 9 |
| ABSENT | 9 |

**The concentration matters more than the totals.** `REAL` clusters almost
entirely in the execution half — routing, fallback, storage atomicity, scrubbing
where adopted. `BROKEN`, `DECORATIVE` and `ABSENT` cluster almost entirely in the
measurement half — savings, quality ledger, provenance, replay, published claims.

The router works. The instruments pointed at it do not.
