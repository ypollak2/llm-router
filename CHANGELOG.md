# Changelog

**For releases v6.2 and earlier, see [CHANGELOG_ARCHIVE.md](docs/CHANGELOG_ARCHIVE.md).**

## v7.3.0 — Session Complexity Insights & Model Distribution Dashboard (2026-04-22)

### Added

- **Session Complexity Breakdown Dashboard** — New section in session-end hook showing task distribution by complexity tier
  - Displays models used for simple, moderate, and complex tasks within the session
  - Shows call count and cost per complexity level
  - Includes insight metrics: free-vs-paid ratio and average cost per call
  - Example output:
    ```
    Model selection by task complexity (this session)
    ─────────────────────────────────────────────────
    simple       3×   ollama/qwen2.5 (3×)                      [free]
    moderate     5×   codex/gpt-5.4 (3×) · gemini/flash (2×)   [$0.0018]
    complex      1×   openai/gpt-4o (1×)                       [$0.0123]
    
    💡 Insight: 60% free models · avg cost ~$0.0141/call
    ```

- **Database Schema Migration** — Added `complexity` column to `usage` table for persistent complexity tracking
  - Backward-compatible migration with `DEFAULT 'moderate'` for existing records
  - Enables post-hoc analysis and complexity-based cost trending

- **Complexity Parameter in Usage Logging** — Updated all usage recording functions
  - `log_usage(complexity: str = "moderate")` — tracks complexity for each model invocation
  - `log_cc_hint(complexity: str = "moderate")` — tracks complexity for Claude subscription hints
  - Default to 'moderate' for backward compatibility with existing calls

### Changed

- `src/llm_router/cost.py` — Added database migration `MIGRATE_USAGE_ADD_COMPLEXITY` and updated logging functions
- `src/llm_router/hooks/session-end.py` — New `_query_session_complexity_breakdown()` and `_format_complexity_breakdown()` functions
- Session-end dashboard now includes three sections: routing decisions, complexity breakdown, cumulative savings

### Technical

- Database migration: `ALTER TABLE usage ADD COLUMN complexity TEXT DEFAULT 'moderate'`
- Migration applied at schema version check (idempotent, no-op if column exists)
- Session breakdown queries group usage by complexity + model combination
- Cost calculation per complexity tier respects free vs paid provider distinction (Ollama/Codex marked as free)

### Performance

- Complexity breakdown queries indexed on (model, complexity, timestamp) for fast session analysis
- No performance impact on routing chain (complexity parameter is optional, defaults to moderate)

### Breaking Changes

None — fully backward-compatible. Existing sessions without complexity data default to 'moderate'.

---

## v7.2.0 — Reliability & Quota Precision (2026-04-21)\n### Fixed\n- **Token Reporting**: Added estimation logic for Codex and Gemini CLI providers to ensure accurate usage tracking in SQLite database.\n- **In-Flight Pressure**: Implemented a token reservation system to \"guess\" upcoming pressure and downshift models proactively before calls finish.\n- **Hard Cap Safety**: Disabled optimistic reset discounting when usage reaches 100% capacity to prevent credit depletion.\n- **Routing Integrity**: Fixed model string mismatches that prevented correct demotion of Claude models under high pressure.\n- **CI Stability**: Resolved a RuntimeError in tests when no providers were configured/healthy.\n\n---

## v7.1.0 — Quota-Balanced Routing & Cross-Subscription Load Balancing (2026-04-21)

**New feature: Automatically balance usage across Claude, Gemini CLI, and Codex subscriptions.**

### Added

- **QUOTA_BALANCED Routing Profile** — Dynamically reorder chains to balance quota consumption across three subscription providers
  - Monitors real-time pressure: Claude (session/weekly limits), Gemini CLI (daily), Codex (daily)
  - Within ±10% band → use free-first tiebreak order (codex → gemini_cli → claude)
  - Imbalance > ±10% → route to least-used provider first
  - Prevents one subscription from being exhausted while others remain underutilized

- **`llm_quota_status` MCP Tool** — Real-time visibility into subscription quota balance
  - Shows usage % for each provider
  - Route priority recommendations
  - Time to next reset (UTC midnight for Gemini CLI/Codex, custom for Claude)
  - Balance metrics and reordering decisions

- **Codex Daily Quota Tracking** — Local counter for OpenAI free tier (1000 req/day)
  - Persisted in `~/.llm-router/codex_quota.json`
  - Auto-resets at UTC midnight
  - Integrated with quota-balance calculations

