# Domain 07 — Providers, caches, performance, async/concurrency

Auditor: 07/12. Baseline: worktree `<worktree>`,
detached at `3c96d23`. All file:line references are against that commit unless noted.
Note on process: this run was interrupted mid-task by a harness plan-mode activation
(no writes made during the interruption) and resumed per coordinator instruction; the
worktree's `CLAUDE.md` is git-ignored and was read from the sibling checkout at
`<repo>/CLAUDE.md` as instructed (read-only, its
measurement-methodology rules applied throughout — see especially the WAL/busy_timeout
and "unknown-as-favourable-answer" lessons, both directly relevant to findings below).
The one live-DB query below was run against a **copy** of `~/.llm-router/usage.db`
(`cp` to the session scratchpad before `sqlite3`), never the original.

## Overview

The provider layer is materially better-architected than the brief's default
assumption of "boilerplate duplicated per provider." There is exactly **one**
wire-protocol adapter (`providers.py`, wrapping `litellm.acompletion`), a real
single-source-of-truth for pricing enforced by a CI lint, and a small
Protocol-based quirks registry instead of inline per-provider branching. That is
the headline finding, and it is a KEEP / do-not-change item, not a defect.

The genuine problems are elsewhere, and both are concrete and evidence-backed:

1. **SQLite concurrency hygiene is inconsistent across writers to the shared
   `usage.db`.** One path (`session_spend.py`) writes with a `busy_timeout` 15x
   shorter than the codebase's own documented standard, wrapped in a bare
   `except Exception: pass` — a silent, contention-triggered loss of a savings
   row on the exact ledger the repo has invested the most verification effort
   in (see the baseline commit's savings-verification work).
2. **A dead cache implementation sits beside a live one under confusingly
   similar names** — `cache/store.py`'s `SemanticCache` is a permanent no-op
   stub, publicly exported, with zero callers, while the real semantic
   dedup cache (`semantic_cache.py`, top-level module, different name
   pattern) is fully implemented, wired into the router, and — importantly —
   the historically-reported cross-passport leak in that real cache has been
   fixed and is now regression-tested (verified below, not just claimed).

Six sites of blocking I/O inside `async def` functions were found by AST scan
(event-loop-blocking, §22); severity varies by call frequency and is noted
per-site. Hook startup overhead (§23) was measured directly: ~120–170 ms
wall-clock for the fast/no-op path, almost entirely import + init cost, not
Python startup (~10 ms baseline measured separately).

---

## §18 — Provider matrix

| Provider (via litellm prefix) | Adapter | Request/response conv. | Streaming | Tools/structured output | Media | Timeout | Retry | Error normalization | Model discovery | Pricing | Credentials | Quirk registered? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| anthropic/ | `providers.py` → litellm | litellm | `call_llm_stream_events` | via litellm passthrough (not adapter-specific code here) | not in this module | `config.request_timeout` | litellm's own | `_normalise_finish_reason` (R15) | `discover.py` | `pricing.py` (sole source) | env/config, litellm reads | `AnthropicPxpipeQuirk` (opt-in pxpipe local proxy) |
| openai/ (incl. o-series) | same | same | same | same | same | same | same | same | same | same | same | `OpenAIReasoningQuirks` (forces temperature=1 for o1/o3/o4) |
| ollama/ | same | same | same, `max_tokens` dropped | same | same | same | same | same | same | same | none (local) | `OllamaQuirks` (drops max_tokens — litellm/Ollama transport bug) |
| openrouter/ | same | same | same | same | same | same | same | same | same | same | same | `OpenRouterQuirks` (re-prepends `anthropic/`, caps max_tokens at 2048) |
| openai_compat/ | same | same | same | same | same | same | same | same | same | same | `openai_compat_base_url` config | `OpenAICompatQuirks` (rewrites to `openai/`, injects `api_base`) |
| google/gemini, codex, perplexity, groq, deepseek, xai, azure | same | same | same | same | same | same | same | same | same | same | same | none registered → `IdentityQuirk` (zero-cost no-op) |

**Single adapter, not per-provider boilerplate.** `src/llm_router/providers.py:44,137-297,330-454`
is the *only* place a wire-format request is built and a response is parsed; every
provider above shares it via `litellm.acompletion`. Provider-specific behavior is
isolated behind the `ProviderQuirk` Protocol (`provider_quirks.py:52-67`), looked up
once per call by provider prefix (`providers.py:226-230,398-401`) and defaulting to a
zero-cost `IdentityQuirk` (`provider_quirks.py:73-88,315-322`) for the ~10 providers
with no registered quirk. This is a genuinely good design: adding a provider that
litellm already speaks does not require touching the adapter at all.

**The real constraint this creates**: the whole provider layer's reach is bounded by
what `litellm.acompletion` supports. There is no fallback raw-HTTP adapter path in
this codebase. A provider litellm does not support cannot be added cheaply under this
architecture — the "add a provider" cost is bimodal (cheap if litellm covers it,
unbounded if not), not a smooth function of effort.

### Boilerplate duplication found

- Ollama's `max_tokens`-drop workaround is implemented **twice**: once inline in
  `call_llm` (`providers.py:206-216`) and once as the identical branch in
  `call_llm_stream_events` (`providers.py:389-390`), in addition to the same logic
  living a third time in `OllamaQuirks.transform_request` (`provider_quirks.py:136-144`)
  — which is registered and *also* invoked at both call sites
  (`providers.py:228-230,399-401`). The quirk-registry version and the inline version
  do overlapping work (inline drops `max_tokens` unconditionally by never adding it for
  `ollama/` models; the quirk removes it if present) — functionally redundant, not
  contradictory, but it is the "next inline patch" the quirk registry's own docstring
  (`provider_quirks.py:1-28`) says it exists to prevent, already recurring inside the
  file that introduced it.

### Stale/duplicated model ids (§18, §73)

- `model_registry.py:271-354` (`_BUNDLED_DEFAULTS`, used only when
  `config/models.yaml` is absent — `model_registry.py:105-120`) contains
  `anthropic/claude-3.5-sonnet` (`model_registry.py:310`) and `openai/gpt-4o`
  (`model_registry.py:304`) as the MID-tier fallback entries a config-less install
  would route to. The module's own comments (`model_registry.py:279-284`) show the
  team already caught and fixed one instance of this exact defect (the Haiku entry
  used to be `claude-3.5-haiku`) but the Sonnet entry in the same table was not
  updated to match. Low real-world impact (this table only activates when
  `config/models.yaml` is missing, which does not happen in a normal install), but it
  is the same class of bug the file's own history warns about.
- `config/models.yaml` (`snapshot_date: 2026-07-10`) is 76 days old against a
  90-day CI staleness gate (`scripts/check_model_registry_freshness.py:37`,
  `MAX_SNAPSHOT_AGE_DAYS = 90`) — not yet failing, but will fail within ~2 weeks of
  this audit if not refreshed. Observational, not a current defect.

---

## §73 — Provider/model/pricing/capability source-of-truth audit

| Concept | Canonical file | Enforcement | Secondary readers that could drift |
|---|---|---|---|
| Price per model | `pricing.py` | `scripts/lint_pricing.py` fails CI on any price literal outside this module (`pricing.py:1-33` docstring; found and fixed the same $15/$75 stale-Opus bug **five times** historically per that docstring) | `model_registry.py` reads via `_pricing.price_for()` (`model_registry.py:36,257`), never hardcodes — verified clean |
| "Latest" version alias (`claude-opus:latest` etc.) | `model_aliases.py` (`LATEST_CLAUDE` dict, lines 19-23) | No dependents on `llm_router` package (deliberately, to avoid circular imports — line 3-5); consumed by `policy._parse_chains` | none found duplicating it |
| Routing chains (profile × task_type → model list) | `policies/standard.yaml`, hydrated into `profiles.ROUTING_TABLE` at import time (`profiles.py:60-115`) | `tests/test_standard_policy_mirror.py` (per `profiles.py:113`); a prior hardcoded-dict version was deliberately deleted (`profiles.py:117-119`) | none — single YAML |
| Model quality/pricing/capability metadata | `config/models.yaml` → `model_registry.ModelRegistry` | `scripts/check_model_registry_freshness.py` (cadence only, not correctness) | `_BUNDLED_DEFAULTS` fallback (see stale-id finding above) |
| Capability detection (needs-tools?, file/context relevance) | `capabilities.py` (explicitly self-described as "the single shared source of truth", line 1-6) | consumed by `chain_builder.needs_claude_tools()` via legacy-boolean shim (line 17-19) | none found |
| Provider enable/disable (ops kill-switch) | `provider_registry.py` `RuntimeProviderRegistry` | version-counter polling across processes (lines 26-29, 166-174) | `admin_actions.py`, `chain_builder.py` (per module docstring, lines 4-9) — **note**: this is NOT a provider/model metadata registry despite the name; it is an orthogonal disable/enable store. Its name is easy to confuse with `model_registry.py` |
| "Which models are cheap/free" for reordering | **THREE separate hardcoded sources**: `profiles._CLAUDE_CHEAP_MODELS` (line 33-36), `profiles._CHEAP_MODELS` (line 52-58), `profiles._FREE_EXTERNAL_MODELS` (line 39-46) | none — plain module-level frozensets, hand-maintained, with inline comments noting when an entry became stale (e.g. line 56: "aliases deprecate 2026-07-24") | `commands/routing.py:46,142,156` reads `_FREE_EXTERNAL_MODELS` directly; the other two are internal to `profiles.py` reordering logic (lines 389-432) — no drift detected today, but this is hand-maintained truth #4 alongside `pricing.py`, `model_registry.py`/`config/models.yaml`, and `tiers.py`'s prefix table, for a question ("is this model cheap/free?") that has four independent hand-edited answers |
| Cost *tier* for the savings dashboard (free_local / free_subscription / paid_api) | `tiers.py` `_TIER_PREFIXES` (lines 35+) | none found | separate concept from `lineage.Tier` (LOCAL/CHEAP/MID/PREMIUM, used by `model_registry.py`) and from `RoutingProfile` (BUDGET/BALANCED/PREMIUM/REASONING, used by `profiles.py`). Three different things are all called "tier" in this codebase — naming collision, not a functional bug (§40 territory, flagged here because it's adjacent to the SSOT question) |

**Bottom line on §73**: pricing has a real, enforced, single source of truth — this
part of the brief's worst-case fear (stale duplicated pricing) does **not** hold for
this codebase today. What remains scattered is the softer, unenforced classification
of "which models are cheap" across four hand-maintained tables that happen to agree
right now.

---

## §22 — Async/concurrency map

`async def` present in 96 files, `asyncio` imported in 66 (grep, this worktree).
Mechanical AST scan (see Validation) for blocking calls inside `async def` bodies
found:

| File:line | Async function | Blocking call | Assessment |
|---|---|---|---|
| `context.py:522` | `get_recent_session_summaries` | `sqlite3.connect(...)` + sync `.execute()/.fetchall()` | **Partially-fixed function.** Three lines above (line 515: `await asyncio.to_thread(db_path.exists)`) the same author explicitly offloaded a *cheap* sync call to avoid blocking the loop, then left the actually-expensive DB read as a raw blocking call right after. Comment at line 514 ("Offload synchronous Path.exists() to thread pool to avoid blocking event loop") proves awareness of the hazard, not absence of it. |
| `agentic/telemetry.py:100` | `_default_recorder` | `sqlite3.connect(str(path))`, no WAL/busy_timeout | Blocks the loop AND is a fourth uncoordinated writer to a `savings_stats`-adjacent DB (see §24/PERF-03). |
| `model_evaluator.py:113` | `eval_ollama_model` | `urllib.request.urlopen(..., timeout=OLLAMA_TIMEOUT)` | Benchmark/eval harness path, not the user-facing routing hot path — lower severity, but still blocks the loop for up to `OLLAMA_TIMEOUT` per call. |
| `tools/subscription.py:137` | `llm_refresh_claude_usage` | `urllib.request.urlopen` | MCP tool handler; blocks the server's event loop for the HTTP round-trip, delaying any other concurrent MCP request in the same process. |
| `tools/admin.py:1342` | `llm_import_profile` | `urllib.request.urlopen` | Same pattern, admin/one-off path. |
| `integrations/helicone.py:127` | `get_helicone_spend` | `urllib.request.urlopen` | Same pattern, external telemetry integration. |

No forgotten-`await` or task-leak was proven within the audit window (would need a
targeted read of every `asyncio.create_task`/`gather` call site; not exhausted —
mark UNCERTAIN rather than clean).

**Sync/async duplicate APIs**: `providers.py` exposes only async entry points
(`call_llm`, `call_llm_stream`, `call_llm_stream_events`) with no sync twin — clean.
The duplication that *does* exist is at the storage layer: `cost.py`'s async
`_get_db()`/`aiosqlite` path (used by `semantic_cache.py`, `execution_ledger.py`) vs.
several **sync** `sqlite3.connect()` call sites writing adjacent/overlapping data to
the same physical file or its siblings (`session_spend.py`, `feedback.py`,
`retrospective.py`, `dashboard_data.py`, `agentic/telemetry.py`) — not a sync/async
API duplication of the same function, but a split-brain persistence pattern for
what is conceptually one ledger (see §24).

**Shared mutable global**: `provider_registry.py:320` (`_global_registry`,
module-level singleton with lazy init) and `config.py:919` (`_config`, same pattern)
are both guarded correctly (lock in the former, single-assignment-then-reuse in the
latter) — checked, not a race.

---

## §23 — Performance: routing overhead independent of inference

**Measured** (not estimated): `hooks/auto-route.py` invoked as a subprocess with a
JSON payload carrying an empty prompt (`{"prompt":"","hook_event_name":"UserPromptSubmit","session_id":"timing-test-000","cwd":"/tmp"}`),
under `HOME=$(mktemp -d)` (clean HOME) and `LLM_ROUTER_ENFORCE=off`, no network calls
made (empty prompt short-circuits before any classification/model call):

```
run 1: real 0.17s   run 2: real 0.13s   run 3: real 0.13s   (n=3, this machine, 2026-09-24)
```

Baseline bare-interpreter startup on the same machine/venv, same n=3: `real 0.01s`
each. So the hook's own import + init cost is **~120–160 ms**, not interpreter
startup — consistent with `hooks/auto-route.py` being a single ~4,800-line script
(`wc -l` on this file was the largest single Python file found in this pass) that
imports a substantial slice of `llm_router` at module load.

**Caveat, per the worktree's own measurement rules** (`CLAUDE.md`, "Wall-clock timings
on this machine are not trustworthy"): this is `time.monotonic()`-free wall-clock via
the `time` shell builtin, n=3, on a single unloaded developer machine — sufficient to
establish an order of magnitude (hundreds of ms, not tens or thousands), not
sufficient for a precise SLA number. It also only measures the **fast-path** (empty
prompt); the Ollama/heuristic/API-fallback chain described in the module's own
docstring (`hooks/auto-route.py:5-10`) was deliberately not exercised, per this
audit's constraint against starting local models or calling paid APIs — so this
number is a *floor*, not the typical case.

**Verified non-issues** (checked, not assumed): `get_config()` is a genuine
module-level singleton (`config.py:916-921`, `global _config`) — not re-read from
disk/env on every call despite being invoked inline throughout the hot path
(`providers.py:176,228`, `provider_quirks.py:228-229`, etc.). `capabilities.py`'s
dozen-plus regexes (lines 105-288) are all compiled at module import time, not
inside the functions that use them — no recompilation-per-call found there.

---

## §24 — Cache inventory

| Cache | Key | TTL | Bound | Persistence | Concurrency | Status |
|---|---|---|---|---|---|---|
| `cache/classification.py` `ClassificationCache` | SHA-256(prompt + quality_mode + min_model) | 3600s, lazy expiry on access | `MAX_ENTRIES = 1000`, LRU via `OrderedDict` | in-memory only, per-process | `asyncio.Lock` | Live, used by `classifier.py:174`, `tools/admin.py:299,320`. Caches `ClassificationResult` (not the routing decision) *specifically* so budget pressure re-applies fresh each time (module docstring) — deliberate, correct design. |
| `cache/store.py` `SemanticCache` | n/a — `get()` always returns `None`, `put()` is a no-op | n/a | n/a | n/a | n/a | **DEAD.** Docstring calls itself "v0.0.1 scaffold" for a "v0.0.2" that never landed. Exported as public API (`cache/__init__.py:25,35`) but grep of all of `src/` and `tests/` found **zero** callers of `SemanticCache(`/`SemanticCacheEntry` anywhere outside its own definition and the `__init__.py` re-export. |
| `semantic_cache.py` (top-level, **not** in the `cache/` package — confusing given the name overlaps with the dead one above) | cosine similarity (≥0.98 default) over a Ollama `nomic-embed-text` embedding, scoped by `task_type` + `project_scope` (a hash of the repo root, `_project_scope()` line 82-105), plus a `_discriminator` veto (numeric literals + polarity-word groups, lines 233-265) | soft: 24h scan window (`_TTL_SECONDS`, line 61) for match eligibility; hard: physical purge at `LLM_ROUTER_PERSIST_TTL_DAYS` (default 30d, lines 163-176, 179-211) | `_MAX_SCAN = 200` rows per lookup (line 65) | SQLite `semantic_cache` table inside the shared `~/.llm-router/usage.db`, via `cost._get_db()`/aiosqlite (line 375,496) | inherits `usage.db`'s WAL mode from `cost.py`'s `enable_wal` call; per-request kill switch via `LLM_ROUTER_SEMANTIC_CACHE=off` (lines 288-291) | **Live**, wired into `router.py:2428` (store) and `router.py:4358` (check). |
| `result_cache.py` | SHA-256(prompt) dedup + BM25/FTS5 ranking for follow-up retrieval (this is retrieval-augmentation, not a strict cache-hit skip) | per task_type: 24h code / 3d analyze / 7d research&generate / 30d query (lines 45-51) | not found bounded by row count in the excerpt read; TTL-gated only | two SQLite DBs: user-level `~/.llm-router/result_cache.db` and project-level `~/.llm-router/projects/<hash>/result_cache.db` (module docstring lines 8-10) | uses `sqlite_wal.enable_wal` (3 call sites, confirmed via grep) — the WAL-hygiene fix **is** applied here | Live, consumed by `idempotency.py`, `context.py`, `routing_quality.py`, `lineage/lineage_store.py`, `semantic/scope.py`, `semantic/seed_lessons.py`. |
| `prompt_cache.py` | n/a — not a local cache at all | n/a (Anthropic's own 5-minute server-side cache TTL, not controlled here) | n/a | none — this module only *injects* `cache_control` breakpoints into the outgoing message list so Anthropic caches server-side | n/a | Correctly out of scope for local-cache concerns; mis-groupable with the other four by name alone (§9 semantic-duplication risk is in the *naming*, not the code). |

### Semantic-cache cross-contamination lead — re-verified

The brief's stated lead ("a cosine≥0.95 semantic cache once served one passport's
answer for another") is **fixed in this codebase, with evidence**, not merely
claimed:

- Threshold raised 0.95 → 0.98 with a measured rationale in-code
  (`semantic_cache.py:40-58`: `"retry 3 times"` vs `"retry 30 times"` scored 0.9925,
  above even 0.98, so the fix explicitly does **not** rely on the threshold alone).
- `_project_scope()` (line 82-105) prevents the originally-reported cross-*project*
  leak by scoping every row and every lookup to a hash of the repo root.
- `_discriminator`/`_equivalence_veto` (lines 233-285) is the actual fix for the
  "close vector, different meaning" failure mode described in the audit lead: it
  vetoes a cosine-similarity hit when numeric literals or polarity words
  ("increase" vs "decrease" etc.) differ between the cached and incoming prompt —
  which is the shape a passport-number collision would take. **Fails closed**: a
  cached row with no discriminator (written before this fix) is treated as
  non-equivalent, not as a match (`_equivalence_veto:273-274`).
- Regression-tested: `tests/semantic/test_c03_cache_cannot_answer_a_different_question.py`
  (166 lines, 10 test functions) includes `test_guard_is_not_vacuous` (line 151) —
  named specifically to satisfy the worktree's own "check the check" rule from
  `CLAUDE.md` (K7 lesson: a red-check that doesn't actually exercise the assertion is
  worse than no test).
- **Live-DB evidence** (copy of `~/.llm-router/usage.db`, queried read-only): the
  production `semantic_cache` table on this machine has **0 rows** and still has the
  **pre-fix schema** (no `project_scope`, no `discriminator` columns — confirmed via
  `.schema semantic_cache` on the copy). This is not a bug: the migration
  (`_ensure_project_scope_column`, lines 108-132) runs lazily inside `check()`/
  `store()`, and those have apparently never fired on this machine (consistent with
  memory of this environment needing a hand-run `ollama serve` for local embeddings
  to work at all). It does mean the fix has zero *live* exercise here to point to —
  the evidence for "fixed" is code + test, not production telemetry, on this
  machine specifically.

---

## §71 — "Add a model" / "add a provider" fitness test

**Add a model with tools + structured output, to a provider litellm already
supports** (the common case): touch `pricing.py` (required, lint-enforced) +
`config/models.yaml` (registry entry) + `policies/standard.yaml` (if it should be
reachable by the router, i.e. added to a chain) + optionally `model_aliases.py` (if
it should absorb a `:latest` alias) + tests (`test_standard_policy_mirror.py`-style,
registry validation) + `guide/PROVIDERS.md`. **Tools/structured-output support is not
a separate integration point** — it flows through litellm's existing OpenAI-style
`tools=`/`response_format=` passthrough already in `_ALLOWED_EXTRA_PARAMS`
(`providers.py:62-79`, though notably `tools` and `response_format` are **not** in
that allow-list today — see Validation Required below, this may mean tool-calling
requests are currently stripped by the extra-params filter and rely on a different,
unaudited call path). Roughly 3-4 files + tests, not the N-file sprawl a
per-provider-adapter architecture would produce.

**Add a genuinely new provider**: cheap (same file set as above, plus one
`ProviderQuirk` subclass if the provider needs one, plus a `_TIER_PREFIXES` entry in
`tiers.py` for savings classification) **if and only if litellm already speaks that
provider's wire protocol**. If it does not, the cost is unbounded — there is no
documented or coded fallback path for a provider `providers.py` cannot delegate to
litellm for.

---

## Findings register

```
ID: PERF-01
Category: Concurrency / durability
Severity: HIGH
Confidence: HIGH (direct code read + live-DB confirmation of shared file path)
Location: Files: src/llm_router/session_spend.py Symbols: _persist_to_claude_usage Lines: 311-333 (call wrapped at 307-309)
Observation: Writes a savings-attribution row into the SAME shared ~/.llm-router/usage.db
that cost.py, semantic_cache.py, and execution_ledger.py all write to via cost._get_db()
(which sets a 30,000ms busy_timeout through sqlite_wal.enable_wal). This one write path
instead opens its own bare sqlite3.connect(str(db_path), timeout=2.0) — a busy_timeout
15x shorter than the codebase's own documented standard for exactly this shared file —
and the entire call is wrapped in `except Exception: pass` (session_spend.py:307-309).
Evidence: sqlite_wal.py's own docstring documents the WAL cold-start race this repo
already got bitten by (66/2400 events silently lost with nothing logged); this call site
reproduces the same silent-loss shape for a different table under a shorter timeout.
Why this exists, if discoverable: session_spend.py predates or was written independently
of the sqlite_wal.py consolidation (RED5-01/02); the comment at line 307 calls the write
"best-effort — never crash the router," which is a reasonable policy for NOT crashing but
does not distinguish "row genuinely doesn't matter" from "row silently vanished."
Why this matters: the baseline commit at HEAD (3c96d23) is entirely about making savings
figures trustworthy ("unverified" labeling, parity-tested SQL). A silently-dropped write
under concurrent load (multiple hook/MCP/CLI processes writing usage.db at once) directly
undermines that effort in a way nothing in that commit's test suite would catch, because
the failure is contention-triggered, not deterministic.
User-visible impact: under load, some routed-turn savings rows silently never appear in
the dashboard/report; the shortfall looks like "less was saved" rather than "a write failed."
Engineering impact: none until it's debugged; the except:pass gives zero signal.
Is behavior currently used? YES — session_spend.py's record_reclaimed() calls this on
every reclaimed-token event per its own docstring context.
Recommended action: SIMPLIFY — route this write through cost._get_db()/enable_wal (or at
minimum raise timeout to sqlite_wal.DEFAULT_BUSY_TIMEOUT_MS) and log (not swallow) a
failure so it is at least observable, per the repo's own "unknown must not be the
favourable answer" rule.
Proposed target: single write path through cost.py's connection helper for every writer
touching usage.db.
Behavioral compatibility risk: LOW — same table, same semantics, only the connection
policy changes.
Security risk: none.
Performance impact: negligible (this write is already off the hot path — fire-and-forget
after a session event).
Estimated complexity removed: removes one of five distinct raw-sqlite3 write paths into
the same physical file.
Validation required: reproduce under concurrent load (N parallel writers hammering
usage.db) and confirm row-loss count drops after the fix; check for other exception
handlers in this file with the same swallow pattern.
Dependencies on other findings: PERF-03 (same class, different files).
```

```
ID: PERF-02
Category: Async/concurrency
Severity: MEDIUM
Confidence: HIGH (AST-verified: async def body contains a direct sqlite3.connect call)
Location: Files: src/llm_router/context.py Symbols: get_recent_session_summaries Lines: 515-522
Observation: Three lines after `await asyncio.to_thread(db_path.exists)` (explicitly
offloading a cheap sync call to avoid blocking the event loop), the same function opens
`sqlite3.connect(str(db_path))` and runs synchronous `.execute()`/`.fetchall()` directly
on the event loop — the actually-expensive part of the operation is not offloaded.
Evidence: context.py:514 comment ("Offload synchronous Path.exists() to thread pool to
avoid blocking event loop") proves the author knew the hazard; line 522's sqlite3.connect
is the same hazard, unaddressed, immediately after.
Why this matters: any concurrent async task in the same process (another MCP tool call,
another routing decision) stalls for the duration of the DB read whenever this function
runs — worse than doing nothing, because it signals "this was thought about" while leaving
the larger blocking call in place.
User-visible impact: latency spikes for concurrent operations sharing this event loop,
proportional to session_summaries table size and disk contention.
Engineering impact: easy, mechanical fix.
Is behavior currently used? YES — session summary retrieval for context injection.
Recommended action: SIMPLIFY — wrap the sqlite3.connect+execute in the same
asyncio.to_thread() pattern already used two lines above, or migrate to aiosqlite like
cost.py.
Proposed target: consistent to_thread or aiosqlite usage across this function.
Behavioral compatibility risk: LOW.
Security risk: none.
Performance impact: removes the larger of two blocking calls in this function.
Estimated complexity removed: n/a (bugfix, not simplification).
Validation required: none beyond a normal review; low-risk mechanical change.
Dependencies on other findings: none.
```

```
ID: PERF-03
Category: Async/concurrency + storage
Severity: MEDIUM
Confidence: HIGH (AST scan + manual read of each site)
Location: Files: src/llm_router/agentic/telemetry.py Symbols: _default_recorder Lines: 95-105 | src/llm_router/model_evaluator.py Symbols: eval_ollama_model Lines: ~95-118 | src/llm_router/tools/subscription.py Symbols: llm_refresh_claude_usage Lines: ~137 | src/llm_router/tools/admin.py Symbols: llm_import_profile Lines: ~1342 | src/llm_router/integrations/helicone.py Symbols: get_helicone_spend Lines: ~127
Observation: Five further `async def` functions perform a fully blocking call
(sqlite3.connect, or urllib.request.urlopen) directly on the event loop, found by an AST
scan of every AsyncFunctionDef body across src/llm_router for time.sleep/urlopen/
sqlite3.connect/subprocess.run.
Evidence: mechanical scan output listed under §22 above; each site manually spot-checked.
agentic/telemetry.py:100's sqlite3.connect additionally has no WAL/busy_timeout at all —
a sixth uncoordinated writer pattern alongside PERF-01.
Why this matters: severity is uneven — tools/subscription.py and tools/admin.py are MCP
tool handlers (blocks the MCP server's event loop for other concurrent tool calls during
the HTTP round-trip); model_evaluator.py and helicone.py are benchmark/telemetry paths,
lower traffic.
User-visible impact: occasional MCP-call latency stalls when one tool handler is
mid-network-call while another request arrives.
Engineering impact: mechanical asyncio.to_thread wrapping would fix all five.
Is behavior currently used? YES for all five (not proven equally hot).
Recommended action: SIMPLIFY — wrap each blocking call in asyncio.to_thread, or convert
the sqlite3.connect site in agentic/telemetry.py to the shared aiosqlite/cost.py pattern.
Proposed target: none require architectural change, all are local wraps.
Behavioral compatibility risk: LOW.
Security risk: none.
Performance impact: removes event-loop stalls proportional to per-call network/DB latency.
Estimated complexity removed: n/a.
Validation required: confirm no test currently depends on synchronous ordering these
functions incidentally provide (unlikely, but not checked for all five).
Dependencies on other findings: PERF-01 (agentic/telemetry.py shares the "no WAL, no
timeout, bare except" writer pattern).
```

```
ID: PRV-01
Category: Dead code / semantic duplication
Severity: MEDIUM
Confidence: HIGH (exhaustive grep of src/ and tests/ for all call-forms)
Location: Files: src/llm_router/cache/store.py Symbols: SemanticCache, SemanticCacheEntry Lines: 1-45 | src/llm_router/cache/__init__.py Lines: 12-15, 25, 35
Observation: cache/store.py's SemanticCache.get() unconditionally returns None and
.put()/.stats() are no-ops (module docstring: "v0.0.1 has no persistence, every lookup
misses"). It is publicly exported from cache/__init__.py's __all__. Grep of every .py file
under src/ and tests/ for `SemanticCache(` or `SemanticCacheEntry` outside its own
definition and re-export found zero call sites.
Evidence: cache/__init__.py:12-15 itself documents this as a placeholder superseded by
"v0.0.2... once sqlite-vec + sentence-transformers are wired" — that v0.0.2 never
happened here; instead a functionally equivalent, fully-implemented, differently-located
cache was built: src/llm_router/semantic_cache.py (top-level, Ollama embeddings, SQLite
persistence, wired into router.py:2428/4358). Two things doing the same conceptual job
("skip an LLM call for a semantically-equivalent prompt") exist under confusingly similar
names in different places, one dead.
Why this exists, if discoverable: classic scaffold-then-superseded pattern — the stub was
written first (v0.0.1), the real implementation (semantic_cache.py) was built later,
under a different module path, and the stub was never removed or even cross-referenced
from the real module's docstring.
Why this matters: a maintainer searching "SemanticCache" will find the dead stub first
(package-level export, matches the obvious name) and could reasonably believe the feature
doesn't exist or is unimplemented, when a fully-featured, security-hardened (project-
scoped, discriminator-vetoed) version lives one directory up under a different name.
User-visible impact: none directly (dead code doesn't run), but a real risk of a future
contributor "fixing" or "implementing" the stub, duplicating semantic_cache.py's already-
hardened logic badly.
Engineering impact: confusion cost > runtime cost.
Is behavior currently used? NO.
Recommended action: DELETE cache/store.py's SemanticCache/SemanticCacheEntry and its
__init__.py export; if a docstring pointer to the real semantic_cache.py is wanted, add
one sentence, not a stub class.
Proposed target: cache/__init__.py keeps only ClassificationCache/get_cache.
Behavioral compatibility risk: NONE — zero callers found.
Security risk: none.
Performance impact: none (dead code, no execution cost).
Estimated complexity removed: ~45 LOC + 3 lines of __init__.py exports, one whole
(fake) concept removed from the public surface.
Validation required: re-run the grep for SemanticCache/SemanticCacheEntry across the full
tree (including docs, notebooks, scripts) immediately before deleting, per the brief's
"no direct import ≠ dead" caution — this check was done against src/ and tests/ only in
this pass, not docs/scripts/architecture/.
Dependencies on other findings: none.
```

```
ID: PRV-02
Category: Positive finding / do-not-change
Severity: LOW (informational)
Confidence: HIGH
Location: Files: src/llm_router/providers.py Lines: 1-58, 137-297 | src/llm_router/provider_quirks.py Lines: 1-328 | src/llm_router/sqlite_wal.py Lines: 1-101
Observation: The provider adapter layer is a single wrapper around litellm plus a
Protocol-based quirk registry (not per-provider inline branching), and the WAL cold-start
race documented in sqlite_wal.py was fixed with measured before/after evidence (4/12
concurrent constructions failed before the fix; 66/2400 events silently lost before the
PRAGMA-return-value check was added) rather than assumed-fixed.
Why this matters: these are exactly the kind of components the brief's DELETE > MERGE >
REUSE preference is built to protect once found — they should not be "simplified" or
rewritten by a later phase of this audit's remediation plan.
Recommended action: KEEP, add to do-not-change register.
Validation required: none — evidence is in-repo and, for the WAL fix, numerically
falsifiable.
Dependencies on other findings: none.
```

```
ID: PRV-03
Category: Semantic duplication / naming
Severity: LOW
Confidence: MEDIUM (file-level read of 4 modules; not exhaustive of every "tier"/"registry" reference repo-wide)
Location: Files: src/llm_router/lineage/__init__.py (Tier: LOCAL/CHEAP/MID/PREMIUM, referenced model_registry.py:37,64) | src/llm_router/tiers.py Lines: 30-35 (Tier: free_local/free_subscription/paid_api) | src/llm_router/types.py (RoutingProfile: BUDGET/BALANCED/PREMIUM/REASONING) | src/llm_router/provider_registry.py Lines: 95 (RuntimeProviderRegistry, an enable/disable store, name-collides with "model registry" concept)
Observation: Three distinct concepts are each called "tier" in this codebase, and
"registry" names both the model-metadata lookup (model_registry.py) and an unrelated
provider-disable/enable store (provider_registry.py). No functional drift was found
between them in this pass, but the naming makes the SSOT question (§73) harder to answer
by inspection than the underlying code actually is.
Why this matters: this is squarely §40 (naming) territory but surfaced here because it
directly affects how confidently a maintainer can answer "where is the model/provider
truth" — the brief's own §73 test.
Recommended action: KEEP the concepts (they answer genuinely different questions), RENAME
for clarity is a candidate for whichever auditor owns §40/naming; flagging here so it
isn't lost.
Validation required: confirm with the naming-domain auditor whether this is already
covered to avoid duplicate reporting.
Dependencies on other findings: none.
```

```
ID: PRV-04
Category: Stale data / dead fallback path
Severity: LOW
Confidence: HIGH (direct read)
Location: Files: src/llm_router/model_registry.py Lines: 271-354 (_BUNDLED_DEFAULTS), 105-120 (load_default)
Observation: The hardcoded fallback catalogue used only when config/models.yaml is
absent still lists anthropic/claude-3.5-sonnet and openai/gpt-4o as its MID tier, while
the same table's own comments (lines 279-284, 336-340) show the team already found and
fixed the identical defect for the Haiku and Opus entries in this exact table.
Why this matters: low current impact (requires config/models.yaml to be missing, which
does not happen in a packaged install), but it is a live instance of the exact bug class
the surrounding comments warn about, in the same function, uncaught.
Recommended action: SIMPLIFY — update the two remaining stale entries to current model
ids for consistency with the rest of the table; low priority given the guarded activation
condition.
Validation required: confirm config/models.yaml is always present in the packaged
distribution (pyproject include list) so this path is genuinely never hit in practice —
not verified in this pass.
Dependencies on other findings: none.
```

```
ID: PERF-04
Category: Performance measurement (routing-critical-path overhead)
Severity: LOW (observational, not a defect)
Confidence: MEDIUM (n=3, single machine, wall-clock not monotonic — see caveat in §23)
Location: Files: src/llm_router/hooks/auto-route.py (whole file, ~4,800 lines)
Observation: Measured hook-process wall-clock for the fast/no-op path (empty prompt,
LLM_ROUTER_ENFORCE=off, clean HOME) at ~120-170ms, against a ~10ms bare-interpreter
baseline on the same machine/venv — i.e. ~110-160ms of import/init cost per invocation,
independent of any classification or model call.
Why this matters: this is routing overhead paid on every UserPromptSubmit regardless of
whether routing actually happens; a single ~4,800-line script importing a meaningful
slice of the package is the likely driver, per §5's "largest files" lens.
User-visible impact: sub-200ms added latency before any user-visible completion begins,
on every prompt, in the hook-based hosts.
Recommended action: no immediate action from this evidence alone; worth a controlled
py-spy/import-profile pass to attribute the ~130ms to specific imports before proposing a
split of auto-route.py, given its size is also a §5/§11 concern outside this domain.
Validation required: repeat with time.monotonic() instrumentation inside the process
(per this worktree's own "wall-clock timings on this machine are not trustworthy" rule)
and with caffeinate -i, across more than 3 runs, before quoting this number externally.
Dependencies on other findings: none.
```

---

## Top items for synthesis (5-10 candidates)

1. **PERF-01 (HIGH)** — session_spend.py's 2s-timeout, exception-swallowed write to the
   shared usage.db is a silent-data-loss bug directly undermining the savings-accuracy
   work in the current HEAD commit. Strong Top-10 correctness-risk candidate.
2. **PRV-01 (dead SemanticCache stub)** — clean, zero-risk deletion; strong deletion-
   ledger candidate (whole fake concept removed, zero callers, evidence exhaustive).
3. **PRV-02 (litellm adapter + quirks registry + WAL fix)** — strong do-not-change
   register candidate; also useful counter-evidence against an assumption that this
   codebase's provider layer needs simplification — it doesn't.
4. **Semantic cache C-03 fix** — good evidence that a previously-reported critical bug
   (cross-passport leak) is genuinely fixed and tested, not just claimed; useful for
   whichever section compiles "claims that check out."
5. **PERF-02/PERF-03 (blocking-in-async, 6 sites)** — moderate, mechanical fix list;
   useful as a batched remediation item rather than 6 separate top-10 entries.
6. **§73 table** — pricing.py is a genuinely enforced single source of truth (lint-
   gated); worth citing as the exemplar the rest of the codebase's SSOT questions
   should be judged against.
7. **PRV-03 (three "tier" concepts)** — hand off to naming/domain-model auditor; flagged
   so it isn't independently rediscovered and double-counted.
8. **§71 finding**: `tools`/`response_format` are not in `providers.py`'s
   `_ALLOWED_EXTRA_PARAMS` allow-list (`providers.py:62-79`) — if tool-calling requests
   are expected to flow through `call_llm`, this needs a follow-up read of how tool
   calls actually reach litellm (possibly a different, unaudited path) before §14/§20
   (agentic tool execution) auditors rely on this file as complete; flagged as
   UNCERTAIN, not asserted as a bug, since this domain's pass did not trace the
   tool-calling call path end to end.
