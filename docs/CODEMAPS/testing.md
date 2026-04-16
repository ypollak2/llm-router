<!-- Generated: 2026-04-16 | Test status: 100% passing (250+ tests) | Token estimate: ~800 -->

# Testing & Development Guide

## Test Suite Status

✅ **All 250+ tests passing** (as of 2026-04-16)
- Unit tests: 150+ (routing, classification, cost calculation)
- Integration tests: 50+ (provider APIs, database)
- Codex routing tests: 15 (all passing)
- Freemium/tier tests: 28 (all passing)
- Policy/digest tests: 24 (all passing)

## Running Tests

```bash
# Full suite (excluding integration tests)
uv run pytest tests/ -q --ignore=tests/test_agno_integration.py

# Single test file
uv run pytest tests/test_router.py -xvs

# Single test
uv run pytest tests/test_router.py::test_route_and_call -xvs

# With coverage
uv run pytest tests/ --cov=src/llm_router --cov-report=term-missing
```

## Key Fixtures (conftest.py)

### Singleton Reset (autouse)
```python
@pytest.fixture(autouse=True)
def _reset_config_singleton():
    """Reset config before/after each test — prevents test pollution."""
    import llm_router.config as config_module
    config_module._config = None
    yield
    config_module._config = None
```
**Why**: Config singleton cached at import time; must reset after monkeypatch.

### mock_env
```python
@pytest.fixture
def mock_env(monkeypatch):
    """Set API keys, profile, and other env vars for tests."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    yield
```

### mock_acompletion
```python
@pytest.fixture
def mock_acompletion():
    """Mock litellm.acompletion to prevent real API calls."""
    # Returns LLMResponse with mocked tokens and cost
    # Also patches health tracker to mark all providers healthy
```

### temp_db
```python
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Provide clean SQLite database for each test."""
    # Sets LLM_ROUTER_DB_PATH to tmp directory
    # Resets config singleton to pick up new path
```

---

## Common Test Patterns

### Testing Routing Chains
```python
@pytest.mark.asyncio
async def test_fallback_to_next_model(mock_acompletion, monkeypatch):
    # Make first model fail
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    
    # Mock first model to raise error
    with patch("litellm.acompletion", side_effect=RuntimeError("Fail")):
        # Second model should be tried
        resp = await route_and_call(TaskType.CODE, "write code")
        assert resp.provider == "codex"
```

### Testing with Budget Pressure
```python
@pytest.mark.asyncio
async def test_downgrade_on_pressure(mock_env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    
    # Mock high pressure
    with patch("llm_router.claude_usage.get_claude_pressure", return_value=0.97):
        resp = await route_and_call(TaskType.CODE, "implement feature", 
                                   profile=RoutingProfile.BALANCED)
        # Should use cheaper model (Codex or Ollama) due to pressure
```

### Testing Ollama Isolation
```python
def test_codex_without_ollama_interference(mock_env, monkeypatch):
    # Disable Ollama to isolate Claude/Codex chain
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: [])
    
    # Reset config to pick up env changes
    import llm_router.config as config_module
    config_module._config = None
    
    # Now Claude should be first in chain
```

---

## Critical Issues Fixed (Apr 16)

### Config Singleton Test Pollution
**Problem**: Tests passing individually but failing in full suite
- Config `_config` cached at import time
- Monkeypatch env vars but config still reads stale cached value

**Solution**: Autouse fixture resets `config_module._config = None` before/after each test
- Ensures config picks up monkeypatched values
- Prevents state leakage to subsequent tests

### Ollama Discovery Cache Interference
**Problem**: Tests disabled Ollama env var but discovery cache had cached models
- Cache checked before env vars, bypassing monkeypatch

**Solution**: Mock `llm_router.discover.get_cached_ollama_models()` to return `[]`
- Applied to all codex routing tests that need clean chain
- Pattern: disable env + mock cache + reset config

---

## Test File Organization

| File | Purpose | Tests |
|------|---------|-------|
| `test_router.py` | Main routing logic | 20+ |
| `test_classifier.py` | Complexity classification | 10+ |
| `test_codex_routing.py` | Codex fallback chains | 15 |
| `test_cost.py` | Cost calculation & tracking | 8 |
| `test_quality_logging.py` | Decision logging | 10+ |
| `test_freemium.py` | Budget enforcement, tier gating | 28 |
| `test_policy_digest_community.py` | Org policy, savings digest, benchmarks | 24 |
| `test_adaptive_router.py` | Dynamic chain building | 15 |
| (40+ more) | Providers, cache, budget, config | 150+ |

---

## Development Workflow

### Adding a New Feature
1. Write failing test (`test_new_feature_...`)
2. Implement code
3. Run single test to verify: `uv run pytest tests/test_new_feature.py -xvs`
4. Run full suite: `uv run pytest tests/ -q --ignore=tests/test_agno_integration.py`
5. Run linter: `uv run ruff check src/ tests/`

### Debugging Test Failures
```bash
# Show full traceback
uv run pytest tests/test_file.py::test_name -xvs --tb=long

# Show local variables at failure
uv run pytest tests/test_file.py::test_name -xvs --tb=long --showlocals

# Run with Python debugger
uv run pytest tests/test_file.py::test_name -xvs --pdb
```

### Checking Test Coverage
```bash
uv run pytest tests/ --cov=src/llm_router --cov-report=html
open htmlcov/index.html
```

---

## Common Pitfalls

| Issue | Cause | Fix |
|-------|-------|-----|
| Test passes alone, fails in suite | Config singleton cached | Add `config_module._config = None` after monkeypatch |
| Ollama models unexpectedly injected | Discovery cache bypass | Mock `get_cached_ollama_models()` to `[]` |
| Monkeypatch env var ignored | Config reads cached singleton | Reset config after all monkeypatches |
| Mock response missing fields | Mock structure mismatch | Copy structure from `mock_litellm_response` fixture |
| Async test hangs | Missing `@pytest.mark.asyncio` | Add marker or ensure asyncio mode is AUTO |
| Database locked | Test didn't clean up | Use `temp_db` fixture which handles cleanup |

