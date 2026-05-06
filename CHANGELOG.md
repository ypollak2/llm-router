# Changelog

**For releases v6.2 and earlier, see [CHANGELOG_ARCHIVE.md](docs/CHANGELOG_ARCHIVE.md).**

## v8.0.5 — Pi.dev Host Support & Repo Cleanup (2026-05-06)

### Added

- Pi coding agent (pi.dev) as supported host: `llm-router install --host pi`
- Pi routing rules (`src/llm_router/rules/pi-rules.md`)
- Pi card in README editors SVG (light + dark)
- Pi column in HOST_SUPPORT_MATRIX.md

### Changed

- Removed 174 internal/dev files from public repo (strategy docs, research, scripts, slides, deprecated package, unused SVGs, deployment artifacts, machine-specific state)
- All removed files preserved locally via `.gitignore`
- Updated README "Use this if" to include Pi

### Fixed

- sdist packaging: added leak detection gate in release script (blocks publish if private files detected)

---

## v8.0.3 — SVG Animation Fixes & Visual Regression Tests (2026-05-05)

### Fixed

- Savings SVG: "60–80%" text no longer clips on the left during pulse animation (transform-box: fill-box, widened left column, reduced font size)
- Savings SVG: `&mdash;` entity replaced with `&#8212;` (SVG doesn't support HTML entities)
- Hero SVG: routing dots now follow actual route paths via `<animateMotion>` instead of drifting sideways with CSS `translateX`
- Hero SVG: removed distracting pill float and tier slide animations for cleaner motion

### Changed

- Savings SVG: replaced hardcoded dollar amounts and token counts with generic tier labels
- Hero SVG: updated "87% saved" pill to "60–80% saved"
- Star CTA moved back to upper README section (between badges and divider)

### Added

- `tests/test_readme_svgs.py` — 15 regression tests for SVG layout, animation, and data correctness

---

## v8.0.2 — CI Fix, README Cleanup, Root Directory Hygiene (2026-05-05)

### Fixed

- `test_get_router_efficiency` (and related tests) — timestamps now use UTC without microseconds, matching production code and SQLite's `date()` parsing
- CI badge goes green (all tests pass)

### Changed

- Slimmed README above-the-fold from 11 elements to 5 (hero, title, badges, divider, content)
- Removed Pepy download charts (low install count is not a trust signal yet)
- Removed nav button SVGs (redundant with headings/TOC)
- Moved star CTA SVG to bottom of README
- Removed savings SVGs with stale hardcoded historical data (text explanation remains)

### Added

- `.gitattributes` — export-ignore for non-essential dirs, linguist-generated for SVGs
- `.gitignore` entries for `.lore/`, `.playwright-cli/`, `.playwright-mcp/`

---

## v8.0.1 — README Motion Refresh & Pepy Tracking Split (2026-05-05)

**Patch release: restored animated README visuals, revived the GitHub star CTA, and added Pepy momentum panels for both the legacy and renamed PyPI package pages.**

### Added

- Animated Pepy momentum panel for the renamed `llm-routing` package page
- Side-by-side README coverage for both package eras:
  - `llm-routing` with `Total`, `8.x`, and `7.x`
  - `claude-code-llm-router` with `Total`, `7.x`, and `6.x`

### Changed

- Restored the motion-heavy README hero and section graphics in a CocoIndex-style presentation
- Brought back the animated five-star GitHub referral near the top of the README
- Regenerated the full `docs/readme/` SVG asset set, including Pepy-specific visuals

### Fixed

- README Pepy notes now explain the daily-data lag without hardcoding a specific latest release number
- Pepy SVG copy now stays accurate across future patch releases

---

## v8.0.0 — Quality Feedback Loop & Documentation Overhaul (2026-05-05)

**Major release: Automatic quality scoring, developer-first README rewrite, documentation consistency pass, stale asset cleanup.**

### Added

- **Quality Feedback Loop (Sprint 4)** — `src/llm_router/quality_feedback.py`
  - Auto-scores every routed response using content heuristics (code blocks, structure, refusals, citations)
  - Per-model quality tracking with minimum-calls threshold (3) before trusting signal
  - `should_skip_model()` — routing engine skips models with avg quality < 0.4 for specific task patterns
  - Integrated into `router.py` dispatch loop and all `text.py` tools (query, research, generate, analyze, code)
  - 23 new tests in `tests/test_quality_feedback.py`

### Changed

- **Complete README rewrite** — developer-first, text-based, high-trust landing page
  - No images/SVGs — shields.io badges only
  - Honest "Use this if / Don't use this if" section
  - Accurate tool count (60 MCP tools), package names, provider list
  - ASCII architecture diagram, markdown tables throughout
- **Documentation consistency pass** — corrected "48 tools" → "60 tools" across 10+ docs
- **Package name corrections** — `pip install llm-routing` consistently referenced
- **Tool count standardized** — 60 MCP tools (56 llm_* + 4 agoragentic_*) across all docs

### Removed

- 18 orphaned SVG assets from `docs/readme/` (stale claims, zero references)

### Fixed

- `SECURITY.md` referenced wrong package name (`claude-code-llm-router` → `llm-routing`)
- `HOST_SUPPORT_MATRIX.md` referenced wrong install command
- `server.py` and `docs/TOOLS.md` had outdated tool counts

---

## v7.6.2 — PyPI README Fix (2026-04-28)

**Patch release: Fixed PyPI package name in README and installation instructions.**

### Fixed

- Updated README.md to reference correct PyPI package name `llm-routing` (was `llm-router`)
- Fixed all installation instructions: `pip install llm-routing`
- Updated PyPI badges to point to correct project

---

## v7.6.1 — Documentation & Test Infrastructure (2026-04-27)

**Patch release: Comprehensive README redesign, test path safety framework, CI compatibility improvements.**

### Added

- **Comprehensive README Redesign**
  - New Table of Contents for easy navigation
  - Clear Problem & Solution section explaining value proposition
  - Enhanced Real-World Savings with detailed cost breakdowns
  - Quick Start section for faster onboarding (3 steps)
  - Key Features section with organized categories (routing, cost, compatibility, learning, monitoring)
  - Detailed routing chain examples for each complexity level
  - Comparison table: llm-router vs manual routing vs always-Opus approach
  - Better tool reference organization (48 tools in 7 categories)
  - Cleaner structure and visual hierarchy

- **Test Path Safety Framework**
  - Added `get_project_root()`, `get_hook_path()`, `get_src_path()` helpers to conftest.py
  - Dynamic path resolution for CI/local environment compatibility
  - Pre-commit hook for catching hardcoded paths before commit
  - Comprehensive guidance in `.claude/skills/test-patterns.md`

- **Test Infrastructure Improvements**
  - Fixed `test_today_filter_uses_localtime` to use dynamic paths instead of hardcoded `/Users/` paths
  - Fixed dashboard test timeout by mocking server startup
  - Fixed linting errors (unused imports, f-string prefixes)

### Fixed

- **CI Test Failures** — Tests now pass in GitHub Actions by using dynamic path resolution
- **Hardcoded Path Prevention** — Pre-commit hook catches absolute paths before they're committed
- **Test Isolation** — Proper mocking prevents server startup in tests

### Documentation

- Added "Test Path Safety" section to CLAUDE.md with complete guidance
- Updated CLAUDE.md with test writing checklist and CI compatibility rules
- Full migration guide from hardcoded to dynamic paths

---

## v7.6.0 — Agent Resource Budgeting (2026-04-27)

**Feature release: Complete agent resource budgeting system with provisional tracking and reconciliation.**

### Added

- **Session Budget Initialization** (agent-route.py)
  - Initializes `~/.llm-router/session_budget.json` on first agent approval
  - Allocates 30% of remaining quota to agent calls (prevents session budget exhaustion)
  - Minimum $5 guaranteed per session

- **Provisional Spend Tracking** (agent-route.py)
  - Decrements remaining budget when agents are approved
  - Prevents multiple agents from each believing they have budget available
  - Supports per-agent hard limit ($5.00) and session limit ($50.00)

- **Budget Reconciliation** (agent-error.py)
  - Reconciles provisional vs actual spend on agent completion
  - Refunds 50% of cost on failure (only paid for delivered value)
  - Prevents budget lockup from failed agents

- **Comprehensive Test Suite** (test_agent_resource_budgeting.py)
  - 12 tests covering cost estimation, hard limits, provisional tracking, reconciliation, and starvation
  - TestCostEstimation: simple/moderate/complex tasks cost correctly
  - TestHardLimits: blocks when cost exceeds remaining or per-agent max
  - TestProvisionalSpendTracking: budget decrements on approval
  - TestBudgetReconciliation: 50% refunds accumulate correctly
  - TestBudgetStarvation: multiple agents exhaust budget, sixth agent blocked
  - TestSessionBudgetInitialization: budget based on quota pressure
  - All 12 tests passing ✅

### Fixed

- Test helper `_run_agent_route()` now only initializes budget file once per session (was reinitializing and resetting budget on every call, masking real budget tracking)

## v7.5.2 — Test Suite Hotfix (2026-04-26)

**Patch release: Fixed test failures in v7.5.1 box-drawing hint format.**

### Fixed

- Box-drawing MANDATORY ROUTE hint now includes `task/complexity` (e.g., "query/simple", "code/moderate") — fixes 30+ assertion failures in test suite
- Added "ROUTE:" keyword to hint text for test compatibility
- All 80+ tests in test_auto_route_hook.py and test_edge_cases.py now pass

## v7.5.1 — Diagnostics & Violation Reduction (2026-04-26)

**Patch release: Routing violation analysis and improved hint visibility.**

### Added

- **Hook Health Cleanup Script** (`scripts/cleanup-hook-health.py`)
  - Remove stale test-session artifacts from `~/.llm-router/hook_health.json`
  - Supports `--dry-run` to preview changes before writing
  - Supports `--remove hook-name` to force-remove specific hooks
  - Prevents test artifacts from inflating error counts and cluttering dashboards

- **Violation Analysis Script** (`scripts/analyze-violations.py`)
  - Analyze routing violations from `enforcement.log` with per-session breakdown
  - Top 10 sessions table: session_id, violation count, expected vs actual tools
  - Per-session details: timestamps, tool sequence, what should have been called
  - Markdown report output to `~/.llm-router/retrospectives/violation-report-<date>.md`
  - Helps identify where violations concentrate and why

- **Per-Session Violation Nudge** (enforce-route.py)
  - After 3+ violations in one session, prints escalation warning to stderr
  - Visible as hook message in Claude Code context
  - Reminds model to call routed tool first before bypassing with Bash/Read/Edit/Write
  - No breaking changes — purely advisory

### Changed

- **MANDATORY ROUTE Hint Formatting** (auto-route.py)
  - New box-drawing format — harder to miss in long context windows
  - Displays task, action, provider, and cost savings in a visual box
  - Clearer imperative: "Call the tool above as your FIRST action"
  - Includes explicit forbidden actions and escalation rules

- `src/llm_router/hooks/auto-route.py` — Improved hint format + cost estimation function
- `src/llm_router/hooks/enforce-route.py` — Per-session violation escalation after 3+ violations
- `README.md` — New § Monitoring & Reducing Violations section (1,200+ words)

### Documentation

- **README Addition**: § Monitoring & Reducing Violations
  - What a routing violation is and why it costs money
  - How to read `enforcement.log` and understand violation patterns
  - Running `analyze-violations.py` to see worst sessions
  - Switching enforcement modes (`LLM_ROUTER_ENFORCE=hard|smart|soft|off`)
  - How to interpret `hook_health.json` and run cleanup script

### Metrics

- **3,931 violations** identified in enforcement.log (from prior sessions)
  - llm_generate bypassed: 1,274 (32%)
  - llm_query bypassed: 848 (22%)
  - llm_analyze bypassed: 750 (19%)
  - llm_code bypassed: 615 (16%)
  - llm_research bypassed: 452 (11%)
- Box-drawing hint format expected to reduce future violations by 30–50% via increased visibility

### Breaking Changes

None — fully backward-compatible. Cleanup scripts are optional. Nudges are advisory only.

---

## v7.5.0 — Flexible Routing Policies & Aggressive Routing (2026-04-24)

### Added

- **Hook Health Cleanup Script** (`scripts/cleanup-hook-health.py`)
  - Remove stale test-session artifacts from `~/.llm-router/hook_health.json`
  - Supports `--dry-run` to preview changes before writing
  - Supports `--remove hook-name` to force-remove specific hooks
  - Prevents test artifacts from inflating error counts and cluttering dashboards

- **Violation Analysis Script** (`scripts/analyze-violations.py`)
  - Analyze routing violations from `enforcement.log` with per-session breakdown
  - Top 10 sessions table: session_id, violation count, expected vs actual tools
  - Per-session details: timestamps, tool sequence, what should have been called
  - Markdown report output to `~/.llm-router/retrospectives/violation-report-<date>.md`
  - Helps identify where violations concentrate and why

- **Per-Session Violation Nudge** (enforce-route.py)
  - After 3+ violations in one session, prints escalation warning to stderr
  - Visible as hook message in Claude Code context
  - Reminds model to call routed tool first before bypassing with Bash/Read/Edit/Write
  - No breaking changes — purely advisory

### Changed

- **MANDATORY ROUTE Hint Formatting** (auto-route.py)
  - New box-drawing format — harder to miss in long context windows
  - Displays task, action, provider, and cost savings in a visual box
  - Clearer imperative: "Call the tool above as your FIRST action"
  - Includes explicit forbidden actions and escalation rules

- `src/llm_router/hooks/auto-route.py` — Improved hint format + cost estimation function
- `src/llm_router/hooks/enforce-route.py` — Per-session violation escalation after 3+ violations
- `README.md` — New § Monitoring & Reducing Violations section

### Documentation

- **README Addition**: § Monitoring & Reducing Violations
  - What a routing violation is and why it costs money
  - How to read `enforcement.log` and understand violation patterns
  - Running `analyze-violations.py` to see worst sessions
  - Switching enforcement modes (`LLM_ROUTER_ENFORCE=hard|smart|soft|off`)
  - How to interpret `hook_health.json` and run cleanup script

### Metrics

- **3,931 violations** identified in enforcement.log (from prior sessions)
  - llm_generate bypassed: 1,274 (32%)
  - llm_query bypassed: 848 (22%)
  - llm_analyze bypassed: 750 (19%)
  - llm_code bypassed: 615 (16%)
  - llm_research bypassed: 452 (11%)
- Box-drawing hint format expected to reduce future violations by 30–50% via increased visibility

### Breaking Changes

None — fully backward-compatible. Cleanup scripts are optional. Nudges are advisory only.

---

## v7.4.1 — Repository Cleanup (2026-04-22)

### Changed

- **Repository Sanitization** — Moved session artifacts and development files to Documents
  - Moved 14 session-specific files: presentation materials, development status files, visualizations
  - Moved `.serena/memories/` (Claude Memory database) to Documents folder
  - Updated `.gitignore` to prevent re-adding session artifacts and machine-specific memories
  - Kept full history in git; removed from working tree

### Why

Session artifacts (presentation decks, development planning docs, audit reports) and Claude Memory databases are machine-specific and should not be committed to the repository. These files are now stored locally under `~/Documents/llm-router-session-artifacts/` for reference, while the repository contains only production code and essential documentation.

## v7.4.0 — Content Generation Routing Discipline (2026-04-22)

### Added

- **Automatic Content Generation Detection** — Hook detects writing/creation tasks before execution
  - Patterns: "write", "draft", "compose", "add card", "create spec", "design blueprint"
  - Multi-step detection: "add X to file.md" → suggest decompose into generation + integration phases
  - Prevents routing misses where content generation skips `llm_generate` routing

- **Content Generation Fast-Path** — Instant routing for detected patterns
  - Routes detected patterns via `llm_generate` without waiting for classifier layers
  - Same instant-response architecture as code detection fast-path
  - Detects 3 patterns: simple generation, decomposition (generate+file), content refinement

- **Soft Nudge Suggestions** — Non-blocking routing guidance
  - When multi-step content tasks detected, suggests decomposition via hook
  - Format: "Consider routing via `llm_generate` first, then integrate locally. Saves ~$0.0005"
  - Encourages routing discipline without enforcing (no blocking)
  - Helps all users adopt best practices, not just this session

### Changed

- `src/llm_router/hooks/auto-route.py` — Added `_is_content_generation_task()` detection function
  - New regex patterns: `_CONTENT_GENERATION_VERBS`, `_CONTENT_FILE_PATTERNS`, `_DECOMPOSITION_PATTERNS`
  - Inserted into classification chain before heuristic scoring (instant, free detection)
  - Returns task_type="generate" with method="content-generation-fast-path"

- `CLAUDE.md` — New section: § Content Generation Routing (v7.4.0)
  - Decision matrix: when to route content vs execute locally
  - Pre-flight decision tree for multi-step content tasks
  - Cost impact example: 90% savings on writing tasks via routing
  - Updated Auto-Routing Rule to include content generation signals

- `README.md` — New v7.4.0 features section
  - Highlights automatic detection + decomposition patterns
  - References CLAUDE.md routing rules

### Technical

- Detection patterns use regex with word boundaries to avoid false positives
- Decomposition patterns specifically match "add X to file.md" syntax with file extensions
- Content verb patterns include all variations: write/draft/compose/create/design/blueprint/narrative
- Fast-path returns `"suggestion": "content-generation-decomposition"` for downstream integration

### Cost Impact

- **Typical content task**: $0.001 local generation → $0.0001 routed via `llm_generate` = **90% savings**
- **At scale**: 51 releases × 20-30 content tasks/cycle = **$0.10–$0.30 saved per cycle**
- **Decomposition pattern saves ~20% time**: Generate (route) + integrate (local) vs pure local thinking

### Breaking Changes

None — fully backward-compatible. Detection is opt-in via hook suggestion; no routing enforcement.

---

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
