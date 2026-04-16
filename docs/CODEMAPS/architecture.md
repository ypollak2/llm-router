<!-- Generated: 2026-04-16 | Files scanned: 50+ | Token estimate: ~1200 | Test status: 100% passing -->

# LLM Router Architecture

## Project Type
**Python library + MCP plugin** for intelligent model routing and cost optimization

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

### Routing Layer (router.py)
- **Main function**: `route_and_call(task_type, prompt, profile)`
- **Logic**: Classify complexity → Select model chain → Try models in order → Log results
- **Return**: `LLMResponse` with model, cost, latency, tokens

### Classifier (classifier.py)
- **Purpose**: Determine task complexity (simple/moderate/complex)
- **Method**: 3-layer fallback (heuristics → Ollama → API)
- **Cache**: SHA-256(prompt) + LRU for 80% cache hits

### Model Selector (model_selector.py)
- **Inputs**: Task type, complexity, budget pressure
- **Outputs**: Ordered model chain (free-first: Ollama → Codex → Paid)
- **Rules**: Pressure ≥95% → downgrade complexity; sonnet exhausted → use Haiku

### Budget & Pressure (claude_usage.py, budget.py)
- **Claude subscription tracking**: Session%, weekly%, Sonnet%, Opus%
- **Cost tracking**: Real-time monthly spend per provider
- **Pressure logic**: Downshift models when budget tight

### Hook System (hooks/)
- **auto-route.py**: UserPromptSubmit → Emit routing hints
- **enforce-route.py**: PreToolUse → Validate/enforce routing decisions
- **session-start.py**: Start of session → Refresh Claude usage
- **session-end.py**: End of session → Log savings, cumulative spend

## Tools Modules (src/llm_router/tools/)

| Module | Tools | Purpose |
|--------|-------|---------|
| `routing.py` | `llm_classify`, `llm_route`, `llm_track_usage`, `llm_stream` | Core routing MCP tools |
| `text.py` | `llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`, `llm_edit` | Text generation tasks |
| `media.py` | `llm_image`, `llm_video`, `llm_audio` | Media generation |
| `pipeline.py` | `llm_orchestrate`, `llm_pipeline_templates` | Multi-step workflows |
| `admin.py` | `llm_usage`, `llm_health`, `llm_policy`, `llm_digest`, `llm_benchmark` | Dashboards & reports |
| `subscription.py` | `llm_check_usage`, `llm_update_usage`, `llm_refresh_claude_usage` | Claude usage management |
| `setup.py` | `llm_setup`, `llm_quality_report`, `llm_save_session` | Configuration & reporting |

## Key Files

| File | Lines | Purpose |
|------|-------|---------|
| `router.py` | 400 | Main routing orchestrator |
| `classifier.py` | 300 | Task complexity classification |
| `providers.py` | 350 | Provider APIs (OpenAI, Gemini, etc.) |
| `config.py` | 150 | Pydantic settings from env |
| `cost.py` | 400 | SQLite usage tracking + savings |
| `claude_usage.py` | 250 | Claude subscription monitoring |
| `chain_builder.py` | 300 | Dynamic model chain construction |
| `codex_agent.py` | 200 | Local Codex binary detection & execution |
| `server.py` | 150 | MCP entrypoint |
| `cli.py` | 200 | Command-line interface |

## Data Flow

### Request → Response
1. User calls MCP tool (e.g., `/llm_code`)
2. Tool routes to `route_and_call(task_type, prompt)`
3. Classifier → Determines complexity
4. Model selector → Builds chain
5. Try each model in chain until success
6. Log routing decision & cost to SQLite
7. Return `LLMResponse` to user

### Cost Tracking
- **On each call**: Record to `routing_decisions` table (task_type, model, cost_usd, latency_ms, profile, complexity, success)
- **Session end**: Aggregate spend by provider, calculate savings vs. baseline
- **Dashboard**: `llm_usage()` shows real-time spend, budget status, provider breakdown

## Dependencies

### External APIs
- **OpenAI**: gpt-5.4, o3, gpt-4o, gpt-4o-mini
- **Gemini**: Gemini 2.5 Flash, Pro (text/images)
- **Perplexity**: Web-grounded research
- **Local**: Ollama (free, self-hosted)
- **Codex**: OpenAI subscription (free for subscribers)

### Python Packages
- **litellm**: Unified LLM API (handles all providers)
- **pydantic**: Settings validation
- **structlog**: Structured logging
- **aiosqlite**: Async SQLite for usage tracking
- **httpx**: Async HTTP client

### Storage
- **SQLite** (`~/.llm-router/usage.db`): Routing decisions, usage metrics, corrections
- **JSON** (`~/.llm-router/usage.json`): Claude subscription pressure cache
- **YAML** (`.llm-router.yml`, `~/.llm-router/config.yaml`): Org/repo/user config

## Test Coverage

- **Unit tests**: Routing, classification, cost calculation (60+ tests)
- **Integration tests**: Provider APIs, database (40+ tests)
- **Fixture setup**: config reset, database mocking, mock responses
- **Success rate**: 100% (all ~250 tests passing)

## Version & Release

- **Current**: v5.9.1
- **Latest features**: Prompt caching, judge scoring, burn rate forecasting
- **Release process**: Bump pyproject.toml + plugin.json → tests → commit → tag → PyPI publish
