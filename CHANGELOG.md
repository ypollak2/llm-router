# Changelog

**For releases v6.2 and earlier, see [CHANGELOG_ARCHIVE.md](docs/CHANGELOG_ARCHIVE.md).**

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
