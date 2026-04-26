<!-- Generated: 2026-04-26 | Version: 7.5.0 | 48+ tools exposed | Token estimate: ~1100 -->

# MCP Tools API Reference (v7.5.0)

## Quick Reference

| Category | Tool Count | Purpose |
|----------|-----------|---------|
| Routing | 4 | Core routing + classification + streaming |
| Text Generation | 6 | Query, research, generate, analyze, code, edit |
| Media | 3 | Image, video, audio generation |
| Pipeline | 2 | Multi-step workflows + templates |
| Admin | 5 | Usage, health, policy, digest, benchmark |
| Subscription | 3 | Claude usage tracking + OAuth |
| Setup | 3 | Configuration + reporting + session save |
| Codex | 1 | Direct Codex routing |
| **Total** | **48+** | **All available via MCP** |

---

## Routing Tools (src/llm_router/tools/routing.py)

### llm_classify
- **Purpose**: Determine task complexity (simple/moderate/complex)
- **Signature**: `(prompt, quality?, min_model?, model?)`
- **Returns**: Classification result with confidence %, recommended model
- **Layers**: Heuristics → Ollama → Gemini Flash (fast, free, no API key needed)
- **Cache**: SHA-256(prompt) LRU, 80%+ hit rate
- **New in v7.5**: Confidence scoring per layer + model override

### llm_route
- **Purpose**: Smart routing with full decision chain
- **Signature**: `(prompt, task_type, complexity_override?, system_prompt?, context?, max_tokens?)`
- **Returns**: LLMResponse (model, output, cost_usd, latency_ms, tokens_used, quality_score)
- **Flow**: Classify → Select chain → Try models → Log → Return
- **Fallback**: Automatic retry on provider error
- **New in v7.5**: Policy-aware routing + quality scoring

### llm_track_usage
- **Purpose**: Manually log a routing decision to SQLite
- **Signature**: `(model, tokens_used, complexity?, cost_usd?)`
- **Returns**: Confirmation + updated cumulative stats
- **Use**: Track external model calls not made via llm-router
- **Example**: Used to record manual Opus calls or third-party LLM usage

### llm_stream
- **Purpose**: Streaming response for long-running tasks
- **Signature**: `(prompt, task_type, model?, system_prompt?, context?, max_tokens?)`
- **Returns**: Text stream (outputs as it arrives)
- **Use**: Real-time feedback for long analysis/generation tasks
- **New in v7.5**: Supports streaming from all providers

---

## Text Generation Tools (src/llm_router/tools/text.py)

| Tool | Param | Default Chain | Best For | Complexity |
|------|-------|---|---|---|
| `llm_query` | `(prompt, ...)` | Ollama→Codex→Flash→Groq | Simple Q&A, fact lookup | Simple |
| `llm_research` | `(prompt, ...)` | Perplexity (web-grounded) | Current events, web search | Moderate |
| `llm_generate` | `(prompt, complexity?, system_prompt?, ...)` | Ollama→Codex→Flash→Sonnet | Content creation, writing | Moderate |
| `llm_analyze` | `(prompt, complexity?, system_prompt?, context?, ...)` | Ollama→Codex→o3→Opus | Deep analysis, debugging, reviews | Complex |
| `llm_code` | `(prompt, complexity?, system_prompt?, context?, ...)` | Ollama→Codex→o3→Sonnet | Code generation, refactoring, architecture | Complex |
| `llm_edit` | `(task, files[], context?)` | Same as llm_code | Bulk edits across files | Moderate |

### New in v7.5: Auto-Route Hook Detection
The auto-route hook now detects when tasks fit these patterns and suggests routing:
- "Write X" / "Draft Y" → suggests `llm_generate`
- "Implement X" / "Add feature" → suggests `llm_code`
- "Analyze X" / "Compare Y" → suggests `llm_analyze`
- "Research X" / "What's latest" → suggests `llm_research`

---

## Media Tools (src/llm_router/tools/media.py)

| Tool | Model | Params | Output | Cost |
|------|-------|--------|--------|------|
| `llm_image` | DALL-E 3 / Gemini Imagen / Flux | `(prompt, model?, size?, quality?)` | Image URL | $0.01–0.04 |
| `llm_video` | Gemini Veo-2 / Runway Gen3 | `(prompt, model?, duration?)` | Video file | $0.02–0.10 |
| `llm_audio` | OpenAI TTS / ElevenLabs | `(text, model?, voice?)` | Audio file | $0.001–0.05 |

