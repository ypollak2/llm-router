# Contributing to LLM Router

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/ypollak2/llm-router.git
cd llm-router
uv sync --extra dev
cp .env.example .env
# Add at least one API key to .env
```

## Running Tests

```bash
# Unit tests (no API keys needed)
uv run pytest tests/ -v --ignore=tests/test_integration.py

# Integration tests (requires API keys in .env)
uv run pytest tests/test_integration.py -v

# Full suite
uv run pytest -v

# Lint
uv run ruff check src/
```

## Adding a New Provider

1. Add the model mapping to `src/llm_router/profiles.py`
2. Add the provider's API key to `src/llm_router/config.py`
3. Add the provider to `_PROVIDER_REGISTRY` in `src/llm_router/server.py` (for `llm_setup`)
4. Update the onboarding wizard in `src/llm_router/onboard.py`
5. Add integration tests in `tests/test_integration.py`
6. Update `docs/PROVIDERS.md` with setup instructions

Most providers work through LiteLLM with zero custom code — just add the model string and key.

## Key Modules

| Module | Purpose |
|--------|---------|
| `server.py` | MCP tool definitions (20 tools), `llm_setup` provider registry |
| `router.py` | Complexity classification and model selection logic |
| `profiles.py` | Model lists per routing profile (budget/balanced/premium) |
| `config.py` | Environment-based configuration, provider detection |
| `claude_usage.py` | Claude subscription usage parsing, time-aware pressure |
| `codex_agent.py` | Codex desktop CLI integration (local, free via OpenAI sub) |
| `provider_budget.py` | Per-provider budget tracking and enforcement |
| `orchestrator.py` | Multi-step pipeline execution across providers |
| `cost_tracker.py` | SQLite-based cost and token tracking |
| `health.py` | Provider health checks with circuit breaker pattern (incl. rate limit detection) |
| `cache.py` | Prompt classification cache (SHA-256 exact-match, LRU, 1h TTL) |
| `classifier.py` | LLM-based complexity classification (integrates with cache) |

## Hook Testing

The auto-route hook (`.claude/hooks/auto-route.py`) is a `UserPromptSubmit` hook that runs on every prompt. When modifying it:

- Test with various prompt types (research, code, analysis, simple queries)
- Verify it runs in ~0ms (heuristic only, no LLM calls)
- Ensure it gracefully handles edge cases (empty prompts, very long prompts)

## Pull Request Guidelines

- One concern per PR
- Include tests for new functionality
- Run `uv run ruff check src/` before submitting
- Follow existing code style (Pydantic models, async/await, type hints)
- Update the README if adding user-facing features

## Code Style

- Python 3.10+ with type hints
- `snake_case` for functions/variables, `PascalCase` for classes
- Pydantic for config and external data
- Async/await throughout (MCP server is async)
- Keep files under 400 lines

## Commit Messages

```
feat: add Runway video generation support
fix: circuit breaker not resetting after cooldown
docs: add ElevenLabs setup guide
test: add integration tests for image routing
```

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