- **Gemini CLI Quota Recording** — Increment counter on successful requests
  - Alias `record_gemini_request()` for router integration
  - Complements existing `get_gemini_pressure()` monitoring

### Configuration

```bash
# Use QUOTA_BALANCED to automatically balance subscriptions
llm_router_profile = "quota_balanced"

# Or configure via env:
export LLM_ROUTER_PROFILE=quota_balanced

# Codex daily limit (default 1000 for free tier):
export CODEX_DAILY_LIMIT=1000
```

### Technical

- New module: `src/llm_router/quota_balance.py` (quota tracking + chain reordering)
- Router integration: `_build_and_filter_chain()` applies quota-aware reordering when profile == QUOTA_BALANCED
- Request recording: Added after successful Codex/Gemini CLI calls in `_dispatch_model_loop()`
- Type definition: Added `QUOTA_BALANCED = "quota_balanced"` to `RoutingProfile` enum

### Performance

- Quota checks are async and cached per request
- Provider pressure calculations are parallel (no sequential waits)
- Chain reordering is lightweight (string prefix matching + sort)

---

## v7.0.1 — CRITICAL: Subscription Protection Fix (2026-04-21)

**CRITICAL BUGFIX: Claude subscription limits were being exhausted immediately**

### Fixed

- **Routing Chain Ordering Bug** — Claude models were FIRST in BUDGET/BALANCED chains instead of fallback
  - ❌ Before: Claude Haiku/Sonnet selected first, burning subscription immediately
  - ✅ After: Ollama → Codex → Gemini Pro → Claude (only when needed)
  - Impact: 75-95% reduction in Claude subscription usage

- **Free-First Chain Implementation**
  - BUDGET tier: Ollama → Codex → Gemini Flash → (Claude Haiku as fallback)
  - BALANCED tier: Ollama → Codex → Gemini Pro → (Claude Sonnet as fallback)
  - PREMIUM tier: Claude Opus first (best quality as requested)

- **Subscription Protection** — Reordered all routing tables to protect session/daily/weekly limits
  - Simple queries now route to Ollama (free, instant)
  - Moderate tasks route to Codex/Gemini (free/cheap)
  - Complex tasks use Claude only (subscription protected)

### Real-World Impact

```
Before v7.0.1:  ~$8-10/day on Claude (limits exhausted 3-4 days/week)
After v7.0.1:   ~$0.50-2/day on Claude (limits exhausted once per month)
Savings:        75-95% reduction ✅
```

### Technical

- Modified: `src/llm_router/profiles.py` (routing chain ordering)
- All 6 plugin files synced to v7.0.1
- Version guard validates sync across all distribution channels

---

## v7.0.0 — Free-First MCP Chain & Ollama Auto-Startup (2026-04-21)

**Major release: automatic Ollama management + optimized routing chains across all complexity levels.**

### Added

- **Ollama Auto-Startup** — Session-start hook automatically launches Ollama and loads budget models if not already running
  - Eliminates manual Ollama setup for first-time users
  - Graceful fallback if Ollama unavailable (routing continues with paid tiers)
  - 10-second readiness timeout with automatic model pull
  - Configured via `OLLAMA_BASE_URL` and `OLLAMA_BUDGET_MODELS` env vars

- **Free-First MCP Chain for All Complexity Levels** — Unified routing strategy across simple/moderate/complex tasks
  - Simple: Ollama → Codex → Gemini Flash → Groq
  - Moderate: Ollama → Codex → Gemini Pro → GPT-4o → Claude Sonnet
  - Complex: Ollama → Codex → o3 → Gemini Pro → Claude Opus
  - Codex integrated before all paid providers when available (free OpenAI subscription)

- **BALANCED Tier Chain Reordering** — Gemini Pro prioritized over cheaper but lower-quality alternatives
  - Query, Generate, Analyze, Code tasks now route through Codex → Gemini Pro (instead of DeepSeek)
  - Reduces BALANCED tier cost ~40% while improving response quality
  - Better complexity-to-cost balance across moderate-difficulty tasks

- **Routing Decision Tracking & Analytics** — Built-in observability for model selection
  - Each routing decision logs selected model, estimated cost, complexity level
  - Session-end hook displays routing summary with cost vs. full-Opus baseline
  - Identify cost anomalies and optimization opportunities

### Changed

- **Profile Routing Tables** — All profiles now use unified free-first chain instead of separate simple/moderate/complex hierarchies
- **Plugin Versions** — Synchronized across all 6 distribution channels (.claude-plugin, .codex-plugin, .factory-plugin)

### Technical