---

## Pipeline Tools (src/llm_router/tools/pipeline.py)

### llm_orchestrate
- **Purpose**: Multi-step LLM workflows with automatic decomposition
- **Signature**: `(task, template?, steps?, max_budget_usd?)`
- **Templates**:
  - `research_report` (3 steps: research → outline → write)
  - `code_review_fix` (3 steps: review → identify issues → fix)
  - `content_pipeline` (4 steps: outline → draft → refine → finalize)
  - `competitive_analysis` (research competitors → compare → synthesize)
- **Tiers**: Free=2 steps max, Pro=unlimited + auto-decomposition
- **Returns**: Pipeline result with step outputs + total cost

### llm_pipeline_templates
- **Purpose**: List available pipeline templates
- **Returns**: JSON array with template names, steps, descriptions, estimated cost

---

## Admin Tools (src/llm_router/tools/admin.py)

### llm_usage
- **Purpose**: Dashboard of routing spend & budget status
- **Signature**: `(period?: "today"|"week"|"month"|"all")`
- **Shows**:
  - Total spend this period
  - Provider breakdown (OpenAI, Gemini, Ollama, etc.)
  - Monthly budget progress %
  - Daily burn rate + forecast
  - Budget cap status
- **Example output**: `$2.45 this week (18% of $50 monthly cap). Forecast: $10.50/month`

### llm_health
- **Purpose**: Provider health status (availability, latency, rate limits)
- **Returns**: Per-provider status matrix:
  - Healthy (green) / Degraded (yellow) / Down (red)
  - Last response time (ms)
  - Error count (24h)
  - Rate limit status
  - Circuit breaker state
- **Triggers**: Circuit breaker activates after 3 consecutive failures

### llm_policy
- **Purpose**: Show active routing policy (merged from org/repo/user levels)
- **Returns**:
  - Policy name + description
  - Providers blocked/allowed
  - Task caps (e.g., max 10 CODE tasks/hour)
  - Confidence thresholds
  - Skip patterns (regex for prompts never routed)

### llm_digest
- **Purpose**: Savings summary + spend spike detection
- **Signature**: `(period?: "today"|"week"|"month"|"all", send?: boolean)`
- **Shows**:
  - Actual spend vs Opus baseline (if all used Opus)
  - Total $ saved
  - Efficiency multiplier (X.XXx cheaper)
  - Spend trend (↑ spike, ↓ declining, → flat)
  - Top cost drivers (which tasks cost most)
- **Send option**: POST digest to webhook if configured

### llm_benchmark
- **Purpose**: Routing accuracy report by task type
- **Signature**: `(days?: 7)`
- **Returns**:
  - % good / bad / neutral by task type (CODE, ANALYZE, GENERATE, QUERY, RESEARCH)
  - Top models by success rate
  - Trend analysis (improving / declining)
  - Confidence: "85% after 20 rated calls"
- **Requires**: User ratings via `llm_rate()` to build confidence

---

## Subscription Tools (src/llm_router/tools/subscription.py)

### llm_check_usage
- **Purpose**: Live Claude subscription usage (session, weekly, monthly quota)
- **Returns**: JSON with:
  - Session usage %
  - Weekly reset date + usage %
  - Extra spend $ (if any)
  - Quota reset times (Unix timestamps)
- **Auth**: Uses OAuth token from system Keychain (macOS) or ENV (Linux)
- **New in v7.5**: Faster caching, more granular quota breakdown

### llm_update_usage
- **Purpose**: Update local usage cache from API response
- **Signature**: `(data: JSON from llm_check_usage)`
- **Effect**: Feeds into budget pressure calculations
- **Used by**: Session-start hook to auto-refresh quota

### llm_refresh_claude_usage
- **Purpose**: Fetch fresh Claude usage via OAuth (no browser needed)
- **Works on**: macOS (reads Keychain), Linux (env vars)
- **Returns**: Updated usage JSON + cache timestamp
- **New in v7.5**: Async fetch with timeout

---

## Setup & Config Tools (src/llm_router/tools/setup.py)

### llm_setup
- **Purpose**: Configure API keys, routing profile, tier, budget
- **Actions**:
  - `status` — Show current configuration
  - `guide` — Interactive setup wizard
  - `discover` — Scan for existing API keys in environment
  - `add` — Add/update API key for provider
  - `test` — Validate API key with test call
  - `provider` — Show details about a provider
  - `install_hooks` — Install auto-routing hooks globally
