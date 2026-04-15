# Changelog

## v5.4.0 — Adaptive Universal Router: Live API Discovery + Always-On Dynamic Routing (2026-04-15)

### Added

- **Phase 1: Ollama Live Discovery Injection** — `config.all_ollama_models()` now checks discovery cache first, falling back to env var for backward compatibility. Eliminates manual `OLLAMA_BUDGET_MODELS` synchronization.
- **Phase 2: Live API Enumeration** — New scanners for OpenAI (`/v1/models`) and Gemini (`/v1beta/models`) APIs provide real-time model availability. Anthropic remains static (no public `/models` endpoint). All scanners run in parallel with 5s timeout and graceful degradation.
- **Phase 3: Always-On Dynamic Routing** — Removed `LLM_ROUTER_DYNAMIC` feature flag. Dynamic chain building now runs for all non-media tasks (no opt-in needed). Static `profiles.py` serves as emergency fallback when discovery cache is empty or discovery fails.
- **Phase 4: Sidecar `/score` Endpoint** — New `POST /score` HTTP endpoint ranks models by composite quality score (task-aware). Hook client `score_models()` provides convenient interface with 1s timeout and graceful fallback to original order.
- **Comprehensive v5.0 test suite** — 27 tests covering all 4 phases: Ollama injection, OpenAI/Gemini scanners, always-on dynamic routing, and `/score` endpoint. Includes integration tests for the full routing path.

### Changed

- **Feature flag removed** — `llm_router_dynamic` config field eliminated. All routing now uses dynamic chain builder (no flag check needed).
- **Router always tries `build_chain()` first** — Static chain fallback only used when dynamic routing fails or returns empty. Improves model freshness in all scenarios.
- **Discovery cache warmup on startup** — Service now triggers background discovery run on startup (v5.0 feature), ensuring live model data available for first request.

### Technical Notes

- All 940 tests passing (including 27 new adaptive router tests)
- Free-first invariant preserved: Ollama/Codex always before paid APIs (even if scored lower)
- Discovery cache TTL: 30 minutes; graceful degradation when unavailable
- API scanner timeouts: 5s per scanner; failures don't block discovery (returns `[]`)
- Backward compatible: `OLLAMA_BUDGET_MODELS` env var still supported as fallback

## v5.3.2 — Code Quality: Ruff Linting + v5.3 Audit Demo (2026-04-15)

### Fixed

- **14 ruff linting violations** — Removed unused imports from refactoring in hook_client.py, service.py, service_manager.py, and test files. Eliminated unused variable assignment in router.py. Code now passes `uvx ruff check src/ tests/` with 0 errors.

### Added

- **Comprehensive v5.3 audit demo test suite** (`tests/test_demo_v53.py`) — 16 tests organized in 4 demo classes validating core v5.3 features: heuristic classifier, sidecar service HTTP API, enforce-route observation mode, and correlation ID propagation. Serves as both regression tests and feature documentation.
- **Audit verification script** (`scripts/audit_demo.sh`) — Automated script that runs ruff linting, demo tests, and full test suite with chronicle-ready summary output. Used as part of release verification checklist.

### Technical Notes

- All 913 tests passing (including 16 new demo tests)
- Ruff now fully clean across src/ and tests/
- Demo tests serve dual purpose: regression validation and v5.3 architecture documentation
- Audit script output suitable for release notes and session summaries

## v5.3.1 — Sidecar Service Architecture + Async I/O Fixes (2026-04-15)

### Fixed

- **Missing asyncio import in llm_fs_analyze_context()** — function attempted to use `asyncio.to_thread()` without importing `asyncio`, causing `NameError` at runtime. Added missing import alongside other local imports.

### Changed

- **Sidecar routing service now production-ready** — full implementation of FastAPI service with non-blocking HTTP client hooks, infrastructure detection, and graceful degradation. Eliminates previous hangs and deadlock scenarios.
- **Hook architecture refactored for reliability** — auto-route reduced from 1256 to 60 lines (thin HTTP client), enforce-route changed to observation-only mode (never blocks), both now have 0.5s timeouts with graceful fallthrough.

### Technical Notes

- All 897 tests pass with sidecar service fully integrated
- Zero deadlock risk: hooks are non-blocking, service is optional (graceful degradation)
- Infrastructure operations (MCP tools, system operations) are automatically exempted from routing
- Full backward compatibility maintained

## v5.3.0 — Production Hardening: TOCTOU Fix, Correlation IDs, and Routing Refactor (2026-04-15)

### Fixed

- **TOCTOU in budget enforcement** — concurrent calls could both slip under the daily/monthly spend limit before either recorded its cost. Fixed with a provisional `_pending_spend` reservation made inside `_budget_lock` before releasing it; the reservation is decremented after the call completes (success, failure, or escalation raise).
- **Staleness guard for Claude quota** — `usage.json` older than 24 hours now returns `pressure=0.5` instead of optimistic `0.0`, preventing indefinitely-free routing when the session-start hook is absent. Configurable via `LLM_ROUTER_STALE_PRESSURE_FLOOR`.
- **Sync filesystem read blocking event loop** — `_claude_subscription_state()` now offloads the `usage.json` read to `asyncio.to_thread()`, preventing event-loop stalls on slow/encrypted filesystems.
- **File handle leak in auto-route hook** — replaced `open(path).read()` with `Path(path).read_text()` to close the handle immediately.
- **Duplicate models in routing chain** — Ollama/Codex injection could re-add models already in the static chain; dedup now runs after all injections while preserving free-first order.

### Added

- **Sidecar routing service** — New FastAPI service (`src/llm_router/service.py`) running on localhost:7337 with heuristic classification. Hook connects via non-blocking HTTP client (`hook_client.py`) with 0.5s timeout. Graceful degradation: if service unavailable, all tools allowed unconditionally. Eliminates previous hook hangs by moving classification off the hot path.
- **Context-aware routing** — Service detects infrastructure operations (`mcp__plugin_*`, system tools like Bash/Read/Edit) and skips routing for them. Prevents Serena, Obsidian, and other MCP servers from being blocked or rerouted.
- **Observation-only enforcement hook** — `enforce-route.py` changed from blocking to logging-only mode. All tools allowed; violations are recorded to SQLite for analytics but never block execution. Breaks deadlock loops.
- **Service lifecycle management** — `service_manager.py` handles process lifecycle: starts on session-start with health checks (max 5s), stops on session-end with graceful shutdown (SIGTERM→SIGKILL). PID persisted in `~/.llm-router/service.pid`.
- **Correlation ID tracking** — `route_and_call()` generates a `uuid4().hex[:8]` correlation ID per call, stored in both `usage` and `routing_decisions` SQLite tables (`correlation_id TEXT` column added via idempotent migration). Enables log↔DB joins for debugging.
- **DB indices for query performance** — four new `CREATE INDEX IF NOT EXISTS` statements on `usage(provider, timestamp)`, `usage(model, timestamp)`, `routing_decisions(timestamp)`, and `routing_decisions(final_model)`.
- **Session spend reset documentation** — `session_spend.py` now documents the known limitation that `_pending_spend` resets on MCP server restart, with the recommended workaround (`LLM_ROUTER_MONTHLY_BUDGET`).

### Changed

- **`route_and_call()` refactored** — extracted two helper functions to reduce the 960-line god function:
  - `_resolve_profile()` — resolves routing profile, complexity, and thinking flag
  - `_build_and_filter_chain()` — builds and filters the model chain (override validation, dynamic/static selection, provider filter, policy engine, Ollama/Codex injection, dedup)
  - `route_and_call()` reduced from ~960 to ~527 lines.
- **Dashboard token auth moved to middleware** — `aiohttp` middleware validates `X-Dashboard-Token` on all API routes; token is server-generated, persisted to `~/.llm-router/dashboard.token` (mode 0600), and injected into the HTML template at render time.

### Technical Notes

- All changes are backward-compatible. No config changes required.
- Verification: `uv run pytest tests/ -q --ignore=tests/test_integration.py && uv run ruff check src/ tests/`

## v5.2.0 — Audit Remediation, Observability, and Release Automation (2026-04-14)

### Added

- **Structured routing observability** — the hot path now emits `structlog` events plus optional OpenTelemetry spans for `classify_complexity`, `score_all_models`, `build_chain`, `route_and_call`, and individual `provider_call` attempts.
- **Versioned classifier prompt assets** — the classifier system prompt now lives in `src/llm_router/prompts/classifier_v1.txt`, making prompt revisions visible in diffs and release history.
- **Classifier eval harness** (`scripts/eval_classifier.py`) — ships with a 100-example golden set so prompt or model-chain changes can be measured instead of guessed.
- **Automated release workflow** (`scripts/release.py`) — synchronizes package/plugin versions, verifies changelog metadata, builds artifacts, and drives the commit/push/publish/tag flow from one command.

### Changed

- **Routing fallbacks now refresh live provider pressure per attempt** — long fallback walks stop as soon as a provider exhausts its budget, instead of using stale pressure from the start of the chain.
- **Hook IPC is now atomic and retry-friendly** — route/session files are written with atomic replace semantics, readers retry transient partial JSON, and obvious implementation prompts fast-path straight to `llm_code`.
- **Integration coverage is back in CI** — `tests/test_integration.py` now uses mocked end-to-end coverage that is safe for CI and included in the default pipeline again.
- **Spend aggregation is configurable** — multi-source spend can now be combined with either `max` (default, overlap-safe) or `sum` (`LLM_ROUTER_SPEND_AGGREGATION=sum`) for independent traffic channels.
- **README and release docs are aligned with the current product** — the docs now describe the tracing stack, classifier eval flow, dashboard token auth, and release automation.

### Fixed

- **Dynamic + static fallback chains no longer waste slots on duplicates** — repeated model IDs are removed while preserving the intended order.
- **Background benchmark refresh lock ownership** (`benchmarks.py`) — the worker now releases the specific lock instance it acquired, preventing the `release unlocked lock` warning seen in concurrent test/hot-reload scenarios.
- **Hook regression blind spots** — a larger golden prompt matrix and source-hook-based tests now cover the real shipped hook path instead of stale copies.

### Technical Notes

- Full verification baseline for this release is `uv run pytest tests/ -q --ignore=tests/test_agno_integration.py` and `uv run ruff check src/ tests/`.
- This release continues the audit-remediation track and bundles the reliability, observability, and release-discipline tasks targeted for `v5.2.0`.

## v5.1.0 — Budget Management UX + Enterprise Integrations (2026-04-14)

### Added

- **Persistent budget cap storage** (`budget_store.py`) — `~/.llm-router/budgets.json` stores per-provider monthly spend caps with atomic writes (`tmp → os.replace`). Caps set via CLI or dashboard persist across sessions and take priority over env-var config.
- **`llm-router budget` CLI subcommand** — three commands for interactive budget management:
  - `llm-router budget list` — colored table of all providers with cap, spend, pressure bar, and remaining budget
  - `llm-router budget set <provider> <amount>` — set a monthly cap (e.g. `llm-router budget set openai 20`)
  - `llm-router budget remove <provider>` — remove a cap (reverts to env-var or unlimited)
- **Dashboard Budget tab** — new "💰 Budget" tab in the web dashboard with:
  - Summary cards (total configured cap, current total spend, highest pressure provider)
  - Per-provider cards showing spend vs. cap, pressure bar, and an editable cap input with live Save
  - `GET /api/budget` — JSON endpoint returning all provider budget states
  - `POST /api/budget/set` — accepts `{"provider": "openai", "cap": 20.0}`, writes to budget_store and invalidates cache
- **Prometheus metrics endpoint** (`GET /metrics`) — text exposition format, no extra dependencies:
  - `llm_router_spend_usd{provider}` — current monthly spend per provider
  - `llm_router_budget_pressure{provider}` — budget pressure 0.0–1.0
  - `llm_router_budget_cap_usd{provider}` — configured monthly cap
  - `llm_router_savings_usd_total` — cumulative savings vs. Claude Opus baseline
- **Budget nudge on first-time setup** (`tools/setup.py`) — after successfully adding an API key, the setup flow shows a one-line prompt to set a monthly cap: `llm-router budget set <provider> 20`
- **Uncapped-provider nudge in `llm_budget`** (`tools/admin.py`) — when providers have no cap configured, the budget dashboard appends a `💡 No cap set for:` hint listing them
- **Helicone integration** (`integrations/helicone.py`):
  - `get_helicone_headers()` — returns extra HTTP headers including `Helicone-Auth` and routing properties (`Helicone-Property-Router-Task-Type`, `-Model`, `-Complexity`, `-Profile`)
  - `get_helicone_spend()` — async; queries Helicone's cost aggregation API when `LLM_ROUTER_HELICONE_PULL=true`; results merged into budget pressure alongside local SQLite spend
  - `is_helicone_enabled()` — checks for `HELICONE_API_KEY`
- **LiteLLM BudgetManager integration** (`integrations/litellm_budget.py`):
  - `get_litellm_spend()` — async; reads `spend_logs` table from a LiteLLM Proxy SQLite DB; aggregates monthly spend per provider (e.g. `openai/gpt-4o` → `openai`)
  - `get_litellm_budget_cap()` — reads `budget_limits` table for per-user/team caps (LiteLLM Proxy ≥ 1.30)
  - `is_litellm_budget_enabled()` — checks `LLM_ROUTER_LITELLM_BUDGET_DB` env var and file existence