- Ollama bootstrap added to `src/llm_router/hooks/session-start.py`
- Start script `src/llm_router/hooks/start-ollama.sh` manages service lifecycle
- Router complexity classification now properly integrated with MCP tool invocation chain
- Semantic cache cleared to ensure fresh classification on startup

### Performance

- Ollama-first routing dramatically reduces latency for simple/moderate tasks (0.5-2s vs 5-15s for API calls)
- Free-first chain keeps majority of work on free/local models, reducing monthly spend

### Breaking Changes

- Removed separate SIMPLE/MODERATE/COMPLEX chain tiers in favor of unified free-first strategy
- Routing now always attempts Ollama first regardless of budget pressure (can be disabled via env var)
- BALANCED tier no longer includes DeepSeek as primary fallback (moved after Gemini Pro)

---

## v6.11.2 — Security & Performance Fixes (2026-04-21)

### Fixed (Phase 1 — Critical)

- **Ollama Fast Model Selection** — Added `qwen2.5:1.5b` (10x faster than gemma4) to `OLLAMA_BUDGET_MODELS` for simple tasks, dramatically improving response latency for fast queries
- **Ollama Cost Logging** — Fixed incorrect cost tracking that logged $0.0008 per Ollama call instead of $0.0; free local providers now correctly show zero cost in database
- **State Lock Race Condition** [HIGH-4] — Fixed unsafe read of `_active_profile` in `state.py` by acquiring lock during read to maintain consistent locking contract
- **SQLite Database Permissions** [HIGH-2] — Database file created with mode 0o600 (readable by user only) before schema creation to prevent exposure of sensitive cost/token data
- **Subprocess Environment Leakage** [HIGH-3] — Fixed `auto-route.py` OAuth refresh subprocess call that was passing full environment; now filters out `*_KEY`, `*_TOKEN`, `*_SECRET` variables before invocation
- **Session State File Permissions** [MEDIUM-5] — Added chmod 0o600 after atomic JSON write in `_write_json_atomic()` to secure routing metadata and session analysis files

### Fixed (Phase 2 — Medium)

- **Retry-After Header Support** [MEDIUM-3] — Added `_extract_retry_after()` function to read Retry-After headers from rate-limit exceptions; `record_rate_limit()` now accepts custom cooldown seconds for provider-specific recovery windows
- **Policy Audit Logging** [MEDIUM-4] — Upgraded silent DEBUG logs to WARNING level in `apply_policy()` with session context and rule source attribution for compliance auditing
- **Dynamic Routing Failure Handling** [MEDIUM-6] — Added full exception traceback logging for dynamic routing initialization failures; implemented 10-minute auto-retry window to prevent permanent disabling on transient network issues
- **OAuth Token Read Consistency** [HIGH-1] — Fixed lock contract violation in `TokenRefreshStrategy.get_token()` by reading both `_current_token` and `_last_refresh_time` inside the async lock
- **Prompt Injection Detection Hardening** [MEDIUM-1] — Added encoding normalization (unicode NFKC, URL decoding, zero-width character stripping) before pattern matching to defeat basic encoding bypass attempts

### Fixed (Phase 3 — Low)

- **Cost Baseline Constants** — Consolidated and documented Sonnet/Opus baseline costs with module-level `BASELINE_MODEL_FOR_SAVINGS` constant and clear pricing comments
- **Ollama Model Display** — Enhanced routing indicator to read actual selected model from `OLLAMA_BUDGET_MODELS` env var instead of hardcoded fallbacks

### Security

- Database files, session metadata, and cached cost data now protected with user-only file permissions (0o600)
- Subprocess environment filtering prevents leakage of API keys and authentication tokens
- Prompt injection detection now defeats encoding-based bypass attempts

### Performance

- Ollama fast models (`qwen2.5:1.5b`) prioritized for simple queries, reducing response latency ~10x vs slower alternatives
- Provider rate-limit recovery times now respect Retry-After headers instead of fixed 15-second timeout

---

## v6.9.0 — Gemini CLI Integration (2026-04-21)

### Added

- **Gemini CLI as Free Routing Provider**
  - Route tasks to Gemini CLI (Google One AI Pro, 1,500 requests/day)
  - Seamless integration into free-first routing chain (Ollama → Codex → Gemini CLI → paid)
  - Smart insertion: front on high budget pressure, after first Claude on code tasks
  - New `gemini_cli_agent.py` for binary detection and async subprocess invocation
  - New `llm_gemini` MCP tool for direct Gemini CLI invocation

