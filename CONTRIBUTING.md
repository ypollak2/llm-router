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
3. Update the onboarding wizard in `src/llm_router/onboard.py`
4. Add integration tests in `tests/test_integration.py`
5. Update `docs/PROVIDERS.md` with setup instructions

Most providers work through LiteLLM with zero custom code — just add the model string and key.

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