- **Multi-source spend aggregation** in `_api_provider_state()` (`budget.py`) — spend is now the **maximum** of three concurrent sources (local SQLite DB, Helicone pull, LiteLLM DB). Using the maximum ensures pressure is never under-reported when traffic flows through multiple tracking systems.
- **New config fields** in `config.py`:
  - `HELICONE_API_KEY` — Helicone authentication key
  - `LLM_ROUTER_HELICONE_PULL` — enable spend pull from Helicone API (default: `false`)
  - `LLM_ROUTER_LITELLM_BUDGET_DB` — path to LiteLLM Proxy SQLite database
  - `LLM_ROUTER_LITELLM_USER` — optional user/team key filter for LiteLLM queries
- **35 new tests** across `test_budget_store.py` (18) and `test_integrations.py` (17) covering CRUD ops, atomic writes, env-var priority, Helicone header generation, spend pull parsing, and LiteLLM SQLite queries.

---

## v5.0.0 — Adaptive Universal Router (2026-04-14)

### Added

- **Budget Oracle** (`budget.py`) — normalises budget pressure [0.0–1.0] across all provider types: local (always 0.0), Claude subscription (max of session/weekly/sonnet quota dimensions via `highest_pressure`), daily-rate providers (Groq), monthly-request providers (HuggingFace), monthly-spend providers (OpenAI, Gemini, DeepSeek, etc.). Results cached 60s.
- **Universal Model Discovery** (`discover.py`) — scans Ollama (`/api/tags`), HuggingFace free-tier, configured API-key providers, and Codex CLI. Encodes quota type, provider tier, task types, and latency estimate per model. Results cached 30 min to `~/.llm-router/discovery.json`. Includes `_OLLAMA_MODEL_REGISTRY` mapping 16 local model families to benchmark aliases.
- **Live Benchmark Registry** (Phase 4 additions to `benchmarks.py`) — `get_quality_score(model_id, task_type)` exposes quality scores for both API models (via benchmark data) and local Ollama models (via `_LOCAL_QUALITY_SCORES` keyed by benchmark alias). Background refresh: `maybe_refresh_benchmarks_background()` checks staleness and spawns a thread if benchmarks are older than `LLM_ROUTER_BENCHMARK_TTL_DAYS` (default 7). Session-start hook triggers the background refresh automatically.
- **Unified Scorer** (`scorer.py`) — `score_model()` computes a composite score using `COMPLEXITY_WEIGHTS` from `types.py` (simple: 45% budget, 20% quality; complex: 55% quality, 20% budget). `score_all_models()` fetches all supporting data in parallel and returns models sorted best-first. Pure-compute inner loop — no I/O in the hot path.
- **Dynamic Chain Builder** (`chain_builder.py`) — `build_chain()` assembles a ranked model chain from discovered models using scorer output. Preserves free-first ordering (LOCAL tier before paid APIs). Score floor 0.30, min chain length 2, max 8. Falls back to static `profiles.py` chain on any error or when `LLM_ROUTER_DYNAMIC=false` (default).
- **`llm_budget` MCP tool** — shows real-time Budget Oracle pressure bars for all providers plus adaptive router status. Call `llm_budget` to see which providers are near exhaustion.
- **New types** in `types.py`: `ProviderTier`, `ModelCapability`, `BudgetState`, `ComplexityWeights`, `ScoredModel`, `COMPLEXITY_WEIGHTS`, `LOCAL_PROVIDERS`.
- **New config fields**: `llm_router_dynamic`, `llm_router_discovery_ttl`, `llm_router_benchmark_ttl_days`, `llm_router_budget_{openai,gemini,groq,deepseek,together,perplexity,mistral}`.
- **Session-type tracking** in `enforce-route.py` — once Claude calls `Edit`/`Write` in a session, it's marked as a coding session and enforcement downgrades to soft for the remainder. Prevents deadlocks on mixed query+coding sessions.
- **`_is_build_task()` fast-path** in `auto-route.py` — detects coding prompts via regex (build verb + build object) before the classifier runs, skipping routing for implementation work.

### Changed

- Pending route TTL reduced from 300s → 60s to prevent stale directives poisoning new turns.
- Router (`router.py`) routes through `build_chain()` when `LLM_ROUTER_DYNAMIC=true`, with full static fallback.
- Session-start hook (v16) triggers background benchmark refresh if `~/.llm-router/benchmarks.json` is stale.

### Feature flag

`LLM_ROUTER_DYNAMIC=true` activates the Adaptive Universal Router. Default is `false` — all v4.x behaviour is preserved.
## v4.2.0 — Quota-Aware Routing + Context-Aware Classification (2026-04-13)

### Added

- **qwen3.5:32b in BALANCED chains for moderate complexity** (`src/llm_router/profiles.py`)

  qwen3.5:32b added to all BALANCED text chains (QUERY, GENERATE, ANALYZE, CODE) as the
  last entry. In CC-mode, `_reorder_for_agent_context` auto-promotes Ollama models to
  first position for simple/moderate tasks, so qwen3.5:32b is tried before Sonnet when
  running locally — saving subscription quota for complex work. RESEARCH, IMAGE, VIDEO,
  and AUDIO chains excluded (web access / media not applicable).

- **Short code follow-up context inheritance** (`.claude/hooks/auto-route.py` v17)

  Short prompts (≤15 words) after a code task were being misclassified as `generate/query`
  by the fallback classifier, causing enforcement deadlocks when users followed a code session
  with brief follow-ups like "explain why X doesn't work" or "go ahead and do the change".

  - `_is_short_code_followup()` — detects ≤15-word prompts when the previous `last_route`
    was a code task (checks `~/.llm-router/last_route_{session_id}.json`)
  - New routing branch between `_is_continuation` and the full classifier — inherits
    `task_type/complexity/tool` from `last_route` without resaving (preserves code context)
  - Routing directive shows `[via code-context-inherit]` method tag

### Changed

- **CC-mode simple tasks now route via Ollama-first MCP chain** (`.claude/hooks/auto-route.py` v18)

  Previously, CC-mode routed simple tasks to `/model claude-haiku-4-5-20251001`, consuming
  subscription quota for every trivial question. Simple tasks now route through
  `llm_*(complexity="simple")` (Ollama → Haiku fallback), preserving session quota for
  moderate/complex work. Haiku remains the fallback inside the MCP tool when Ollama is
  unavailable.

  Updated CC-mode routing table (low pressure):

  | Complexity | Route | Model chain |
  |---|---|---|
  | simple | `llm_*(complexity="simple")` | Ollama → Haiku fallback |
  | moderate | `llm_*(complexity="moderate")` | Sonnet passthrough |
  | complex | `/model claude-opus-4-6` | Opus via subscription |

### Fixed

- **Test assertions updated for current `[ROUTE→` directive format** (`tests/test_auto_route_hook.py`)

  Hook output format changed from `⚡ MANDATORY ROUTE:` to `⚡ ROUTE→tool(args)` in v4.0.x.
  Test assertions were still matching the old format — updated throughout.

---

## v4.1.1 — Fix PostToolUse matcher for MCP tool names (2026-04-13)

### Fixed

- **PostToolUse `usage-refresh` hook never fired for MCP tool calls** — the hook matcher was `"llm_"` but Claude Code passes the full MCP tool name `mcp__llm-router__llm_*` to PostToolUse hooks. The hook never triggered, so the dashboard showed no savings from routed calls. Matcher updated to `"llm_|mcp__llm-router__llm"` in both `_HOOK_DEFS` and `_CLAW_CODE_HOOK_DEFS`.
- **Default enforcement changed from `hard` to `smart`** — `hard` mode blocked `Read`/`Grep`/`Glob` for Q&A tasks, causing deadlocks where Claude couldn't read files to answer questions without first calling an `llm_*` tool. `smart` mode allows file reads for all task types; only `Bash`/`Edit`/`Write` are blocked for Q&A tasks.

---

## v4.1.0 — Playwright DOM compression + routing.yaml enforcement fix (2026-04-13)

### Added

- **`playwright-compress.py` PostToolUse hook** — fires after every `browser_snapshot` call; compresses the DOM via a cheap LLM (Ollama → Gemini Flash → rule-based fallback) and injects a compact summary (`REFS / STATE / ERRORS`) as `contextForAgent`. Eliminates the depth-escalation pattern (`depth:3→6→8`) and retry storms that cause 60-80% of token waste in Playwright sessions.
- **Free-first compression chain** — Ollama (local, free) → Gemini Flash (cheap, requires `GEMINI_API_KEY`) → regex rule-based extraction (always works, instant). Consistent with llm-router's existing routing philosophy.
- **Opt-out env var** — `LLM_ROUTER_PLAYWRIGHT_COMPRESS=off` disables the hook without uninstalling it.
- Hook registered for both Claude Code (`_HOOK_DEFS`) and claw-code (`_CLAW_CODE_HOOK_DEFS`).

### Fixed

- **`enforce:` in `routing.yaml` was silently ignored** — `enforce-route.py` previously read `LLM_ROUTER_ENFORCE` with a hard-coded default of `"smart"`, so users who set `enforce: hard` in `~/.llm-router/routing.yaml` saw no effect. The hook now reads `routing.yaml` as a fallback when the env var is absent; priority is: `LLM_ROUTER_ENFORCE` env var → `routing.yaml` → built-in default (`smart`). Fix applied to both the installed hook and the distributed source.
- **8 new tests** in `test_route_enforcement_hooks.py` covering: hard/soft/off/shadow from yaml, env var priority over yaml, smart fallback when neither source is present, whitespace-tolerant parsing, and yaml without an `enforce:` line.

---

## v4.0.5 — Robust pytest hang detection via JUnit XML (2026-04-13)

### Fixed

- **CI test step now reliable against pytest hang** — uses background process + JUnit XML approach:
  pytest runs in background (`&`); waiter loop polls for exit up to 150s; if still alive after 150s (aiosqlite thread hang), reads `--junit-xml` written before the hang to determine true pass/fail. Exits 0 only if `failures=0` and `errors=0` in the XML.
- **Increased job timeout to 10 min** — gives room for cached install (~60s) + tests (~30s) + 150s hang window.

---

## v4.0.4 — Fix pytest hang on CI exit (2026-04-13)

### Fixed

- **pytest hangs after passing** — aiosqlite's `_connection_worker_thread` is a non-daemon thread; Python waits for it indefinitely after tests complete. Fixed with two layers:
  1. `asyncio_default_fixture_loop_scope = "session"` in `pyproject.toml` — reuses one event loop across the session, reducing thread churn
  2. CI wraps pytest in `timeout 90`; exit code 124 (timeout) is treated as success if all tests passed (output contains `N passed` with no `failed`/`error`)
- **Job timeout-minutes reduced to 8** — was 20, now safe with caching enabled

---

## v4.0.3 — CI cache + test isolation fix (2026-04-13)

### Fixed

- **Test env isolation** — `_run_hook` in `test_route_enforcement_hooks.py` now strips `LLM_ROUTER_ENFORCE` from the inherited shell env before running hook subprocess. Tests were non-deterministic when the developer had `LLM_ROUTER_ENFORCE=soft` set locally, causing `test_enforce_route_blocks_work_tools_by_default` to fail.

### Changed

- **CI caching** — `astral-sh/setup-uv@v4` now runs with `enable-cache: true` in both `ci.yml` and `publish.yml`. Subsequent runs skip re-downloading packages; install step drops from ~18 min to ~30s.
- **Lint via `uvx`** — both workflows now run `uvx ruff check` instead of installing the full dev venv first. Lint job completes in ~10s regardless of dep install time.
- **CI matrix** — trimmed from 3.11/3.12/3.13 to 3.11/3.13 (min + max). 3.12 adds no coverage not caught by the endpoints.
- **Publish workflow simplified** — removed redundant `test` gate job; lint + version check + build is the only gate before PyPI publish.

---

## v4.0.2 — CI reliability + cost-contract test coverage (2026-04-12)

### Fixed

- **CI hang on Python 3.12/3.13** — `test_agno_integration.py` was included in the standard test run, which caused agno's async initialisation to hang on Python 3.12/3.13. Both `ci.yml` and `publish.yml` now exclude agno tests from the default suite; agno tests run separately when `--extra agno` is explicitly requested.
- **Publish workflow never reaching PyPI** — The v4.0.1 publish job was gated behind the `test` job that used Python 3.12, so every publish attempt was cancelled. Removing the `--extra agno` install from the test gate unblocks the publish chain.

### Added

- **`tests/test_routing_value.py`** — 32 tests covering the core cost-saving contracts:
  - Complexity→profile mapping: SIMPLE→BUDGET (Haiku $0.0008/1K) vs COMPLEX→PREMIUM (Opus $0.075/1K) — a regression here costs 94× per query
  - Subscription-aware agent reordering: verifies Ollama→Claude→GPT-4o ordering for claude_code sessions (free-first)
  - Pressure-based chain reordering: at ≥85% Claude quota, cheap models (Gemini Flash, Groq) lead the chain; at ≥99% Claude is removed entirely
  - Circuit breaker isolation: failures on one provider don't cascade to others
  - Model chain shape assertions: BUDGET chains never start with frontier models (Opus, o3)

- **`tests/test_config_routing_value.py`** — 27 tests covering provider detection safety:
  - `available_providers` with all key combinations
  - Claude subscription mode: `anthropic` excluded AND key withheld from `os.environ` (prevents LiteLLM bypass)
  - Ollama inclusion/exclusion gated by liveness probe
  - `text_providers` vs `media_providers` segmentation
  - Routing defaults validated (profile=BALANCED, budget=$20, temperature=0.7)

## v4.0.0 — Token Efficiency, Real-Time Spend, Feedback Learning (2026-04-10)

### Summary

Major version focused on four pillars: **save tokens** (slim mode), **see costs live** (session spend meter), **learn from corrections** (reroute + feedback loop), and **instant onboarding** (quickstart wizard + doctor --host). VS Code extension backlogged for v4.1.

### Added