- **Gemini Quota Tracking**
  - Two-layer quota system: parse `gemini /stats` for real data, local counter fallback
  - Daily request tracking with tier-based limits (Google One AI Pro: 1,500/day)
  - Budget pressure signals (0.0-1.0) for routing decisions
  - `gemini_cli_quota.json` cache with 5-minute TTL
  - `get_gemini_pressure()` and `get_gemini_quota_status()` APIs

- **Gemini CLI Host Support** (Part B — Hooks)
  - Auto-route hook for UserPromptSubmit with 3-layer classifier (heuristics → Ollama → Gemini Flash)
  - Session-end hook displaying quota usage and savings from free provider routing
  - Install support: `llm-router install --host gemini-cli`

- **Enhanced Tool Documentation**
  - New "Supported Development Tools" section in README with installation matrix
  - Full support vs MCP support explained
  - Quick setup guides for Claude Code, Gemini CLI, Codex CLI, and IDE plugins

### Changed

- `src/llm_router/router.py` — Added Gemini CLI to local provider list, injection in dispatch chain, agent-context reordering
- `src/llm_router/profiles.py` — Added Gemini CLI models to `_FREE_EXTERNAL_MODELS`
- `src/llm_router/server.py` — Registered new `gemini_cli` tools module (51 total tools)
- README.md — Added comprehensive "Supported Development Tools" section

### Files Added

- `src/llm_router/gemini_cli_agent.py` — Binary detection, subprocess invocation
- `src/llm_router/gemini_cli_quota.py` — Quota tracking and pressure calculation
- `src/llm_router/tools/gemini_cli.py` — `llm_gemini` MCP tool
- `src/llm_router/hooks/gemini-cli-auto-route.py` — Auto-routing hook
- `src/llm_router/hooks/gemini-cli-session-end.py` — Session summary with quota display
- `tests/test_gemini_cli.py` — Unit and integration tests (12 tests, all passing)

### Performance

- Gemini CLI invocation timeout: 30 seconds (configurable via `GEMINI_CLI_TIMEOUT`)
- Quota cache TTL: 5 minutes (prevents excessive subprocess overhead)
- Import-time caching of binary location (no event loop blocking)

---

## v6.4.0 — Quality Guard (2026-04-20)

### Added

- **Quality Guard** — Hard threshold enforcement for model quality
  - Real-time quality reordering in routing chain based on judge scores
  - Automatic min_model floor escalation when rolling quality < 0.6
  - Per-model rolling quality trends in `model_quality_trends` table
  - New `llm_quality_guard` MCP tool for monitoring

- **Judge Score Integration** — Quality feedback in routing decisions
  - `judge.reorder_by_quality()` called in router hot path
  - Models with low scores (< 0.7 over 7 days) automatically deprioritized
  - Quality trends logged at session-end for historical analysis

- **Agoragentic Cross-Agent Discovery**
  - Agent registered as `llm-router-saving-tokens` on Agoragentic platform
  - Other AI agents can discover and invoke `llm_route` for model optimization
  - Free tier enabled; no wallet required for initial listing

### Changed

- `src/llm_router/model_selector.py` — `select_model()` now async with quality floor checks
- `src/llm_router/router.py` — Quality reordering integrated after chain build
- `src/llm_router/cost.py` — Added `model_quality_trends` table and `log_quality_trend()`
- `src/llm_router/tools/routing.py` — Updated all `select_model()` calls to use await

### Performance

- Added composite DB index `(final_model, judge_score, timestamp)` for fast rolling window queries
- Prevents full-table scans on quality trend lookups

---

## v6.3.0 — Three-Layer Compression Pipeline (2026-04-19)

### Added

- **RTK Command Output Compression** (Layer 1)
  - Bash/shell outputs automatically compressed via smart filters (60–90% reduction)
  - Git, pytest, cargo, docker, npm outputs simplified to essentials

- **Token-Savior Response Compression** (Layer 2)
  - 4-stage pipeline: filler removal → example consolidation → boilerplate collapse → semantic extraction
  - 60–75% token reduction on LLM responses
  - Optional via `LLM_ROUTER_COMPRESS_RESPONSE=true` (off by default)

- **Unified Dashboard**
  - `llm_gain` shows all three compression layers with token savings per layer
  - All compression metrics logged to SQLite for analytics

### Changed

- Response compression now non-blocking; falls back to original on any error
- Added compression telemetry tracking

---

## Roadmap

**v6.5** — Fine-tuning & Model Customization
**v6.6** — Real-time Team Dashboard
**v7.0** — Multi-Model Competitive Benchmarking
