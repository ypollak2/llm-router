# LLM Router — Project Instructions

## Auto-Routing Rule

When a user's task would clearly benefit from an external LLM — research requiring web access, content generation, deep analysis beyond your training data, or code generation that could leverage a specialized model — **automatically use the appropriate `llm_*` MCP tool** without the user needing to type `/route`.

### Decision Matrix

| Signal in user prompt | Action |
|---|---|
| Research, current events, "what's the latest" | `llm_research` |
| "Write", "draft", "create content", brainstorming | `llm_generate` |
| Deep analysis, compare, evaluate, debug complex logic | `llm_analyze` |
| "Generate code", "implement", "refactor" (for external model) | `llm_code` |
| Simple factual question, quick lookup | `llm_query` |
| Image/visual request | `llm_image` |
| Multi-step pipeline | `llm_orchestrate` |

### When NOT to auto-route

- Tasks you can handle directly (file edits, git operations, shell commands)
- Questions about this codebase specifically (use Read/Grep instead)
- When the user explicitly asks YOU to do something, not an external LLM

### Routing Hint

If a `[ROUTE: <task_type>/<complexity>]` hint appears at the start of the conversation context, use it to select the right tool and skip re-classification. This hint is injected by the auto-classify hook.

## Development

```bash
# Run tests
uv run pytest tests/ -x -q

# Lint
uv run ruff check src/ tests/

# Run single test
uv run pytest tests/test_classifier.py -x -q

# Build
uv build

# Run server locally
uv run llm-router
```

## Architecture

- `src/llm_router/server.py` — MCP tool definitions (FastMCP)
- `src/llm_router/router.py` — Core routing with fallback chains
- `src/llm_router/classifier.py` — LLM-based complexity classification
- `src/llm_router/model_selector.py` — Budget-aware model selection
- `src/llm_router/profiles.py` — Routing tables per profile/task_type
- `src/llm_router/types.py` — All dataclasses and enums (frozen)
- `src/llm_router/config.py` — Pydantic settings from env vars
- `src/llm_router/cost.py` — SQLite usage tracking
- `src/llm_router/health.py` — Circuit breaker per provider
- `src/llm_router/claude_usage.py` — Live Claude subscription monitoring
- `src/llm_router/provider_budget.py` — External provider budgets
- `src/llm_router/codex_agent.py` — Local Codex integration
- `src/llm_router/cache.py` — Prompt classification cache (SHA-256 + LRU)

## Patterns

- All dataclasses are `frozen=True` — never mutate, create new instances
- All routing/API calls are `async def`
- Budget pressure is applied fresh per call (never cached)
- Classification results ARE cached (complexity doesn't change with budget)
- MCP tools return formatted strings, not structured data