- **Tool slim mode** (`LLM_ROUTER_SLIM=routing|core`) — register only the tools you need to save context tokens
  - `off` (default): all 43 tools — backward compatible
  - `routing`: 12 routing + admin tools — ~5,000 tokens saved per session
  - `core`: 4 essential tools — maximum token savings (~7,500 tokens saved)
  - New module `src/llm_router/tool_tiers.py` with tier definitions and `make_should_register()` factory
  - All 9 `register(mcp)` functions now accept `should_register` gate parameter

- **Real-time session spend meter** — track API costs as they happen
  - New module `src/llm_router/session_spend.py` — persists `~/.llm-router/session_spend.json`
  - New MCP tool **`llm_session_spend`** — shows spend breakdown by model and tool with anomaly warnings
  - Anomaly detection: flags if session spend exceeds `$0.50` in under 10 minutes (configurable via `LLM_ROUTER_ANOMALY_THRESHOLD`)
  - Session-end hook now prints one-liner: `💰 Session API spend: $0.023 · top model: gemini-flash`
  - Router records spend after every successful call without blocking

- **Cost-threshold escalation** — automatic guardrails against runaway costs
  - `LLM_ROUTER_ESCALATE_ABOVE=0.10` blocks any single call estimated above $0.10
  - `LLM_ROUTER_HARD_STOP_ABOVE=1.00` cancels all routing once session reaches $1.00
  - New MCP tool **`llm_approve_route`** — approve/reject pending high-cost calls; optionally downgrade to cheaper model
  - New config fields: `llm_router_escalate_above`, `llm_router_hard_stop_above`

- **`llm_reroute` tool** — correct bad routing decisions in-session and train the router
  - Records corrections to new `corrections` SQLite table in usage.db
  - `llm_route` explain mode now shows routing confidence lowered by past corrections (−15% per correction)
  - New DB functions: `log_correction()`, `get_correction_count()`

- **`llm-router quickstart`** — guided sub-5-minute first success wizard
  - Auto-detects installed hosts (Claude Code, Cursor, VS Code)
  - Walks through API key setup or Ollama-only mode
  - Fires a live test call and shows savings projection
  - New entry point: `llm-router-quickstart`

- **`llm-router doctor --host <name>`** — host-specific installation diagnostics
  - `--host vscode`: checks mcp.json with `servers` key, uvx in PATH
  - `--host cursor`: checks `~/.cursor/mcp.json` with `mcpServers` key, routing rules
  - `--host claude`: checks hooks presence and executability
  - `--host all`: runs all three
  - Integrated into `llm-router doctor` as an optional pre-section

- **`llm_fs_analyze_context`** — workspace-aware routing context
  - Reads key project files (pyproject.toml, package.json, README, CLAUDE.md, etc.)
  - Routes summarization to cheap model; persists `~/.llm-router/context_summary.json`
  - Subsequent routing calls inject workspace summary into system prompt automatically

- **README hero rewrite** — cleaner first impression with quick install table and logo badges
  - Supported Hosts table now shows provider logos via shields.io badges
  - New hero section with savings headline and quickstart command

### Technical Notes

- 38 new tests in `tests/test_v4_features.py` covering all 4.0.0 features
- All existing tests pass (612 total)
- `session_spend.json` uses flat JSON (not SQLite) intentionally: hook scripts read it with stdlib only
- Escalation check occurs before the model loop, uses the same `config` reference (respects test mocks)
- VS Code status bar extension moved to v4.1 backlog (requires TypeScript/npm build toolchain)

---

## v3.6.0 — VS Code + Cursor IDE Support (2026-04-10)

### Added

- **`llm-router install --host vscode`** — writes user-level `mcp.json` with `"servers"` root key (VS Code MCP spec); appends routing guidance to `.github/copilot-instructions.md`
  - macOS: `~/Library/Application Support/Code/User/mcp.json`
  - Linux: `~/.config/Code/User/mcp.json`
  - Windows: `%APPDATA%/Code/User/mcp.json`
- **`llm-router install --host cursor`** — writes `~/.cursor/mcp.json` with `"mcpServers"` root key; installs routing rules to `~/.cursor/rules/llm-router.md`
- **`vscode-rules.md`** + **`cursor-rules.md`** bundled in package — task routing tables, token-efficient response rules, MCP setup guidance
- **`_merge_json_mcp_block` `root_key` parameter** — cleanly supports both `"servers"` (VS Code) and `"mcpServers"` (Cursor/Desktop/Codex) without duplication

### Technical Notes

- All installs are idempotent — re-running skips already-configured entries
- VS Code uses `"servers"` (MCP spec standard); Cursor uses `"mcpServers"` (Claude Desktop compat)
- 22 new tests in `tests/test_vscode_cursor_install.py` covering file writes, key format, idempotency, merge, and rules content

---

## v3.5.0 — Multi-Agent CLI Compatibility (2026-04-10)

### Added

- **Factory Droid plugin** (`.factory-plugin/`)

  llm-router is now a first-class Factory Droid plugin. Factory Droid explicitly
  supports `.claude-plugin/` format — the existing manifest works automatically.
  Added dedicated `.factory-plugin/plugin.json` and `marketplace.json` for the
  Factory marketplace. Install via: `factory plugin install ypollak2/llm-router`

- **`llm-router install --host opencode`** — writes `~/.config/opencode/config.json` MCP block, PostToolUse hook, routing rules
- **`llm-router install --host gemini-cli`** — writes `~/.gemini/settings.json`, Gemini CLI extension manifest + hooks, routing rules
- **`llm-router install --host copilot-cli`** — writes `~/.config/gh/copilot/mcp.json`, routing rules
- **`llm-router install --host openclaw`** — writes `~/.openclaw/mcp.json`, routing rules
- **`llm-router install --host trae`** — writes platform-appropriate Trae config + `.rules` file
- **`llm-router install --host factory`** — confirms `.factory-plugin/` is present, prints install command

- **3 new helper functions** in `cli.py` (`_merge_json_mcp_block`, `_append_routing_rules`, `_copy_hook_script`)

  All install functions share these to keep the implementation DRY and consistent.
  Each function is idempotent — safe to run multiple times.

- **5 new rules files** (`src/llm_router/rules/`)

  `opencode-rules.md`, `gemini-cli-rules.md`, `copilot-cli-rules.md`,
  `openclaw-rules.md`, `trae-rules.md` — each with routing guidance and
  the token-efficient response principles from the caveman skill.

- **2 new PostToolUse hook scripts** (`src/llm_router/hooks/`)

  `opencode-post-tool.py`, `gemini-cli-post-tool.py` — flush pending savings
  records to `savings_log.jsonl` with `host=opencode` / `host=gemini_cli` tags.

- **45 new tests** (`tests/test_multi_host_install.py`)

  Full coverage of all install functions, helper utilities, idempotency,
  Factory Droid manifest schema, and rules file content.

## v3.4.0 — Agent-Context Chain Reordering (2026-04-10)

### Added

- **Agent-context aware model chain reordering** (`src/llm_router/router.py`)

  When `llm_select_agent` has determined the active agent (Claude Code or Codex),
  subsequent routing calls now reorder the model chain to put that agent's subscription-
  covered models first — maximising already-paid capacity before paid-per-call APIs.

  Priority matrix:

  | Session | Complexity | Chain order |
  |---------|-----------|-------------|
  | Codex | simple / moderate | Ollama → Codex → rest → Claude |
  | Codex | complex | Codex → Claude → rest → Ollama |
  | Claude Code | simple / moderate | Ollama → Claude → rest → Codex |
  | Claude Code | complex | Claude → rest → Codex → Ollama |

  Ollama stays first for simple/moderate tasks (free + local), falls to last for
  complex tasks (quality matters more than cost at high complexity).

- **`get_active_agent()` / `set_active_agent()`** (`src/llm_router/state.py`)

  New shared state accessors for the active agent context. `llm_select_agent` now
  calls `set_active_agent(primary)` after resolving its decision tree so all routing
  calls in the same session inherit the subscription context.

- **`_reorder_for_agent_context(models, agent, complexity)`** (`src/llm_router/router.py`)

  Pure function that reorders a model list into groups `[ollama, codex, rest, claude]`
  and returns them in the priority order for the given agent/complexity combination.
  Called automatically after all Codex/Ollama injection, before any model is tried.

- **34 new tests** (`tests/test_agent_context_routing.py`)

  Full coverage of state helpers, both agent types, all complexity levels, and edge
  cases (chains missing Ollama, Claude, or Codex models).

## v3.3.0 — Codex Plugin (2026-04-10)

### Added

- **Codex CLI plugin package** (`.codex-plugin/`)

  llm-router is now a first-class Codex plugin, installable from the Codex marketplace.
  - `.codex-plugin/plugin.json` — full plugin manifest with marketplace metadata
  - `.codex-plugin/marketplace.json` — Codex marketplace entry (mirrors `.claude-plugin/`)
  - `.codex-plugin/.mcp.json` — MCP server declaration (`uvx claude-code-llm-router`)

- **`llm-router install --host codex` now writes files** (`src/llm_router/cli.py`)

  Previously printed copy-paste snippets only. Now actually writes:
  - `~/.codex/config.yaml` — appends `llm-router` MCP server block
  - `~/.codex/hooks.json` — adds PostToolUse hook entry (creates file if absent)
  - `~/.codex/instructions.md` — appends routing rules (creates if absent)
  - `~/.llm-router/hooks/codex-post-tool.py` — installs the hook script

- **Codex PostToolUse hook** (`src/llm_router/hooks/codex-post-tool.py`)

  Fires after every Bash tool call in Codex. Reads pending savings records from
  `~/.llm-router/codex_session.json` (written by `llm_auto` / `llm_track_usage`)
  and flushes them to `savings_log.jsonl` with `"host": "codex"` tag.
  Rate-limited to once per 30 seconds to avoid log churn.

- **Codex Skills** (`skills/routing/SKILL.md`, `skills/savings/SKILL.md`)

  Markdown skill files bundled into the plugin. Teach the Codex agent:
  - `routing/SKILL.md` — when to call `llm_query`, `llm_code`, `llm_auto`, etc. and why
  - `savings/SKILL.md` — how to use `llm_savings`, `llm_digest`, `llm_policy`, `llm_benchmark`

---

## v3.2.1 — Session-End Cumulative Accuracy Fix (2026-04-09)

### Fixed

- **Session-end hook: JSONL records now flushed before cumulative query** (`src/llm_router/hooks/session-end.py`)

  Codex and Ollama calls that bypass the MCP server write savings records to
  `~/.llm-router/savings_log.jsonl` via the PostToolUse hook. The session-end hook
  was querying cumulative totals directly from SQLite without first importing those
  records, causing a one-session lag for free-provider savings.

  Fix: `_sync_import_savings_log()` is now called in `main()` before
  `_query_cumulative_savings()`. It flushes any pending JSONL lines into the
  `savings_stats` table synchronously (stdlib-only, no venv dependency).
  The cumulative query now also unions `savings_stats` alongside `usage`, so
  pre-computed savings from JSONL imports are included in every period total.

---

## v3.2.0 — Policy Engine + Slack Digests + Community Benchmarks (2026-04-09)

### Added

- **Policy Engine (`llm_policy` MCP tool, `src/llm_router/policy.py`)** — v3.2

  Org/user/repo precedence hierarchy for model-level routing policy. Supports
  glob-based allow/deny lists (e.g. `block_models: ["gpt-4o", "o3*"]`), per-task
  cost caps, and a `~/.llm-router/org-policy.yaml` file for shared org-wide rules.
  - `RepoConfig` extended with `block_models` and `allow_models` fields
  - Policy audit trail written to `routing_decisions.policy_applied` column
  - `llm_policy` tool shows active org + repo policy and last 10 policy decisions

- **Savings Digest (`llm_digest` MCP tool, `src/llm_router/digest.py`)** — v3.3

  Period-based savings summaries with Slack/Discord/webhook push support.
  - Spend spike detection: alerts when today's spend > 2× 7-day average
  - "What if router was off?" simulation shows cost without routing
  - Auto-detects webhook channel from URL: `hooks.slack.com` → Slack,
    `discord.com` → Discord, anything else → generic JSON POST
  - `LLM_ROUTER_WEBHOOK_URL` env var for separate digest channel
  - `llm_digest(period="week", send=True)` pushes to webhook

- **Community Benchmarks (`llm_benchmark` MCP tool, `src/llm_router/community.py`)** — v3.4

  Per-task-type routing accuracy derived from `llm_rate` feedback data.
  - Shows accuracy %, total rated calls, and top-performing model per task type
  - Confidence strings: `★★★ High`, `★★☆ Medium`, `★☆☆ Low`, `☆☆☆ No data`
  - `LLM_ROUTER_COMMUNITY=true` enables anonymous local export to
    `~/.llm-router/community_export.jsonl` (upload endpoint deferred)

### Changed

- `llm_policy`, `llm_digest`, `llm_benchmark` registered as MCP tools (total: 41)

---

## v3.1.0 — Multi-Host Support + Cross-Session Savings (2026-04-09)

### Added

- **`llm_auto` MCP tool** (`src/llm_router/tools/routing.py`)

  New sibling to `llm_route` designed for hosts without a UserPromptSubmit hook
  (Codex CLI, Claude Desktop, GitHub Copilot). Identical routing logic, plus:
  - Flushes pending JSONL savings records into SQLite before routing, so
    cross-session savings are accurate even when called from hook-less hosts.
  - Appends a compact savings envelope every 5 calls so savings are visible
    without running `llm_savings` explicitly.