- **Example**: `llm_setup(action="add", provider="openai", api_key="sk-...")`

### llm_quality_report
- **Purpose**: Show routing quality metrics
- **Returns**:
  - % good / bad per task type
  - Trend (↑ improving, ↓ declining)
  - Confidence level ("High" after 50+ calls, "Low" after 5)
- **New in v7.5**: Per-provider quality breakdown

### llm_save_session
- **Purpose**: Persist session notes for continuity
- **Signature**: `(summary, decisions?, pending?)`
- **Storage**: `~/.llm-router/sessions/YYYY-MM-DD_HHmmss.md`
- **Use**: Resume context in next session

---

## Codex Tool (src/llm_router/tools/codex.py)

### llm_codex
- **Purpose**: Direct Codex routing (bypass auto-route hook)
- **Signature**: `(prompt, model?: "gpt-5.4"|"o3"|"o4-mini"|"gpt-4o")`
- **Use**: When auto-routing would be suboptimal
- **Example**: "I know this needs o3, route directly"
- **New in v7.5**: Explicit model selection

---

## Tool Routing Strategy

### Free-First Chain (Default)
```
Simple tasks:    Ollama → Codex → Gemini Flash → Groq → GPT-4o-mini
Moderate tasks:  Ollama → Codex → GPT-4o → Gemini Pro → Sonnet (subscription)
Complex tasks:   Ollama → Codex → o3 → Gemini Pro → Opus (subscription)
Research:        Perplexity (web-grounded, always)
```

### Policy-Driven Chains (NEW in v7.5)
**Aggressive**: Fast-first, more Ollama/Codex, skip acks
- Saves 60–75% vs Opus baseline
- Good for high-volume development

**Balanced**: Cost/quality tradeoff, normal chains
- Saves 35–45% vs Opus baseline
- Good for mixed workloads (default)

**Conservative**: Quality-first, less Ollama, more paid
- Saves 10–15% vs Opus baseline
- Good for critical work

### Under Pressure (Subscription ≥95%)
1. Downgrade task complexity: `complex` → `moderate`, `moderate` → `simple`
2. Push to external APIs: Codex or paid (OpenAI, Gemini)
3. Reserve subscription for critical tasks only

### Codex Injection (ALWAYS in chain)
- **Before paid externals**: Codex is prepaid, free to use
- **After Ollama**: Codex > any API-key model for cost efficiency
- **High pressure**: Codex at front even before free Ollama (if Ollama slower)

---

## Error Handling & Fallback

| Scenario | Response | Fallback |
|----------|----------|----------|
| Model fails | Try next in chain | Last resort: return error |
| All models fail | BudgetExceededError | Escalate to user |
| Rate limited | Exponential backoff (1s, 2s, 4s, ...) | Skip provider for 24h |
| Network timeout | Retry 3x with jitter | Circuit breaker |
| No API keys | Use Ollama only | Error if Ollama unavailable |
| Policy violation | Suggest compliant alternative | Block if org policy blocks |

---

## Tool Tiers & Rate Limits

| Tier | Sessions/Month | Tools/Session | Pipeline Steps | Cost Cap | Codex | Ollama |
|------|---|---|---|---|---|---|
| Freemium | Unlimited | 50/session | 2 steps max | None | ✅ | ✅ |
| Pro | Unlimited | Unlimited | Unlimited | Custom | ✅ | ✅ |
| Team | Unlimited | Unlimited | Unlimited | Per-user | ✅ | ✅ |

---

## Cost Breakdown by Tool Category

| Category | Avg Cost | Range | Cheapest |
|----------|----------|-------|----------|
| Query | <$0.001 | $0–0.0005 | Ollama |
| Research | $0.002 | $0.001–0.01 | Perplexity |
| Generate | $0.0003 | $0–0.01 | Ollama |
| Analyze | $0.01 | $0–0.20 | Ollama/Codex |
| Code | $0.008 | $0–0.10 | Ollama/Codex |
| Image | $0.015 | $0.01–0.04 | DALL-E |
| Video | $0.06 | $0.02–0.10 | Gemini Veo-2 |
| Audio | $0.01 | $0.001–0.05 | OpenAI TTS |

