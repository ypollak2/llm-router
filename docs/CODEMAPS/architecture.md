<!-- Generated: 2026-04-26 | Version: 7.5.0 | Files scanned: 72 modules + 84 tests | Lines: 26K+ | Token estimate: ~1200 -->

# LLM Router Architecture (v7.5.0)

## Project Type
**Python library + MCP plugin** for intelligent model routing and cost optimization
**Status**: Production/Stable (100% test passing)

## High-Level System Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     Claude Code / User                       │
└──────────────────────┬──────────────────────────────────────┘
                       │
         ┌─────────────▼──────────────┐
         │   MCP Server (server.py)   │  ← Exposes tools to Claude
         └──┬──────────────────────┬──┘
            │                      │
       ┌────▼────┐         ┌──────▼──────┐
       │ Tools    │         │ Orchestrator│
       │ Modules  │         │ (router.py) │
       └────┬────┘         └──────┬──────┘
            │                     │
    ┌───────┴──────────┬──────────┴───────────┐
    │                  │                      │
┌───▼────┐      ┌──────▼────┐         ┌──────▼────┐
│ Routing │      │Classifier │         │  Budget   │
│ Chains  │      │  (LLM)    │         │ (Pressure)│
└────┬────┘      └──────┬────┘         └──────┬────┘
     │                  │                     │
     └──────────────────┼─────────────────────┘
                        │
          ┌─────────────▼─────────────┐
          │   Provider Chain          │
          │ (Ollama→Codex→GPT→Gemini) │
          └─────────────┬─────────────┘
                        │
          ┌─────────────┼─────────────┐
          │             │             │
    ┌─────▼─┐    ┌─────▼─┐    ┌─────▼──┐
    │ Local │    │ Cloud │    │ Web    │
    │Ollama │    │ APIs  │    │ Search │
    └───────┘    └───────┘    └────────┘