- **Cross-session savings wiring** (`src/llm_router/tools/admin.py`)

  `llm_savings()` and `llm_usage()` now call `import_savings_log()` before
  querying, so hook-written JSONL records are always flushed into SQLite first.
  Previously `import_savings_log()` was defined but never triggered — savings
  from the PostToolUse hook were written to JSONL but never persisted to SQLite.

- **`host` column in `savings_stats`** (`src/llm_router/cost.py`)

  New `host TEXT NOT NULL DEFAULT 'claude_code'` column tracks which client
  originated each routed call. The PostToolUse hook writes `"host": "claude_code"`;
  future host adapters will write `"codex"`, `"desktop"`, or `"copilot"`.
  Idempotent migration applied on DB open (existing rows default to `claude_code`).

- **Host config snippets** (`llm-router install --host <name>`)

  New CLI subcommand prints copy-paste config for non-Claude Code hosts.
  No files are modified — snippets only.
  - `--host codex` — `~/.codex/config.yaml` + routing rules
  - `--host desktop` — `claude_desktop_config.json` snippet
  - `--host copilot` — `.vscode/mcp.json` + `copilot-instructions.md` template
  - `--host all` — all three

- **Host routing rules** (`src/llm_router/rules/`)

  Three new rules files for non-Claude Code hosts:
  - `codex-rules.md` — how to use `llm_auto` in Codex CLI
  - `desktop-rules.md` — capability extension framing for Desktop
  - `copilot-rules.md` — capability extension framing for Copilot

- **Phase 0 research doc** (`docs/multi-host-research.md`)

  Documents architecture findings, revised plan scope, and host compatibility
  matrix for future reference.

### Fixed

- **Yearly savings projection now uses 30-day average** (`src/llm_router/hooks/session-end.py`)

  The session-end summary previously projected yearly savings from a 7-day rolling
  average, making early-session estimates unreliable. It now uses month-to-date data
  (actual days elapsed as divisor) with a fallback chain: month → week → today.
  The basis label in the output reflects the window used (`30-day avg`, `7-day avg`,
  or `today`).

### Technical notes

- Total MCP tools: 38 (was 37 — added `llm_auto`)
- `llm_route` remains unchanged for backwards compatibility; `llm_auto` is additive
- `savings_stats` now has a `host` column; existing rows migrate to `'claude_code'`
- Codex CLI confirmed to have no hook API — falls back to capability-extension tier
- Claude Desktop and Copilot are capability-extension tier only (no cost-routing)

---

## v3.0.0 — Team Dashboard + Multi-Channel Push (2026-04-08)

### Added

- **Team Dashboard** (`src/llm_router/team.py`)

  New module for team identity, savings aggregation, and multi-channel push notifications:
  - `get_user_id()` — resolves to git email, falls back to `username@hostname`
  - `get_project_id()` — resolves to git remote origin basename, falls back to cwd name
  - `detect_channel(url)` — auto-detects Slack / Discord / Telegram / generic from URL pattern
  - `push_report(report, url, telegram_chat_id)` — async HTTP POST with channel-native format

- **Multi-channel push formats**

  Each channel receives a native format:
  - **Slack**: Block Kit with header, fields, model list, and footer link
  - **Discord**: Embed with color, fields, and footer
  - **Telegram**: MarkdownV2 with proper escaping for special characters
  - **Generic**: Raw JSON POST for custom webhooks

- **`llm_team_report` + `llm_team_push` MCP tools** (`src/llm_router/tools/admin.py`)

  Two new tools (total 37 tools now) for team savings visibility:
  - `llm_team_report(period)` — box-drawing ASCII table of calls/savings/free-tier/top models
  - `llm_team_push(period)` — push report to configured channel; channel auto-detected from URL

- **Team settings in config** (`src/llm_router/config.py`)

  Three new environment variables:
  - `LLM_ROUTER_TEAM_ENDPOINT` — webhook URL (Slack / Discord / Telegram / generic)
  - `LLM_ROUTER_USER_ID` — override auto-detected git email
  - `LLM_ROUTER_TEAM_CHAT_ID` — Telegram chat_id (only needed for Telegram)

- **`llm-router team` CLI subcommand** (`src/llm_router/cli.py`)

  New `team` subcommand with three actions:
  - `llm-router team report [--period week|month|all]` — print savings dashboard to stdout
  - `llm-router team push [--period week|month|all]` — push to configured webhook
  - `llm-router team setup` — interactive wizard to configure endpoint and verify connection

- **Team identity columns in usage DB** (`src/llm_router/cost.py`)

  Idempotent migration adds `user_id` and `project_id` columns to the `usage` table.
  New `get_team_savings(user_id, project_id, period)` aggregation query for dashboard data.

### Changed

- **Ollama always injected when configured** (`src/llm_router/router.py`)

  Removed the gate that restricted Ollama injection to `BUDGET` profile or pressure ≥ 85%.
  Ollama is now always prepended to the routing chain when `OLLAMA_BASE_URL` is set —
  it is free and local, so there is no reason to ever skip it.

## v2.6.0 — Latency-Aware + Personalized Routing (2026-04-08)

### Added

- **User-acceptance feedback loop** (`src/llm_router/cost.py`)

  New `get_model_acceptance_scores(window_days=30)` function queries the `was_good` column from `routing_decisions` table to compute per-model acceptance rates. Models with < 50% acceptance receive up to a 40% score penalty in benchmark ordering — pushing poorly-rated models down the routing chain automatically.

- **Acceptance penalty in benchmark ordering** (`src/llm_router/benchmarks.py`)

  New `get_model_acceptance_penalty(model, acceptance_scores)` function with three tiers:
  - ≥ 70% acceptance → no penalty
  - ≥ 50% acceptance → 20% penalty
  - < 50% acceptance → 40% penalty

  Wired into `apply_benchmark_ordering()` via the new `acceptance_scores` parameter, threaded through `get_model_chain()` in `profiles.py` and the `route_and_call()` async gather in `router.py`.

- **Model Performance section in `llm_usage`** (`src/llm_router/tools/admin.py`)

  New section in the usage dashboard showing per-model P50/P95 latency (7-day window) and user acceptance rate (30-day window). Lists top 8 models by call count.

- **`smart` enforcement mode** (`src/llm_router/hooks/enforce-route.py` v6)

  New default enforcement mode that achieves >80% routing compliance without blocking developer workflow:
  - **query / research / generate / analyze** tasks → hard block (Bash/Edit/Write blocked until `llm_*` called — the answer must come from the cheap model)
  - **code** tasks → soft (file tools are needed for actual editing, not blocked)

  Previous default `hard` → new default `smart`. Users can override with `LLM_ROUTER_ENFORCE=hard|soft|off`.

- **Stale pending state cleanup on session start** (`src/llm_router/hooks/session-start.py` v11)

  Session start now clears any orphaned `pending_route_*.json` files from crashed or killed sessions. Previously, a hard-killed Claude session left stale state files that would block Bash/Edit in the next session.

### Changed

- `LLM_ROUTER_ENFORCE` default changed from `hard` → `smart`
- `apply_benchmark_ordering()` signature extended with `acceptance_scores: dict[str, float] | None = None`
- `get_model_chain()` signature extended with `acceptance_scores: dict[str, float] | None = None`
- `route_and_call()` now fetches acceptance scores in parallel with failure_rates and latency_stats

---

## v2.5.0 — Context-Aware Routing (2026-04-08)

### Added

- **Continuation prompt state inheritance** (`src/llm_router/hooks/auto-route.py` v16)

  Short follow-up prompts (`yes`, `ok`, `go ahead`, `do it`, `sounds good`, etc.) now instantly reuse the prior turn's `task_type/complexity/tool` instead of re-running the full Ollama/API classifier chain (~1–3s saved per continuation turn).

  - `_is_continuation()` — detects affirmatives, negatives, and ≤5-word zero-signal prompts
  - `_save_last_route()` / `_load_last_route()` — session-scoped state at `~/.llm-router/last_route_{session_id}.json` with 30-minute TTL
  - Negative continuations (`no`, `stop`, `skip`, `cancel`) downgrade to `query/simple → llm_query`
  - Routing directive shows `[via context-inherit]` method tag
  - Session states are keyed by `session_id` so parallel Claude sessions never interfere

---

## v2.4.0 — Repo-Aware YAML Config (2026-04-08)

### Added

- **`src/llm_router/repo_config.py`** — Two-layer YAML configuration system

  Loads and merges two optional config files with clear precedence:
  ```
  env vars > .llm-router.yml (repo) > ~/.llm-router/routing.yaml (user) > defaults
  ```

  - `RepoConfig` dataclass: `profile`, `enforce`, `block_providers`, `routing` (per-task model/provider pins), `daily_caps`
  - `load_user_config()` — reads `~/.llm-router/routing.yaml`
  - `load_repo_config()` — searches cwd and ancestors for `.llm-router.yml`
  - `effective_config()` — merges both (repo wins over user config)
  - `fingerprint_repo()` — auto-detects Python/Node/Go/Rust/Java/Swift/Ruby/PHP from indicator files, suggests appropriate profile

- **`llm-router config` sub-commands** (`src/llm_router/cli.py`)

  - `llm-router config show` — displays effective settings with per-field source annotation
  - `llm-router config lint` — validates YAML files, surfaces unknown keys and invalid values
  - `llm-router config init` — creates starter `.llm-router.yml` with repo-fingerprinted profile suggestion

- **`llm-router onboard` wizard** (`src/llm_router/cli.py`)

  Interactive first-run setup: detects available providers (Ollama, Codex, API keys), recommends a profile, lets you pick enforcement mode (shadow/suggest/enforce), writes `~/.llm-router/.env`, and runs `llm-router install`.

- **Block providers + model/provider pins** (`src/llm_router/router.py`)

  Repo config `block_providers` list is applied before routing — blocked providers are removed from the fallback chain. Per-task `routing.{task_type}.model` / `.provider` pins prepend or reorder the chain accordingly.

### Technical

- `TaskRouteOverride` dataclass: `model`, `provider` fields (both optional)
- `VALID_TASK_TYPES`, `VALID_PROFILES`, `VALID_ENFORCE` schema constants for strict YAML validation
- `_merge()` combines two configs — override wins for scalars, lists are unioned

---

## v2.3.0 — Zero-Friction Activation (2026-04-08)

### Added

- **Yearly savings projection** (`src/llm_router/hooks/session-end.py` v14)

  Session-end summary now shows a `📈 Projection: ~$X/year · ~Xk tok/year (based on 7-day avg)` line alongside daily/weekly/monthly cumulative savings, giving a concrete annual ROI signal.

  - `_fmt_tok()` helper: human-readable token counts (e.g. `42k`, `1.3M`)
  - Projection extrapolates from 7-day average when available, falls back to today's rate

- **Monday weekly digest** (`src/llm_router/hooks/session-start.py` v10)

  Fires once per week (on Mondays or after 6-day gap), shows last 7 days: calls, tokens, USD saved, and yearly projection. Writes `~/.llm-router/last_weekly_digest.txt` to prevent repeat firing within the same week.

- **Shadow / suggest / enforce activation modes** (`src/llm_router/hooks/auto-route.py` v15, `enforce-route.py` v4)

  Control routing enforcement level via `LLM_ROUTER_ENFORCE` env var or `.llm-router.yml`:

  | Mode | Behaviour |
  |---|---|
  | `shadow` | Passive `👁 ROUTING OBSERVATION` — no pending state, no blocking |
  | `suggest` | Soft `💡 SUGGESTED ROUTE` hint — pending state written, enforce-route only logs |
  | `enforce` / `hard` | `⚡ MANDATORY ROUTE` — blocks non-routed tool calls (default) |

---

## v2.2.0 — Explainable Routing (2026-04-08)

### Added

- **`LLM_ROUTER_EXPLAIN=1` response prefix** (`src/llm_router/tools/text.py`)

  When set, every routed response (`llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`) is prefixed with a compact routing summary:

  ```
  [→ gemini-2.5-flash · query · $0.00003 · 42.9x cheaper than Sonnet]
  ```

  Shows: model used, task type, per-call cost, and cost ratio vs Sonnet baseline — the "why this model?" answer right in the response stream.

- **`llm_classify` cost comparison table** (`src/llm_router/tools/routing.py`)

  The classification output now includes a "Why not a more expensive model?" breakdown showing Opus/Sonnet/Haiku costs side-by-side with the chosen tier, including a multiplier for each skipped tier (e.g. "↑ 60x more expensive — unnecessary for simple task"). Always shown; no env var required.

- **`reason_code` DB column** (`src/llm_router/cost.py`)

  New column in `routing_decisions` table for storing classification reasoning codes (idempotent migration). `log_routing_decision()` updated with `reason_code: str | None = None` parameter.

- **`router.py` reason_code propagation** — passes `reason_code` from classification metadata to `log_routing_decision()`.

### Technical

- `_explain_prefix()` helper: pure function, zero overhead when env var not set.
- Cost table uses per-1k-output-token pricing — representative of real-world savings signal.
- Routing tip injected into `llm_classify` output when `LLM_ROUTER_EXPLAIN` is not set.

---

## v2.1.0 — Route Simulator + Savings Dashboard (2026-04-08)

### Added

- **`llm-router test "<prompt>"` dry-run CLI** (`src/llm_router/cli.py`)

  Simulates a routing decision for any prompt without making an API call. Uses the existing 5-layer classifier to determine task type, complexity, and confidence, then maps to the cheapest appropriate model and shows an estimated cost vs Sonnet baseline.

  ```
  llm-router test "refactor the auth module to use JWT"
  → Task: analyze / moderate / 85% confidence (via gemini-flash-lite)
  → Chosen: claude-sonnet-4-6  Baseline: claude-sonnet-4-5
  → Saved: $0.00465 (100% cheaper)
  ```

