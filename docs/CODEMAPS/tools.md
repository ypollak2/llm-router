<!-- Generated: 2026-04-16 | Files scanned: routing, text, media, pipeline, admin, setup | Token estimate: ~1000 -->

# MCP Tools API Reference

## Routing Tools (src/llm_router/tools/routing.py)

### llm_classify
- **Purpose**: Determine task complexity (simple/moderate/complex)
- **Params**: `prompt`, `quality` (optional), `min_model` (optional)
- **Returns**: Classification with confidence, recommended model
- **Logic**: Heuristics → Ollama → Gemini Flash → default to moderate

### llm_route
- **Purpose**: Full smart routing with model selection
- **Params**: `prompt`, `task_type`, `complexity_override`, `system_prompt`, `max_tokens`
- **Returns**: LLMResponse with selected model, cost, latency
- **Chain**: Build → classify → select → try models → log & return

### llm_track_usage
- **Purpose**: Manually log a routing decision to SQLite
- **Params**: `model`, `tokens_used`, `complexity`, `cost_usd`
- **Returns**: Confirmation + cumulative stats
- **Use case**: Track external model calls not made through llm-router

### llm_stream
- **Purpose**: Streaming response for long-running tasks
- **Params**: `prompt`, `task_type`, `model` (optional), `system_prompt`
- **Returns**: Text stream (shows output as it arrives)

---

## Text Generation Tools (src/llm_router/tools/text.py)

| Tool | Task | Chain | Best for |
|------|------|-------|----------|
| `llm_query` | QUERY | Ollama→Codex→Flash→Groq | Simple factual questions |
| `llm_research` | RESEARCH | Perplexity (web-grounded) | Current events, web lookup |
| `llm_generate` | GENERATE | Ollama→Codex→Flash→Sonnet | Content creation, writing |
| `llm_analyze` | ANALYZE | Ollama→Codex→o3→Opus | Deep analysis, debugging |
| `llm_code` | CODE | Ollama→Codex→o3→Sonnet | Code generation, refactoring |
| `llm_edit` | CODE | Same | Cheap bulk edits across files |

---

## Media Tools (src/llm_router/tools/media.py)

| Tool | Model | Use |
|------|-------|-----|
| `llm_image` | DALL-E 3 / Gemini Imagen / Flux | Image generation |
| `llm_video` | Gemini Veo-2 / Runway Gen3 | Video generation (5-30s) |
| `llm_audio` | OpenAI TTS / ElevenLabs | Speech synthesis |

---

## Pipeline Tools (src/llm_router/tools/pipeline.py)

### llm_orchestrate
- **Purpose**: Multi-step LLM workflows
- **Params**: `task`, `template` (optional), `steps` (optional)
- **Templates**: `research_report` (3 steps), `code_review_fix`, `content_pipeline`
- **Free tier**: Max 2 steps; Pro tier: unlimited + auto-decomposition

### llm_pipeline_templates
- **Purpose**: List available pipeline templates
- **Returns**: JSON of template names, steps, descriptions

---

## Admin Tools (src/llm_router/tools/admin.py)

### llm_usage
- **Purpose**: Dashboard of routing spend & budget
- **Params**: `period` (today/week/month/all)
- **Shows**: Provider spend, monthly progress, budget cap, forecast
- **Example**: `"$0.42 this week (35% of monthly budget)"`

### llm_health
- **Purpose**: Provider health status (latency, errors, rate limits)
- **Returns**: Per-provider: healthy/degraded/down + last response time
- **Triggers**: Circuit breaker on 3 consecutive failures

### llm_policy
- **Purpose**: Show active routing policy (org + repo + user levels)
- **Returns**: Merged policy (providers blocked, models allowed, task caps)

### llm_digest
- **Purpose**: Savings summary + spend spike detection
- **Params**: `period`, `send` (optional, sends to webhook)
- **Shows**: Actual spend, baseline (if all Opus), $ saved, efficiency multiplier

### llm_benchmark
- **Purpose**: Routing decision quality report by task type
- **Returns**: Accuracy%, top models, ratings distribution, trend
- **Data**: Requires user ratings via `llm_rate` to build confidence

---

## Setup & Config Tools (src/llm_router/tools/setup.py)

### llm_setup
- **Purpose**: Configure API keys, routing profile, tier, budget
- **Actions**: `status`, `guide`, `discover`, `add`, `test`, `provider`, `install_hooks`
- **Example**: `llm_setup(action="add", provider="openai", api_key="sk-...")`

### llm_quality_report
- **Purpose**: Show routing accuracy benchmarks by task type
- **Returns**: % good/bad for each task type, trend analysis
- **Confidence**: "X% confident after N rated calls"

### llm_save_session
- **Purpose**: Persist session notes for next session
- **Params**: `summary`, `decisions`, `pending` (optional)
- **Storage**: `~/.llm-router/sessions/YYYY-MM-DD_HHmmss.md`

---

## Subscription Tools (src/llm_router/tools/subscription.py)

### llm_check_usage
- **Purpose**: Live Claude subscription usage (session%, weekly%, extra spend)
- **Returns**: JSON with all quota details + reset times
- **Requires**: ANTHROPIC_API_KEY (via OAuth token from Keychain)

### llm_update_usage
- **Purpose**: Update local usage cache from API response
- **Params**: `data` (JSON from llm_check_usage)
- **Effect**: Feeds into budget pressure calculations

### llm_refresh_claude_usage
- **Purpose**: Fetch fresh Claude usage via OAuth (macOS Keychain)
- **Returns**: Updated usage cache
- **Requires**: Claude Code OAuth token stored in Keychain

---

## Tool Routing Strategy

### Free-First Chain (No budget pressure)
```
simple:   Ollama → Codex/gpt-5.4 → Gemini Flash → Groq → GPT-4o-mini
moderate: Ollama → Codex/gpt-5.4 → GPT-4o → Gemini Pro → Sonnet (sub)
complex:  Ollama → Codex/gpt-5.4 → o3 → Gemini Pro → Opus (sub)
```

### Under Pressure (Sonnet ≥95% or Weekly ≥95%)
- Downgrade complexity: `complex` → `moderate`, `moderate` → `simple`
- Push to external models: Codex or paid APIs (OpenAI, Gemini)
- Reserve subscription for critical tasks

### Codex Injection Logic
- **CODE tasks**: After first Claude (if available), before paid externals
- **ANALYZE/GENERATE/QUERY**: Before all paid externals (subscription is free)
- **High pressure (≥95%)**: Codex at front, even before Claude

---

## Error Handling

| Scenario | Response | Fallback |
|----------|----------|----------|
| Model fails | Try next in chain | Last resort: return error |
| All models fail | BudgetExceededError | Escalate to user |
| No API keys | Use Ollama only | Error if Ollama unavailable |
| Rate limited | Exponential backoff | Dex next day |
| Network error | Retry with jitter | Circuit breaker after 3 failures |