```

## Core Services

### Routing Layer (router.py, 400 lines)
- **Main function**: `route_and_call(task_type, prompt, profile)`
- **Logic**: Classify complexity → Select model chain → Try models in order → Log results
- **Return**: `LLMResponse` with model, cost, latency, tokens
- **New in v7.5**: Flexible routing policies (aggressive/balanced/conservative)

### Classifier (classifier.py, 300 lines)
- **Purpose**: Determine task complexity (simple/moderate/complex)
- **Method**: 3-layer fallback (heuristics → Ollama → API)
- **Cache**: SHA-256(prompt) + LRU for 80%+ cache hits
- **New in v7.5**: Confidence scoring per classification layer

### Model Selector (model_selector.py, 250 lines)
- **Inputs**: Task type, complexity, budget pressure, routing policy
- **Outputs**: Ordered model chain (free-first: Ollama → Codex → Paid)
- **Rules**: Policy-aware ordering; pressure ≥95% → downgrade complexity
- **New in v7.5**: Policy-driven chain construction (aggressive/balanced/conservative)

### Budget & Pressure (claude_usage.py, 250 lines | budget.py, 150 lines)
- **Claude subscription tracking**: Session%, weekly%, Sonnet%, Opus%
- **Cost tracking**: Real-time monthly spend per provider
- **Pressure logic**: Downshift models when budget tight
- **New in v7.5**: Per-provider budget caps + team quota tracking

### Hook System (hooks/, 8 bundled scripts)
- **auto-route.py**: UserPromptSubmit → Emit routing hints
- **enforce-route.py**: PreToolUse → Validate/enforce routing decisions
- **session-start.py**: Start of session → Refresh Claude usage + profile
- **session-end.py**: End of session → Log savings, cumulative spend
- **usage-refresh.py**: PostToolUse (LLM tools) → Update cost tracking
- **agent-route.py**: PreToolUse (Agent) → Route agent tasks to cheap models
- **New in v7.5**: Playwright/Bash compress hooks (reduce context size on lengthy operations)

## Tools Modules (src/llm_router/tools/, 8 files)

| Module | Tools | Purpose |
|--------|-------|---------|
| `routing.py` | `llm_classify`, `llm_route`, `llm_track_usage`, `llm_stream` | Core routing MCP tools |
| `text.py` | `llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`, `llm_edit` | Text generation tasks |
| `media.py` | `llm_image`, `llm_video`, `llm_audio` | Media generation |
| `pipeline.py` | `llm_orchestrate`, `llm_pipeline_templates` | Multi-step workflows |
| `admin.py` | `llm_usage`, `llm_health`, `llm_policy`, `llm_digest`, `llm_benchmark` | Dashboards & reports |
| `subscription.py` | `llm_check_usage`, `llm_update_usage`, `llm_refresh_claude_usage` | Claude usage management |
| `setup.py` | `llm_setup`, `llm_quality_report`, `llm_save_session` | Configuration & reporting |
| `codex.py` | `llm_codex` | Direct Codex routing (bypass auto-routing) |

## Key Modules by Category

### Routing & Decision Making
| Module | Lines | Purpose |
|--------|-------|---------|
| `router.py` | 400 | Main routing orchestrator + fallback chains |
| `classifier.py` | 300 | Task complexity classification (3-layer) |
| `model_selector.py` | 250 | Model chain building + budget awareness |
| `profiles.py` | 180 | Routing tables per profile/task_type |
| `dynamic_routing.py` | 200 | Adaptive chain building based on provider health |
| `prompt_cache.py` | 150 | Classification result caching |

### Budget & Cost Tracking
| Module | Lines | Purpose |
|--------|-------|---------|
| `cost.py` | 400 | SQLite usage tracking + savings calculation |
| `claude_usage.py` | 250 | Live Claude subscription monitoring (OAuth) |
| `budget.py` | 150 | Budget enforcement + pressure calculation |
| `budget_store.py` | 120 | SQLite budget store (daily caps, resets) |
| `provider_budget.py` | 130 | Per-provider budget tracking |
| `quota_tracker.py` | 140 | Quota balance monitoring across subscriptions |

### Provider Integration
| Module | Lines | Purpose |
|--------|-------|---------|
| `providers.py` | 350 | Provider APIs (OpenAI, Gemini, Codex, Perplexity) |
| `codex_agent.py` | 200 | Local Codex binary detection & execution |
| `gemini_cli_agent.py` | 180 | Gemini CLI detection & integration |
| `health.py` | 200 | Circuit breaker + rate limit detection per provider |
| `model_evaluator.py` | 180 | Benchmark provider quality + latency |
| `model_tracking.py` | 150 | Model-specific performance logging |

### Configuration & State
| Module | Lines | Purpose |
|--------|-------|---------|
| `config.py` | 150 | Pydantic settings from env + fallback config.yaml |
| `state.py` | 100 | Shared mutable state (profile, last_usage) |
| `policy.py` | 200 | Org/repo/user routing policies (YAML-based) |
| `types.py` | 300 | All dataclasses, enums, type definitions |

### Analysis & Reporting
| Module | Lines | Purpose |
|--------|-------|---------|
| `scorer.py` | 180 | Quality scoring + judge LLM integration |
| `judge.py` | 160 | Judge model for quality evaluation |
| `retrospective.py` | 140 | Session retrospectives + savings reports |
| `forecast.py` | 130 | Burn rate forecasting |
| `model_tracking.py` | 150 | Track model performance over time |

## Data Flow

### Request → Response
1. User calls MCP tool (e.g., `/llm_code`)
2. Tool routes to `route_and_call(task_type, prompt, profile)`
3. Classifier → Determines complexity (3-layer: heuristic, Ollama, API)
4. Policy check → Verify task allowed for profile
5. Model selector → Builds chain (Ollama → Codex → Paid APIs)
6. Try each model in chain until success
7. Log routing decision to SQLite (tokens, cost, latency, quality)
8. Return `LLMResponse` to user

### Cost Tracking Flow
- **On each call**: Record to `routing_decisions` table (model, cost_usd, tokens, latency, success)
- **Provider health**: Update circuit breaker status + rate limit info
- **Session end**: Aggregate spend by provider + complexity tier
- **Dashboard**: `llm_usage()` shows spend vs budget, forecast

### Budget Pressure Update
- **Session start**: Fetch Claude subscription usage via OAuth → cache to `~/.llm-router/usage.json`
- **Per-call**: Calculate pressure = max(session%, weekly%, sonnet%, opus%)
- **Routing adjustment**: If pressure ≥95%, downgrade complexity + prioritize free models
- **Team level**: If org has daily cap → track spend against cap per user

## External Dependencies

### Cloud APIs (20+ providers)
- **OpenAI**: gpt-5.4, o3, gpt-4o, gpt-4o-mini
- **Google**: Gemini 2.5 Flash, Gemini Pro 2, Gemini 2.0 Flash
- **Perplexity**: Web-grounded research
- **Local**: Ollama (free, self-hosted)
- **Codex**: OpenAI subscription (free for subscribers)
- **Groq**: Fast inference API
- **DeepSeek**: Cost-optimized models

### Python Packages (Core)
- **litellm** (1.50.0+): Unified LLM API
- **pydantic** (2.0+): Settings validation
- **structlog** (24.4.0+): Structured logging
- **aiosqlite** (0.20.0+): Async SQLite
- **fastapi** (0.100.0+), **uvicorn** (0.24.0+): Dashboard HTTP server
- **aiohttp** (3.9.0+): Async HTTP client

### Storage
- **SQLite** (`~/.llm-router/usage.db`): Routing decisions, budgets, metrics
- **JSON** (`~/.llm-router/usage.json`): Claude subscription pressure cache
- **YAML** (`.llm-router.yml`, `~/.llm-router/config.yaml`): Org/repo/user config + policies

## Test Coverage

- **Test files**: 84 (up from 50+)
- **Total tests**: 250+ (all passing)
- **Unit tests**: 150+ (routing, classification, cost, policy)
- **Integration tests**: 50+ (provider APIs, SQLite, hooks)
- **End-to-end**: 20+ (full routing chains, multi-hop fallback)
- **Markers**: `@pytest.mark.slow`, `@pytest.mark.integration`, `@pytest.mark.requires_ollama`
- **Success rate**: 100% (all tests passing)

## Version & Release

- **Current**: v7.5.0
- **Latest features**: Flexible routing policies, team quota tracking, judge scoring, burn rate forecasting
- **Release process**: Version sync (pyproject.toml + plugin.json + marketplace.json) → tests → commit → tag → PyPI
- **Automated**: `bash scripts/release.sh` handles all steps with rollback on failure

## Architecture Highlights (v7.5.0)

### 1. Policy-Driven Routing (NEW)
- Users can choose routing policy: aggressive/balanced/conservative
- Policy controls: confidence threshold, skip patterns, provider ordering
- Applied at session start via `LLM_ROUTER_POLICY` env var
- Enables org-wide routing standards via `~/.llm-router/org-policy.yaml`

### 2. Provider Health Tracking (ENHANCED)
- Circuit breaker per provider (3 failures → mark unhealthy)
- Rate limit detection + exponential backoff
- Health status in `llm_health()` tool
- Affects model ordering when provider unhealthy

### 3. Caveman Mode (OUTPUT OPTIMIZATION)
- Reduces output tokens ~75% (fragments, no filler)
- 4 intensity levels: off/lite/full/ultra
- Preserves all technical detail, removes prose
- Controlled via `LLM_ROUTER_CAVEMAN_INTENSITY` env var

### 4. Team Features (ENTERPRISE)
- Multi-user quota tracking (per-user daily spend caps)
- Org routing policies + audit logging
- Team savings reports + cost distribution
- Webhook notifications for policy violations

### 5. Quality Scoring (NEW)
- Judge LLM rates routing decisions (good/bad)
- Tracks confidence over time
- Feeds into `llm_benchmark()` quality reports
- Users rate responses via `llm_rate()` tool

## Recent Changes (since 2026-04-16)

| Category | Change | Impact |
|----------|--------|--------|
| Policies | Added flexible routing policies | More user control over routing behavior |
| Team | Added multi-user quota tracking | Enterprise feature for org management |
| Quality | Added judge scoring system | Better routing decision validation |
| Hooks | Added Playwright/Bash compress hooks | Reduced context bloat on long operations |
| Tools | Added `llm_codex` direct tool | Bypass auto-routing when needed |
| Tests | Grew from 50 → 84 test files | Better coverage of new features |