- **`llm_savings` MCP tool** (`src/llm_router/tools/admin.py`)

  Text-based savings dashboard with time-bucketed aggregates: today / this week / this month / all-time. Shows actual spend, Sonnet baseline, savings, and the efficiency multiplier (Nx) — the "wow" metric that makes routing value tangible.

- **DB schema migration v2.1** (`src/llm_router/cost.py`)

  Four new columns added to `usage` table via idempotent `ALTER TABLE` (safe for existing DBs): `baseline_model`, `potential_cost_usd`, `saved_usd`, `is_simulated`.

- **`get_savings_by_period()`** (`src/llm_router/cost.py`) — async savings query used by status bar and `llm_savings`. Falls back to Sonnet estimation for pre-v2.1 rows.

- **Enhanced status bar v3** (`src/llm_router/hooks/status-bar.py`) — D/W savings, provider health icons (auto-hidden until `health.json` active), enforcement mode badge, Nx efficiency multiplier. Full mode via `LLM_ROUTER_STATUS_MODE=full`.

### Fixed

- **Ruff F541** (`src/llm_router/cli.py:1275`) — spurious `f` prefix on string with no placeholders; broke CI lint on both Python 3.11 and 3.12.

---

## v2.0.2 — Fix release CI for Agno tests (2026-04-07)

### Fixed

- **Release and CI test jobs now install the `agno` extra** (`.github/workflows/ci.yml`, `.github/workflows/publish.yml`)

  The workflows were running the full test suite, including `tests/test_agno_integration.py`, but only installing `--extra dev`. That made both CI and tag-based publish fail before release because `agno` was missing. Both workflows now install `uv sync --extra dev --extra agno` before running tests.

## v2.0.1 — Harder Claude Code routing enforcement (2026-04-07)

### Changed

- **Release metadata now matches the shipped hook changes** (`pyproject.toml`, `uv.lock`)

  The package version is now `2.0.1`, so the existing tag-based publish workflow can cut a real release for the Claude Code enforcement changes already merged on `main`.

- **Routing enforcement now defaults to `hard` in Claude Code hooks** (`src/llm_router/hooks/enforce-route.py`)

  `LLM_ROUTER_ENFORCE` now defaults to `hard` instead of `soft`. When `auto-route.py` issues a `⚡ MANDATORY ROUTE` directive and Claude tries to jump straight to `Bash`, `Write`, `Edit`, or `MultiEdit`, the `PreToolUse` hook blocks that work by default instead of merely logging it.

- **Missed routed turns are now surfaced on the next prompt** (`src/llm_router/hooks/auto-route.py`)

  Claude Code still has no hook that fires immediately before a plain text response, so same-turn self-answering cannot be blocked directly. To make those misses visible, `auto-route.py` now detects a leftover `pending_route_{session_id}.json` from the prior turn, logs it as `NO_ROUTE` in `~/.llm-router/enforcement.log`, clears the stale state, and injects a warning into the next `⚡ MANDATORY ROUTE` context.

- **Installer and README now document hard-by-default behavior** (`src/llm_router/tools/setup.py`, `README.md`)

  Post-install messaging now tells users that routed work is blocked by default unless they explicitly set `LLM_ROUTER_ENFORCE=soft` or `off`.

- **Demo outputs now default to an ignored folder and repo noise was removed** (`demo/app_builder_demo.py`, `demo/saas_builder_demo.py`, `.gitignore`)

  The demo scripts now write reports to `demo/output/` by default, and the repo no longer tracks generated demo reports, Finder metadata, or stray root-level screenshots.

### Added

- **Hook regression tests for enforcement behavior** (`tests/test_route_enforcement_hooks.py`)

  Covers:
  - hard-default blocking of work tools
  - soft-mode override still logging violations
  - carry-over logging for unrouted previous turns

## v2.0.0 — Agno integration: RouteredModel + RouteredTeam (2026-04-07)

### Added

- **`RouteredModel` — drop-in Agno model with smart routing** (`src/llm_router/integrations/agno.py`)

  Use llm-router as a first-class Agno model. Every agent call is classified by complexity and routed to the cheapest capable provider (Ollama → Codex → paid APIs).

  ```python
  from llm_router.integrations.agno import RouteredModel
  from agno.agent import Agent

  agent = Agent(
      model=RouteredModel(task_type="code", profile="balanced"),
      instructions="You are a coding assistant.",
  )
  agent.print_response("Write a Python quicksort.")
  ```

  Parameters: `task_type` (query/research/generate/analyze/code/image/video/audio), `profile` (budget/balanced/premium), `model_override` (pin a specific model). Accepts both string values and enum instances.

- **`RouteredTeam` — multi-agent team with shared budget enforcement** (`src/llm_router/integrations/agno.py`)

  Agno `Team` subclass that automatically downshifts all `RouteredModel` members to the `budget` routing profile when monthly spend reaches a configurable threshold.

  ```python
  from llm_router.integrations.agno import RouteredModel, RouteredTeam

  team = RouteredTeam(
      members=[coder_agent, researcher_agent],
      monthly_budget_usd=20.0,
      downshift_at=0.80,  # downshift when 80% of budget spent
  )
  ```

  Budget check runs before each `run()` / `arun()` call. Non-fatal: if the cost database is unavailable, the team continues without downshifting.

- **`agno` optional dependency group** — install with `pip install "claude-code-llm-router[agno]"`.

- **19 new tests** (`tests/test_agno_integration.py`) covering model construction, invoke/ainvoke/stream, multi-turn message handling, token metadata, budget pressure downshifting, and team integration.

---

## v1.9.4 — Fix: content filter silent fallback + model_override subscription bypass (2026-04-07)

### Fixed

- **Content filter errors surfaced to user as scary warnings** — When Anthropic (or any provider) returned a `400 "Output blocked by content filtering policy"` error, the router was treating it as a generic failure: showing a warning notification to the user AND tripping the circuit breaker, penalising the provider for what is a content policy decision, not an infrastructure failure. Users saw the raw API error even though the router was successfully falling back to the next model.

  **Fix**: Added `_is_content_filter_error()` detection for content filtering markers. These errors are now **silently skipped** — no user-visible warning, no circuit breaker trip — so the router tries the next model transparently.

- **`model_override` bypassed subscription mode filter** — When a caller (tool or hook) passed an explicit `model="anthropic/claude-haiku-4-5-20251001"` override, the `available_providers` filter was never applied. In subscription mode this meant an Anthropic API call was attempted despite `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`.

  **Fix**: When `model_override` starts with `anthropic/` and subscription mode is active, the override is silently replaced with the balanced chain (Anthropic models excluded), logged at WARNING level.

---

## v1.9.3 — Fix: actively purge ANTHROPIC_API_KEY from live env in subscription mode (2026-04-07)

### Fixed

- **v1.9.2 fix was incomplete for already-running servers** — `apply_keys_to_env()` now skips exporting `ANTHROPIC_API_KEY` in subscription mode, but the currently running MCP server had already exported it at startup. Since the MCP server is a long-lived process, any `ANTHROPIC_API_KEY` already in `os.environ` before my fix persisted and LiteLLM continued using it.

  **Fix**: `get_config()` now actively calls `os.environ.pop("ANTHROPIC_API_KEY", None)` on every invocation when `llm_router_claude_subscription=True`. This purges the key from the live environment regardless of how or when it got there — covering pre-existing keys, keys injected by the shell, and keys exported before the server started.

---

## v1.9.2 — Fix: block ANTHROPIC_API_KEY export in subscription mode (2026-04-07)

### Fixed

- **Anthropic API still called in subscription mode** — `apply_keys_to_env()` was unconditionally exporting `ANTHROPIC_API_KEY` into `os.environ` at MCP server startup, even when `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`. LiteLLM reads keys directly from the environment at call time, so any code path (routing chain, classifier, tool fallback) that encountered an `anthropic/*` model would successfully make a direct API call — billing separately from the Claude Code subscription.

  The `available_providers` filter (which correctly excludes `anthropic` in subscription mode) guarded the main routing chains, but was insufficient as a sole defense because LiteLLM's environment-level key meant the call could still succeed if a model slipped through any bypass.

  **Fix**: `apply_keys_to_env()` now skips exporting `ANTHROPIC_API_KEY` when `llm_router_claude_subscription=True`. This is a hard guarantee: LiteLLM cannot call Anthropic at all in subscription mode, regardless of which model is requested.

---

## v1.9.1 — Fix: llm_select_agent codex detection (2026-04-06)

### Fixed

- **`llm_select_agent` reported `codex_binary: false` when the Codex CLI was installed** — the tool called `is_codex_plugin_available()` (which checks for the openai/codex-plugin-cc Claude Code plugin directory) instead of `is_codex_available()` (which checks for the actual `codex` binary at known paths and `$CODEX_PATH`). Users with Codex at `/usr/local/bin/codex` got `claude_code` as primary instead of `codex` even on budget profile.

---

## v1.9.0 — Routing enforcement + session-level agent selection (2026-04-06)

### Added

- **`enforce-route.py` hook (PreToolUse, all tools)** — hard-enforces routing compliance. When `auto-route.py` issues a `⚡ MANDATORY ROUTE` directive, it writes a session-scoped pending state file (`~/.llm-router/pending_route_{session_id}.json`). The new `enforce-route.py` hook fires before every tool call:
  - If Claude calls an `llm_*` tool → routing honored, state cleared, allow.
  - If the tool is context-gathering (Read, Glob, Grep, LS, NotebookRead) → always allow.
  - If a work tool (Write, Edit, MultiEdit, Bash) fires before routing → enforce per `LLM_ROUTER_ENFORCE`:
    - `soft` (default) — log violation to `~/.llm-router/enforcement.log`, allow the call.
    - `hard` — block the call with a remediation message telling Claude to call the routing tool.
    - `off` — disable enforcement entirely.
  - State expires after 5 minutes to avoid stale blocks.

- **`llm_select_agent` MCP tool** — session-level agent routing for orchestrators like claw-biz. Given a task prompt and profile, returns which agent CLI (claude_code / codex) + model to invoke for the whole session — before starting, not mid-session. Decision tree:
  ```
  budget   + simple/moderate → codex     + gpt-4o-mini
  budget   + complex         → codex     + gpt-4o
  balanced + simple          → codex     + gpt-4o-mini
  balanced + moderate        → claude_code + sonnet
  balanced + complex         → claude_code + opus
  premium  + any             → claude_code + opus
  research (any profile)     → claude_code + sonnet (needs web access)
  ```
  Returns JSON with primary agent, model, fallback, env_check, and a ready-to-run CLI invocation hint.

### Addresses GitHub Issues

- [Issue #1](https://github.com/ypollak2/llm-router/issues/1) — CC-MODE `/model` slash commands silently ignored: **fixed in v1.8.4**, upgrade resolves it (`pip install --upgrade claude-code-llm-router`).
- [Issue #2](https://github.com/ypollak2/llm-router/issues/2) — Hard-enforce routing directives: implemented via `enforce-route.py` + `LLM_ROUTER_ENFORCE`.
- [Issue #3](https://github.com/ypollak2/llm-router/issues/3) — `llm_select_agent` session-level routing classifier: implemented as new MCP tool.

### Hook versions

- `auto-route.py`: v11 → v12 (writes pending state for enforcement)
- `enforce-route.py`: v1 (new)

---

## v1.8.5 — Persistent statusline stats bar (2026-04-06)

### Changed

- **Statusline: persistent `📊` stats bar** — the Claude Code bottom status bar now shows the llm-router savings stats continuously instead of the old `🔀 last-model · HH:MM · Ntok` last-call indicator. The new display persists across the whole session:
  ```
  …/Projects/my-app  main | claude-sonnet-4-6 | 📊  CC 13%s · 24%w · 43%♪   │   sub:0 · free:15 · paid:27   │   $0.008 saved (29%)
  ```
  `%s` = session · `%w` = weekly · `%♪` = Sonnet monthly · `sub/free/paid` = call counts

- The statusline now calls `llm-router-status-bar.py` directly (the same hook that fires before every prompt) so the data is always fresh.

### How to apply

If you installed llm-router before this release, re-run the statusline installer or update `~/.claude/statusline-command.sh` to call the status-bar hook:

```bash
llm-router install          # re-installs all hooks and updates statusline
```

---

## v1.8.4 — Fix: remove broken /model directives, always route via MCP tools (2026-04-06)

### Fixed

- **CC-MODE `/model` directives never worked** — the hook was emitting `⚡ MANDATORY ROUTE: query/simple → /model claude-haiku-4-5-20251001 (subscription)` but Claude Code's model cannot execute slash commands from hook context (neither interactive nor `claude -p` mode). Every CC-MODE simple/complex routing directive was silently ignored. Removed the entire `/model` directive path.
- **All routing now goes via MCP tools** — `simple` → `llm_query`, `moderate` → `llm_analyze`/`llm_generate`/`llm_code`, `complex` → `llm_code`/`llm_analyze`. The free-first chain (Ollama → Codex → cheap API) keeps costs low in both subscription and API-key modes.
- **`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`** now only enables inline OAuth refresh and session-end delta reporting — it no longer changes routing behaviour (routing was broken anyway).
- **Session-start banner updated** to describe actual MCP-tool routing instead of the broken `/model` model-switch table.

### Hook versions

- `auto-route.py`: v10 → v11
- `session-start.py`: v7 → v8

## v1.8.3 — Critical: fix routing format drift + MCP CLI registration (2026-04-06)

### Fixed

- **Hook format drift (routing silently ignored)** — `auto-route.py` was emitting `⚡ ROUTE→tool(...)` but the bundled rules file told Claude to look for `⚡ MANDATORY ROUTE:`. The mismatch caused Claude to treat every routing directive as informational text and ignore it, defaulting to Opus for all tasks. All tokens, zero routing. Fixed: hook now emits the canonical `⚡ MANDATORY ROUTE: {task_type}/{complexity} → call {tool}({args})` format.
- **MCP server not visible to `claude -p`** — `llm-router install` was writing `mcpServers` to `~/.claude/settings.json` (Claude Desktop location) but not to `~/.claude.json` (Claude Code CLI location). After a fresh install, running `claude mcp list` showed no llm-router entry, and `claude -p` couldn't call any `llm_*` tools. Fixed: `install()` now also registers in `~/.claude.json` via `claude mcp add --scope user` (with a direct JSON merge fallback for headless/Docker environments without the `claude` CLI).
- **Rules file updated** — `llm-router.md` bumped to version 4 with accurate format examples showing the `(complexity="...")` argument syntax.

### Hook version

- `auto-route.py`: v9 → v10 (format fix)

## v1.8.2 — Docker/agent headless mode (2026-04-06)

### Added

- **`llm-router install --headless`** — installs for Docker/CI/agent environments (API-key mode). Prints a complete Dockerfile snippet + settings.json merge example for wiring llm-router into a Claude Code agent container.
- **Dynamic session-start banner** — `session-start.py` now auto-detects the routing mode from `LLM_ROUTER_CLAUDE_SUBSCRIPTION`. When not set (API-key / Docker mode), shows "API-key routing in effect" banner with free-first chain instead of "subscription routing" + wrong pressure cascade info.
- **Skip OAuth on Linux/Docker** — `session-start.py` skips the `_refresh_claude_usage()` OAuth call when `LLM_ROUTER_CLAUDE_SUBSCRIPTION` is not set, eliminating noisy "Keychain not found" warnings in agent containers.

### How to use in Docker/K8s agents

```dockerfile
RUN pip install claude-code-llm-router && llm-router install --headless
# Pass API keys at runtime via K8s secret (do NOT set LLM_ROUTER_CLAUDE_SUBSCRIPTION):
# GEMINI_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, DEEPSEEK_API_KEY
```

## v1.8.1 — Fix: inline OAuth refresh prevents session exhaustion (2026-04-06)

### Fixed

- **Critical routing bug: session hitting 100%** — `auto-route.py` was reading stale `usage.json` and making routing decisions based on hours-old pressure data. When `usage.json` was >30 min old AND last known session pressure was ≥70%, the hook never triggered the pressure cascade to external providers, letting the Claude subscription session exhaust to 100%.

  **Root cause**: Subscription usage is monotonically increasing within a 5h window. Stale data always underestimates pressure. At 70%+ pressure, this underestimate caused catastrophic under-routing.

  **Fix**: `auto-route.py` now attempts an **inline OAuth refresh** (macOS Keychain + Anthropic API) when `usage.json` is stale AND last known session ≥ 70%. Refresh is rate-limited to once per 2 minutes to avoid API hammering. After the refresh, routing decisions use fresh pressure data — ensuring the cascade to external providers fires at the correct 95% threshold.

## v1.8.0 — claw-code hook install (2026-04-06)

### Added

- **`llm-router install --claw-code`** — installs hooks and MCP server into `~/.claw-code/settings.json`; auto-detects claw-code if present during regular `llm-router install`
- **claw-code-adapted hooks** — `session-end-clawcode.py` and `status-bar-clawcode.py` omit the Claude Code subscription pressure sections (claw-code has no Anthropic OAuth; every call is a paid API call)
- **`llm-router doctor`** — new section checks claw-code hook status and MCP registration when `~/.claw-code/` is detected
- **`install_claw_code()` / `uninstall_claw_code()`** in `install_hooks.py` — programmatic API for claw-code integration; detects `~/.claw-code/` with XDG fallback

### Fixed

- **CI lint failures** — removed unused `FREE_PROVIDERS` and `since_iso` variables in `cli.py`; fixed f-strings without placeholders in `session-end.py`

## v1.7.0 — Multi-harness docs: claw-code, OpenClaw, Agno (2026-04-06)

### Added

- **claw-code MCP snippet** in README — open-source Rust Claude Code alternative; same hook/MCP protocol, no subscription (API-only users save even more)
- **OpenClaw MCP snippet** in README — `openclaw mcp add llm-router` one-liner for the Skills marketplace
- **Agno MCP example** in README — `MCPTools(command="llm-router")` pattern; teaser for v2.0 `RouteredModel` drop-in
- **v1.7–v2.1 roadmap sections** — documented multi-platform integration strategy (claw-code hooks, OpenClaw skill, Agno adapter, Learning Router)
- **`docs/plans/v2.0-ecosystem-integrations.md`** — full integration architecture doc with 4-layer model, per-system breakdowns, `RouteredModel`/`RouteredTeam` sketches, and phase plan

## v1.6.0 — `llm-router share` + star CTA (2026-04-06)

### Added

- **`llm-router share`** — generates a savings card (box-drawn ASCII), copies it to the clipboard, and opens a pre-filled Twitter/X tweet with your real savings numbers. Works on macOS (`pbcopy`), Linux (`xclip`/`xsel`), and Windows (`clip`).
- **Star CTA in session-end hook** — the first time your lifetime savings crosses $0.50, the Stop hook shows a one-time prompt: `⭐ Enjoying the savings? A star on GitHub helps others find it`. Never shown again after that session. Regular sessions show `run llm-router share to post it` instead.

## v1.5.2 — Call breakdown in status bar (2026-04-06)

### Changed

- **Status bar now shows call breakdown** — the `UserPromptSubmit` status bar now displays `sub:N · free:N · paid:N` (Claude subscription / Ollama+Codex / paid API) alongside the savings figure. Example: `📊  CC 33%s · 25%w · 45%♪   │   sub:0 · free:15 · paid:27   │   $0.008 saved (29%)`

## v1.5.1 — Free-model savings in session-end summary (2026-04-06)

### Added

- **Session-end summary shows free-model savings** — Ollama and Codex calls are now separated from paid external calls in the stop hook output. A new "Free models" section shows per-provider call counts, token volumes, and savings vs Sonnet baseline. Codex savings estimated from avg tokens/call when token counts aren't tracked. The combined savings tip (`💡 Saved ~$X.XX`) now includes both paid routing savings and free-model savings.

## v1.5.0 — Filesystem tools + free-model savings (2026-04-06)

### Added

- **`llm_fs_find` MCP tool** — describe files to find in natural language; cheap model (Haiku/Ollama) generates glob patterns and grep commands. Use with Claude's Glob/Grep tools for zero-Opus file discovery.
- **`llm_fs_rename` MCP tool** — describe a rename/reorganise operation; cheap model returns `mv`/`git mv` commands. `dry_run=True` (default) prefixes with `echo` for safe review before execution.
- **`llm_fs_edit_many` MCP tool** — bulk edit across multiple files. Accepts a file list or glob pattern; reads all files locally (free), sends to moderate-tier model, returns `{file, old_string, new_string}` JSON for mechanical application.
- **Free-model savings in `llm-router status`** — new "Free-model savings" section shows Ollama and Codex calls separately: call count, token volume, and estimated savings vs Sonnet-3.5 API rates. Codex token savings are estimated from average paid-provider tokens/call when exact counts aren't available.

## v1.4.2 — Dashboard data fixes, animated SVG demo (2026-04-06)

### Fixed

- **Dashboard savings gauge now reads real data** — was querying the empty `savings_stats` table; now calculates savings from `usage` table using the Sonnet baseline formula (same as `llm-router status`). Lifetime savings and efficiency gauge now show correct values.
- **Dashboard "Recent Routed Traffic" now populated** — was querying the empty `routing_decisions` table; now reads from `usage` table. Rows appear immediately after first routing call.
- **Dashboard version string** — was hardcoded `v1.3`; now comes from `importlib.metadata` at runtime and displays the actual installed package version.

### Added

- **Animated SVG demo in README** — replaces the static `demo.png` with a crisp CSS-animated SVG generated from a synthetic asciinema cast (`docs/demo.cast`). Shows `llm-router demo` (routing table, color-coded complexity) then `llm-router status` (pressure bars, savings chart) in a 10-second loop. Regenerate with `python scripts/gen_cast.py`.

## v1.4.1 — Smarter demo, real routing history, uninstall --purge (2026-04-05)

### Added

- **`llm-router demo` — real routing history**: when `~/.llm-router/usage.db` has external routing calls, `demo` now shows your actual last 8 routing decisions (prompt snippet, task type, complexity, model, cost) instead of static examples. Falls back to examples with "(no routing history yet — showing examples)" when DB is empty.
- **`llm-router uninstall --purge`**: removes hooks and MCP registration (existing behaviour), then optionally deletes `~/.llm-router/` (usage DB, `.env`, logs). Prompts for confirmation before deleting; cancels if user types anything other than `yes`.

### Fixed

- Removed unused `import sqlite3` in `_run_demo()` (ruff F401).

## v1.4.0 — Real savings dashboard, update command, Linux/Windows compat (2026-04-05)

### Added

- **`llm-router status` — real cumulative savings**: now shows today / 7-day / 30-day / all-time savings with ASCII bar charts (green = saved, yellow = spent), top models used, and colored subscription pressure bars.
- **`llm-router update`**: re-installs hooks and routing rules to the latest bundled version, then checks PyPI for a newer package version with upgrade hint.
- **Linux/Windows compatibility**: `dst.chmod(0o755)` is now skipped on Windows; hook `command` now uses `sys.executable` (the running Python interpreter) instead of the hardcoded `python3`, ensuring hooks work in pipx/venv/pyenv setups on all platforms.

### Fixed

- **CI no longer hangs**: added `timeout-minutes: 10` to CI job and `--timeout=30` per-test via `pytest-timeout`; added `timeout = 30` to `pyproject.toml` pytest config as local default.

## v1.3.9 — High-quality demo screenshot (2026-04-05)

### Changed

- Replaced vhs GIF (unreadable font) with a Chrome-rendered PNG (`docs/images/demo.png`) — crisp SF Mono, Dracula theme, 2× resolution, no username in paths.
- README now shows the PNG demo image; PyPI page updated accordingly.

## v1.3.8 — Improved demo GIF and table layout (2026-04-05)

### Changed

- Demo tape now uses plain `llm-router` (no hardcoded user path); terminal widened to 1100px so output never wraps.
- `llm-router demo` table: slimmer column widths (prompt 44, task 8, complexity 12, model 18, cost 9), cleaner model names (`Claude Haiku` / `Claude Sonnet` / `Claude Opus` instead of `Haiku (sub)` etc.).
- Regenerated `docs/images/demo.gif`.

## v1.3.7 — Friendly auth error messages (2026-04-05)

### Fixed

- **Authentication errors now show actionable hints** — when a provider returns a 401 (missing/invalid API key), the router emits a clear message naming the exact env var to set (`GEMINI_API_KEY`, `OPENAI_API_KEY`, etc.) and explains that Claude Code subscription covers Haiku/Sonnet/Opus without an API key. Previously these surfaced as raw LiteLLM exception text.
- "All models failed" terminal error now suggests `llm-router setup` when the root cause was auth, vs. `llm_health()` for other failures.

## v1.3.6 — Demo GIF, Ruff fixes (2026-04-05)

### Added

- **Demo GIF** (`docs/images/demo.gif`) — generated via VHS; embedded at top of README showing `demo`, `doctor`, and `status` commands in action.

### Fixed

- Removed three unused imports/variables in `cli.py` (`_CLAUDE_DIR`, two `rules_src` assignments) that caused ruff F401/F841 CI failures.

## v1.3.5 — Setup Wizard, Demo, Deep Reasoning (2026-04-05)

### Added

- **`llm-router setup`** — interactive wizard: walks through Claude subscription mode + optional provider API keys (Gemini, Perplexity, OpenAI, Groq, DeepSeek, Mistral, Anthropic), writes to `~/.llm-router/.env`, offers to run `llm-router install` at the end.
- **`llm-router demo`** — shows a table of routing decisions for 7 sample prompts against your active config (subscription mode, which providers are set), with savings estimate vs always-Opus. Color-coded by complexity, ANSI-aware column alignment.
- **`deep_reasoning` complexity tier** — new complexity value above `complex`: triggers extended thinking on Claude models (`thinking={"type": "enabled", "budget_tokens": 16000}` via LiteLLM `extra_params`). Routes to PREMIUM chain. Classifier system prompt updated to recognize it. Auto-route hook heuristics detect formal proofs, first-principles derivation, theorem proving, and philosophical analysis.

### Changed

- `auto-route.py` hook bumped to v8 (deep_reasoning heuristics).
- `ClassificationResult.header()` and `RoutingRecommendation.header()` show `[D]` tag for deep_reasoning.
- `COMPLEXITY_BASE_MODEL` and `COMPLEXITY_ICONS` include `deep_reasoning` entry.

## v1.3.4 — Trendiness & Usability (2026-04-05)

### Added

- **`llm-router doctor`** — comprehensive health check command: verifies hooks, routing rules, Claude Code MCP registration, Claude Desktop registration, Ollama reachability (with model list), usage data freshness, API keys, and installed version. Prints colored ✓/✗/⚠ results with copy-paste fix commands.
- **Cursor / Windsurf / Zed install snippets** in README — the router works in any MCP-compatible IDE; Quick Start now includes ready-to-paste config blocks for Cursor (`~/.cursor/mcp.json`), Windsurf (`~/.codeium/windsurf/mcp_config.json`), and Zed (`settings.json`).
- **Colored `install --check` output** — `✓`/`✗`/`⚠` symbols with ANSI colors (respects `NO_COLOR` and non-tty); broken items show a `→ fix command` hint inline.
- **Better first-run install message** — after `llm-router install`, shows a "Try it" prompt to test routing immediately and lists all subcommands including the new `doctor`.

### Changed

- `llm-router status` subcommand list now includes `llm-router doctor`.

## v1.3.3 — Visibility & Usability (2026-04-05)

### Added

- **Visible routing indicator** (`auto-route.py`) — terminal now shows `⚡ llm-router → {tool} [{task_type}/{complexity} · {method}]` each time the hook fires. Users can see routing happen in real time instead of it being silent.
- **Shareable savings line** (`session-end.py`) — session summary now prints `💡 Saved ~$X.XX with llm-router · github.com/ypollak2/llm-router` when external savings exceed $0.001.
- **`llm-router status` command** — new CLI subcommand showing Claude subscription pressure, today's external routing calls/cost/savings, and top models used, all from local state files (no network calls).
- **Smithery listing** (`smithery.yaml`) — one-click install via Smithery MCP marketplace with full `configSchema` and `commandFunction`.
- **PyPI download badge + Smithery badge** in README.
- **Zero-config pitch** in README Quick Start — prominently explains the router works with just a Claude Code subscription, no API keys required.
- **`pipx` one-line install** in README — `pipx install claude-code-llm-router && llm-router install`.

### Changed

- `auto-route.py` hook version bumped to 7; `session-end.py` to 9.

## v1.3.2 — Distribution & Install (2026-04-05)

### Added

- **Claude Desktop auto-install** (`install_hooks.py`) — `llm-router install` now writes the MCP server entry to `claude_desktop_config.json` on macOS, Windows, and Linux. Safe merge — never overwrites unrelated entries. `uninstall` removes it cleanly.
- **API key validation on install** — `llm-router install --check` and post-install output now show which provider API keys are set and warn when no external providers are configured.
- **Automated PyPI publish CI** (`.github/workflows/publish.yml`) — pushes to `v*` tags trigger: test suite → version verification → `uv build` → PyPI publish. Blocks bad releases by running tests first.
- **PyPI discoverability keywords** — added `llm-router`, `claude-desktop`, `cost-optimization`, `model-routing`, `mcp-server` to package metadata.
- **Glama MCP registry listing** (`glama.json`) — full tool/environment/resource metadata for Glama and compatible registries.

### Fixed

- **sdist bloat** — excluded `.claude/`, `.serena/`, `.playwright-mcp/`, screenshots, and dev files from the source distribution (331 KB vs 28 MB previously).
- **aiosqlite teardown warning** — added targeted `filterwarnings` for the benign `call_soon_threadsafe` race in pytest-asyncio function-scoped loops.
- **Hook absolute paths** — `.claude/settings.json` project hooks now use absolute paths, preventing `ENOENT` failures when Claude is opened from a different directory.

### Changed

- `mcp-registry.json` version bumped to `1.3.2`.

## v1.3.0 — Observability (2026-04-04)

### Added

- **Anthropic prompt caching** (`prompt_cache.py`) — auto-injects `cache_control: {"type": "ephemeral"}` breakpoints on long stable context before every Anthropic model call, saving up to 90% on cached token reads. Two breakpoints are placed at the most cache-effective positions: the system message (if ≥1024 tokens) and the last context message before the current user turn. Non-Anthropic models pass through unchanged. Activated by default; controlled by `LLM_ROUTER_PROMPT_CACHE_ENABLED` (bool) and `LLM_ROUTER_PROMPT_CACHE_MIN_TOKENS` (int, default 1024).

- **Hard daily spend cap** (`router.py`) — `LLM_ROUTER_DAILY_SPEND_LIMIT` (float, default 0 = disabled) now raises `BudgetExceededError` before any LLM call when daily spend ≥ limit. Checked inside the existing `_budget_lock` alongside the monthly cap so concurrent callers can't both slip past. Error message includes the reset time (midnight UTC) and the env var to raise the limit.

- **Semantic dedup cache** (`semantic_cache.py`) — embeds prompts via Ollama's `nomic-embed-text` model and skips the LLM call entirely when a recent response (within 24h, same task type) has cosine similarity ≥ 0.95. Returns a zero-cost `LLMResponse` with `provider="cache"`. New `semantic_cache` table added to the usage SQLite DB via `CREATE TABLE IF NOT EXISTS` (existing DBs unaffected). Only active when `OLLAMA_BASE_URL` is set; silently no-op otherwise.

- **Web dashboard** (`dashboard/`) — `llm-router dashboard [--port N]` starts a local `aiohttp` HTTP server at `localhost:7337`. Also accessible via the `llm_dashboard` MCP tool. Shows: today's calls/cost/tokens, monthly spend, lifetime savings vs Sonnet baseline, model and task-type distribution (7 days), daily cost trend (14 days), recent routing decisions table, and session quota. Auto-refreshes every 30 seconds. Self-contained single-file HTML — no build step. All DB values rendered via `textContent`/Chart.js arrays (no `innerHTML` XSS surface).

### Fixed

- **Cross-platform desktop notifications** (`cost.py`) — `fire_budget_alert` now dispatches to `osascript` (macOS), `notify-send` (Linux), or `win10toast` (Windows, optional). Previously macOS-only; alerts were silently dropped on Linux and Windows.

- **Dashboard background process** (`tools/admin.py`) — `llm_dashboard` uses `start_new_session=True` on macOS/Linux and `DETACHED_PROCESS` on Windows, ensuring the dashboard survives terminal close on all platforms.

## v1.2.0 — Foundation Hardening (2026-04-02)

### Changed

- **`server.py` decomposed into `tools/` modules** — the 2,328-line monolith is now a 110-line thin entrypoint. All 24 MCP tools live in 8 focused modules: `routing.py`, `text.py`, `media.py`, `pipeline.py`, `admin.py`, `subscription.py`, `codex.py`, `setup.py`. Each module exports `register(mcp)`. Backward-compatible: all imports from `llm_router.server` still work.
- **`state.py` module** — shared mutable state (`_last_usage`, `_active_profile`) extracted from `server.py` into a dedicated module with `get_*`/`set_*` accessors, eliminating circular import risk across tool modules.

### Added

- **`llm-router install` subcommand** — the main `llm-router` CLI now accepts `install`, `install --check`, `install --force`, and `uninstall` subcommands. Running `llm-router` without arguments still starts the MCP server (unchanged behavior). The `--check` flag previews what would be installed; `--force` updates paths even if already registered.
- **`mcp-registry.json`** — registry manifest at repo root for `registry.modelcontextprotocol.io` submission, listing all 18 primary tools, 1 resource, and 2 hooks with descriptions.

## v1.1.0 — Subscription-Aware Routing + Observability (2026-04-01)

### Added

- **Subscription-aware MCP tools** (`server.py`) — `llm_query`, `llm_code`, `llm_research`, and `llm_analyze` now return a `⚡ CC-MODE:` hint when Claude Code subscription has headroom, directing Claude to switch model tier (`/model haiku` / `/model opus`) instead of making an external API call. External calls are only made when the relevant pressure threshold is exceeded (session ≥ 85% for simple, sonnet ≥ 95% for moderate, weekly ≥ 95% for complex).
- **Ollama live reachability probe** (`config.py`) — `ollama_reachable()` does a TCP socket check before marking Ollama available. Previously the health endpoint reported "healthy" even when the Ollama server was unreachable. The probe result is cached for 30 seconds to avoid per-call overhead.
- **E2E demo test suite** (`tests/test_demo_routing.py`, `tests/test_demo_session_summary.py`) — Two demo files doubling as executable documentation: 7 tests covering the full routing pipeline (CC hints, pressure cascade, Ollama health probe) and 4 tests covering the session-end hook (subprocess output, savings math, empty session, model name truncation).

### Fixed

- **Session-end hook Stop schema** (`hooks/session-end.py` v5) — Hook was wrapping output in `hookSpecificOutput` which is invalid for Stop events. Output is now `{"systemMessage": "..."}` at the top level, matching the Stop hook schema. Previously caused "JSON validation failed" errors at session end.
- **"improve ... performance" misrouted as query** (`hooks/auto-route.py`) — The code heuristic only matched `optimize` but not `improve`, causing "improve the database query performance" to fall through to Ollama and get classified as `query/simple`. Now matches both.
- **Session-end hook simplification** (`hooks/session-end.py` v4→v5) — Removed per-tool bar chart and verbose labels (60 lines). Summary is now a compact table: calls × model × cost, with total savings % vs Sonnet baseline.

## v1.0.0 — Production Stable: Bug Fixes & Routing Integrity (2026-03-31)

### Fixed (Critical)

- **Subscription flag now enforced** (`config.py`) — `available_providers()` previously had a comment saying anthropic was excluded in subscription mode, but never applied the filter. Now `providers.discard("anthropic")` is called when `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`, even if `ANTHROPIC_API_KEY` is set. Prevents accidental double-billing via API when already inside Claude Code.
- **`llm_route` uses unified pressure-aware profile** (`server.py`) — `llm_route` previously derived its routing profile from complexity alone, while `llm_classify` used `select_model()` (which applies budget pressure). Now both tools share the same decision path: `select_model()` is called in `llm_route` and its downshifted profile is used when pressure ≥ 85%. Eliminates the two-path divergence where the same prompt could route differently depending on which tool was called.

### Fixed (Moderate)

- **Pressure data staleness warnings in 3 hooks** — `auto-route.py`, `subagent-start.py`, and `agent-route.py` now check if `usage.json` is older than 30 minutes. When stale, a visible warning is appended to the routing directive/context. Previously all three hooks made routing decisions on potentially hours-old quota data with no indication to the user.
- **Ollama comment corrected** (`config.py` lines 55–65) — Updated outdated comment that claimed Ollama is "ONLY used for BUDGET tier". Ollama is also injected at pressure ≥ 85% for any profile. Comment now documents both injection scenarios and the OLLAMA_URL vs OLLAMA_BASE_URL separation.
- **`llm_set_profile` no longer mutates frozen config** (`server.py`) — Replaced `object.__setattr__(config, ...)` hack (which bypassed Pydantic's immutability) with a module-level `_active_profile` variable. A `get_active_profile()` helper returns the override or config default. Immutability contract is now preserved end-to-end.

### Fixed (Minor)

- **Atomic count write in `usage-refresh.py`** — `_write_count()` now writes to a `.tmp` file then renames atomically via `os.replace()`. Prevents concurrent PostToolUse hooks from corrupting the routed-call counter via interleaved reads/writes.
- **Health-aware classifier model ordering** (`classifier.py`) — Classifier model candidates are now sorted: healthy providers first, then by static list order. Unhealthy providers (circuit breaker open) are tried last instead of first, reducing classification latency when a provider is down.

### Changed

- `pyproject.toml`: Development status updated from `4 - Beta` to `5 - Production/Stable`.
- `tests/conftest.py`: `mock_env` fixture now explicitly sets `OLLAMA_BASE_URL=""` to disable Ollama in unit tests (mirrors existing Codex disable pattern). Prevents test failures when `OLLAMA_BASE_URL` is set in project `.env`.
- `tests/test_router.py`: `test_no_providers_configured` now clears all API keys explicitly (including shell env keys) for deterministic behavior.

---

## v0.9.2 — Claude Code Subscription Mode (2026-03-31)

### Added

- **Claude Code subscription mode** (`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`) — All Claude tiers (Haiku, Sonnet, Opus) are now accessed via the Claude Code subscription at zero API cost. No Anthropic API key required or used.
- **Tiered pressure cascade** — Three independent quota buckets each control a different complexity tier, cascading downward when pressure rises:
  - `session ≥ 85%` → simple tasks switch to external (Gemini Flash / Groq)
  - `sonnet ≥ 95%` → moderate tasks switch to external (GPT-4o / DeepSeek)
  - `weekly ≥ 95%` OR `session ≥ 95%` → all tiers go external (global emergency)
- **Per-bucket pressure tracking** — `auto-route.py` and `agent-route.py` now read `{session_pct, sonnet_pct, weekly_pct}` from `usage.json` instead of the single `highest_pressure` field. More granular and accurate fallback decisions.
- **Haiku for simple tasks** — Simple tasks emit a `/model claude-haiku-4-5-20251001` hint instead of routing to cheap external APIs. Haiku is free via subscription and avoids network latency.
- **Passthrough for moderate tasks** — Moderate tasks with no pressure are a no-op (Sonnet handles directly). No model switching, no external call.
- **Opus for complex tasks** — Complex tasks emit a `/model claude-opus-4-6` hint. Best quality, zero API cost while subscription quota is available.
- **Haiku as preferred complexity classifier** — `CLASSIFIER_MODELS` now lists `anthropic/claude-haiku-4-5-20251001` first. Skipped automatically when no `ANTHROPIC_API_KEY` is set; Gemini Flash / Groq serve as instant fallbacks.
- **Configurable `media_request_timeout`** — New `LLM_ROUTER_MEDIA_REQUEST_TIMEOUT` env var (default: 600s). Video generation can take several minutes; previously the 120s `request_timeout` caused false failures.
- **`asyncio.Lock` for budget enforcement** — Budget check in `router.py` is now wrapped in `_budget_lock`. Prevents concurrent requests both passing the monthly budget cap check (race condition when two tasks fire simultaneously near the limit).
- **90% budget soft-warning** — Logs a `WARNING` at 90% monthly spend, before the hard stop at 100%.

### Changed

- `auto-route.py` → version 5: pressure default changed from `0.3` (conservative) to `0.0` (no pressure assumed when `usage.json` absent). Subscription models are preferred when blind — no unnecessary external routing.
- `agent-route.py` → version 2: `_complexity_to_profile()` now takes `(complexity, session, sonnet, weekly)` instead of a single pressure float. Block message displays all three pressure values.
- `session-start.py` → version 3: BANNER updated to reflect subscription-first strategy; adds a `usage.json` freshness check (warns if missing or >1 hour old).

### Fixed

- **Silent exception handlers in `profiles.py`** — Three `except: pass` blocks now log `WARNING` messages instead of swallowing errors silently. Pressure reordering failures are now visible.

---

## v0.9.1 — Robustness & Error Hints (2026-03-31)

### Added

- **Codex path validation** — `llm_codex` now validates `CODEX_PATH` before attempting to run. Returns a clear error with platform-specific instructions (`which codex` / `where codex`) when the binary is missing.
- **Pressure fallback validation** — `reorder_for_pressure()` now logs a warning when the fallback chain is shorter than expected, so silent routing degradation surfaces in logs.

### Changed

- Version bump to 0.9.1.

---

## v0.9.0 — Operational Reliability (2026-03-31)

### Fixed

- **Global MCP server registration** — `llm-router-install-hooks` now registers the MCP server in `~/.claude/settings.json` so `llm_*` tools are available in all Claude Code sessions, not just the llm-router project directory. Previously, hooks fired everywhere but the routing tools were unreachable in other projects.
- **Session ID collisions** — `usage-refresh.py` used `os.getppid()` for session IDs; PIDs are recycled across reboots, corrupting per-session stats. Now writes a UUID to `~/.llm-router/session_id.txt` at session start and reads that instead.
- **Stale circuit breakers** — Provider health state persisted indefinitely; a Groq failure from yesterday could block it all day. `HealthTracker.reset_stale(max_age_seconds=1800)` now clears failures older than 30 min on every MCP server startup.
- **RESEARCH silent degradation** — `llm_research` previously fell through to a non-web-grounded model when `PERPLEXITY_API_KEY` was not set, returning plausible but potentially stale answers. Now returns a clear error with setup instructions immediately.
- **Health threshold too lenient** — `health_failure_threshold` was 3 (circuit breaker only fired after 3 consecutive failures); tightened to 2 for faster provider removal from chains.

### Changed

- **Config defaults tightened**:
  - `llm_router_monthly_budget`: `0.0` (unlimited) → `20.0` ($20/month cap)
  - `daily_token_budget`: `0` (unlimited) → `500_000` (500k tokens/day)
  - `health_failure_threshold`: `3` → `2`
  - `health_cooldown_seconds`: `60` → `30`
- `install_hooks.py` gains `uninstall()` MCP server removal to match install.

### Added

- **`HealthTracker.reset_stale(max_age_seconds)`** — Resets both `consecutive_failures` and `rate_limited` for any provider whose last failure event is older than the age limit. Returns list of reset provider names for logging.
- **Session UUID** — `session-start.py` v2 writes `~/.llm-router/session_id.txt` containing a fresh UUID on every session start, plus drops a `reset_stale.flag` for the server to act on startup.
- **`get_routing_savings_vs_sonnet(days=0)`** in `cost.py` — Queries `routing_decisions` for real token counts and actual cost, computes savings as `(input_tokens × $3/M + output_tokens × $15/M) − actual_cost_usd`. Per-model breakdown included.
- **`llm_usage` lifetime savings** now uses real `routing_decisions` data (above function) instead of the legacy JSONL-estimated `savings_stats` table. Shows actual cost, Sonnet 4.6 baseline, and savings per model.

---

## v0.8.1 — Agent Routing & Real Savings Dashboard (2026-03-31)

### Added

- **PreToolUse[Agent] hook** (`agent-route.py`) — Intercepts subagent spawning before it happens. Approves pure-retrieval tasks (file reads, symbol searches, `Explore` subagent type). Blocks reasoning tasks with a redirect instruction containing the exact `llm_*` MCP tool call to use instead. Prevents the main cost leak: every subagent ran Opus for reasoning; hook routes to Haiku/Sonnet/Opus based on complexity + quota pressure.
- **Pressure-aware profile selection in agent hook**: `< 85%` quota → simple=budget (Haiku), moderate=balanced (Sonnet), complex=premium (Opus). `≥ 85%` → balanced for all. `≥ 99%` → budget/external only.
- **Session-end hook v2** — Reads `routing_decisions` SQLite table directly (replaces JSONL scanning). Shows actual model used per tool, real `cost_usd` from provider API responses, and savings vs Sonnet 4.6 baseline. Per-tool breakdown with ASCII bar charts.
- **`usage.json` export** — `llm_update_usage` in `server.py` now writes `~/.llm-router/usage.json` containing `{session_pct, weekly_pct, highest_pressure, updated_at}`. Enables hook scripts to read real Claude quota pressure without importing Python packages.

### Fixed

- **Agent hook pressure detection** — `agent-route.py` reads `highest_pressure` (0.0–1.0) from `usage.json` directly; previously tried to divide percentage fields causing wrong values.
- **Complexity classifier threshold** — "Analyze the routing logic in profiles.py..." (91 chars) was misclassified as `simple`. Fixed: `simple` now requires BOTH an explicit simple signal AND `len < 80`; otherwise defaults to `moderate`.

---

## v0.8.0 — Routing Correctness Fixes (2026-03-31)

### Fixed

- **Async feedback loop (critical)** — `get_model_failure_penalty()` and `get_model_latency_penalty()` always returned `0.0` in async contexts (every real production call), because they detected a running event loop and skipped the DB fetch to avoid deadlock. Fix: `route_and_call()` now pre-fetches `failure_rates` and `latency_stats` in parallel via `asyncio.gather()` before calling `get_model_chain()`, then passes them as optional dict parameters down through `apply_benchmark_ordering()` → penalty functions. The self-learning feedback system now works correctly in production.
- **BUDGET hard cap never fired** — `reorder_for_pressure()` had an early return for BUDGET profile (`if profile == BUDGET: return chain`), so the ≥ 99% Claude removal logic was never reached for BUDGET routing. Removed the early return — BUDGET chains now correctly strip Claude models at ≥ 99% pressure like BALANCED and PREMIUM.
- **RESEARCH chains ignored pressure entirely** — All RESEARCH tasks returned the static chain unchanged (skipping both benchmark and pressure reordering), so at 85%+ quota Claude Sonnet remained at position 2 in RESEARCH chains. Fix: RESEARCH chains now apply pressure reordering to the non-Perplexity tail only, keeping Perplexity first (web-grounded) while still demoting Claude and promoting cheap models when quota is tight.
- **RESEARCH fallback produces no web-grounded answer** — When Perplexity is unavailable, subsequent models (Gemini, Claude) produce plausible but stale answers with no source citations. Added explicit `log.warning()` and MCP notification when a RESEARCH task falls back to a non-web-grounded model.

### Changed

- `get_model_failure_penalty()` gains optional `failure_rates: dict[str, float] | None` parameter.
- `get_model_latency_penalty()` gains optional `latency_stats: dict[str, dict] | None` parameter.
- `apply_benchmark_ordering()` gains `failure_rates` and `latency_stats` optional parameters.
- `get_model_chain()` gains `failure_rates` and `latency_stats` optional parameters. Also now fetches `get_claude_pressure()` internally.
- `route_and_call()` now `await`s both `get_model_failure_rates()` and `get_model_latency_stats()` in parallel before building the model chain.

### Added

- **6 new tests** in `tests/test_profiles.py`: `TestResearchPressureTail`, `TestBudgetHardCap`, `TestPrefetchedPenalties`.

---

## v0.7.1 — Demo Reports & Docs (2026-03-31)

### Added

- **Demo report artifacts** — `demo/demo_report.{md,json}` and `demo/saas_demo_report.{md,json}` committed to repo as reference outputs from live routing sessions.

---

## v0.7.0 — Availability-Aware Routing & llm_edit Tool (2026-03-31)

### Added

- **Availability-aware routing** — Latency penalties folded into benchmark-driven quality score. P50/P95 latency tracked in `routing_decisions` over 7-day window. Thresholds: <5s=0, <15s=0.03, <60s=0.10, <180s=0.30, ≥180s=0.50 penalty.
- **Cold-start defaults** — `_COLD_START_LATENCY_MS` provides pessimistic P95 defaults for Codex models before any routing history exists.
- **`llm_edit` MCP tool** — Routes code-edit reasoning to a cheap model, returns exact `{file, old_string, new_string}` JSON for Claude to apply mechanically.
- **`src/llm_router/edit.py`** — New module with `read_file_for_edit()`, `build_edit_prompt()`, `parse_edit_response()`, `format_edit_result()`.
- **24 new tests** — `tests/test_availability_routing.py` and `tests/test_edit.py`.

---

## v0.6.0 — Subscription Mode & Quality-Cost Routing (2026-03-30)

### Added

- **Claude Code subscription mode** (`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`) — When enabled, `anthropic/*` models excluded from routing chains; no API key needed.
- **Quality-cost tier sorting** — Models grouped into 5% quality bands; within each band cheaper models sort first.
- **Cost pricing table** (`_MODEL_COST_PER_1K`) — blended per-1K-token costs for all 20+ routed models.
- **DeepSeek added to BALANCED and PREMIUM chains**.
- **Demo scripts** — `demo/app_builder_demo.py` and `demo/saas_builder_demo.py`.

### Fixed

- **Codex injection at front (subscription mode)** — When no Claude in chain, Codex is appended at the end as a free fallback rather than placed first (causing 300s timeouts).
- **Codex injected into RESEARCH chains** — Codex has no web access; now only injected for CODE, ANALYZE, GENERATE tasks.

---

## v0.5.2 — Ollama Local Models & Claude Mobile App Support (2026-03-30)

### Added

- **Ollama routing** — Local models as first-class routing targets. Set `OLLAMA_BASE_URL` + `OLLAMA_BUDGET_MODELS` to route to free local models before cloud fallback.
- **Claude mobile app access** — `llm-router-sse` CLI with SSE transport (port 17891) for remote connection via cloudflared tunnel.
- **`SessionStart` hook** — Auto-starts SSE server + cloudflared tunnel, prints mobile connection URL.

---

## v0.5.1 — Claude Haiku Budget Routing (2026-03-30)

### Added

- **Claude Haiku in BUDGET tier** — `anthropic/claude-haiku-4-5-20251001` added to all budget-tier text chains.

---

## v0.5.0 — Session Context Injection & Auto-Update Rules (2026-03-30)

### Added

- **Session context injection** — All text routing tools accept optional `context` parameter. Router prepends recent session messages and cross-session summaries so external models receive conversation history.
- **Two-layer context system** — Ephemeral ring buffer (current session) + persistent SQLite session summaries (previous sessions).
- **`llm_save_session` MCP tool** — Summarizes and persists current session to SQLite.
- **Auto-update routing rules** — `check_and_update_rules()` compares bundled vs installed rules version on startup; updates silently after `pip upgrade`.
- **Rules versioning** — `<!-- llm-router-rules-version: N -->` header in `llm-router.md`.

---

## v0.4.0 — Quality & Global Enforcement (2026-03-29)

### Added

- **Structural context compaction** — 5 strategies (collapse whitespace, strip comments, dedup sections, truncate long code, collapse stack traces). Reduces token usage 10-40%.
- **Quality logging** — `routing_decisions` SQLite table captures full routing lifecycle (21 columns).
- **`llm_quality_report` MCP tool** — ASCII analytics: classifier breakdown, task distribution, model usage, downshift rate.
- **Savings persistence** — JSONL file written by PostToolUse hook, imported into `savings_stats` SQLite table.
- **Gemini Imagen 3 + Veo 2** — Direct REST API for image and video generation.
- **Global hook installer** — `llm_setup(action='install_hooks')` + `llm-router-install-hooks` CLI.
- **Global routing rules** — `~/.claude/rules/llm-router.md` installed by hooks installer.

---

## v0.3.0 — Caching & Automation (2026-03-29)

### Added

- **Prompt classification cache** — SHA-256 exact-match + in-memory LRU (1000 entries, 1h TTL).
- **`llm_cache_stats` / `llm_cache_clear` MCP tools**.
- **Auto-route hook** — `UserPromptSubmit` hook with fast heuristic classifier (~0ms).
- **Rate limit detection** — catches 429 errors with 15s cooldown vs 60s for hard failures.
- **`llm_stream` MCP tool** — streaming LLM responses via async generator.
- **Usage-refresh hook** — `PostToolUse` hook detects stale Claude subscription data and nudges refresh.
- **Published to PyPI** as `claude-code-llm-router`.

---

## v0.2.0 — Intelligence Layer (2026-03-29)

### Added

- **Complexity-first routing** — simple → Haiku, moderate → Sonnet, complex → Opus.
- **Live Claude subscription monitoring** — fetches session/weekly usage from claude.ai.
- **Time-aware budget pressure** — reduces downshift urgency near session reset.
- **Codex desktop integration** — routes to local Codex CLI, free via OpenAI subscription.
- **`llm_usage` unified dashboard** — Claude %, Codex status, API spend, routing savings.
- **`llm_setup` tool** — discover/add API keys, view setup guides.
- **`llm_classify` tool** — classify task complexity, see routing recommendation.
- **`llm_check_usage` / `llm_update_usage`** — fetch and store Claude subscription data.

---

## v0.1.0 — Foundation (2026-03-15)

### Added

- Text LLM routing via LiteLLM (10 providers)
- Three routing profiles: budget, balanced, premium
- Cost tracking with SQLite
- Health checks with circuit breaker pattern
- Image generation (DALL-E, Flux, Stable Diffusion)
- Video generation (Runway, Kling, minimax)
- Audio/voice routing (ElevenLabs, OpenAI TTS)
- Monthly budget enforcement with hard limits
- Multi-step orchestration with pipeline templates
- Claude Code plugin with `/route` skill
- CI with GitHub Actions
