<!-- Generated: 2026-04-26 | Test status: 100% passing (250+ tests, 84 test files) | Coverage: 80%+ | Token estimate: ~900 -->

# Testing & Development Guide (v7.5.0)

## Test Suite Status

✅ **All 250+ tests passing** (as of 2026-04-26)
- **Test files**: 84 (up from 50+)
- **Unit tests**: 150+ (routing, classification, cost, policy, hooks)
- **Integration tests**: 50+ (provider APIs, SQLite, OAuth, hook system)
- **E2E tests**: 20+ (full chains, fallback scenarios)
- **Coverage**: 80%+ of core routing logic
- **Execution time**: ~30–45 seconds full suite (parallel: ~15s)

### Test Categories
```
Routing:           test_router.py, test_route.py, test_adaptive_router.py
Classification:    test_classifier.py, test_classifier_eval.py
Cost/Budget:       test_cost.py, test_budget.py, test_budget_store.py
Policy/Teams:      test_policies.py, test_policy_integration.py, test_team.py
Hooks:             test_hooks.py, test_enforce_route_safety.py, test_hook_health.py
Providers:         test_codex_routing.py, test_gemini_cli.py, test_integration.py
Quality/Judge:     test_judge.py, test_scorer.py, test_quality_logging.py
Freemium/Tiers:    test_freemium.py, test_tool_tiers.py, test_rate_limit.py
Advanced:          test_orchestrator.py, test_streaming.py, test_compression.py
```

---

## Running Tests

### Fast Tests (10–15s, dev iteration)
```bash
# Only fast-marked tests (default)
uv run pytest tests/ -q

# Single file
uv run pytest tests/test_router.py -xvs

# Single test
uv run pytest tests/test_router.py::test_route_and_call -xvs
```

### Full Test Suite (30–45s)
```bash
# Include slow tests (integration, provider calls)
uv run pytest tests/ -m "" -q

# With coverage report
uv run pytest tests/ -m "" --cov=src/llm_router --cov-report=term-missing -q
```

### Parallel Execution (faster on multi-core)
```bash
# Auto-detect CPU cores
uv run pytest tests/ -n auto -q

# Fixed worker count
uv run pytest tests/ -n 4 -m "" -q
```

### Selective Test Markers
```bash
# Exclude slow tests
uv run pytest tests/ -m "not slow" -q

# Only integration tests
uv run pytest tests/ -m "integration" -q

# Skip Ollama-dependent tests (CI environment)
uv run pytest tests/ -m "not requires_ollama" -q
```

---

## Key Fixtures (conftest.py)

### Singleton Reset (autouse)
```python
@pytest.fixture(autouse=True)
def _reset_config_singleton():
    """Reset config singleton before/after each test."""
    import llm_router.config as config_module
    config_module._config = None
    yield
    config_module._config = None
```
**Why**: Config caches at import time; must reset after monkeypatch.

### mock_env
```python
@pytest.fixture
def mock_env(monkeypatch):
    """Set API keys, profile, env vars for tests."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    yield
```

### mock_acompletion
```python
@pytest.fixture
def mock_acompletion(monkeypatch):
    """Mock litellm.acompletion to prevent real API calls."""
    def mock_func(*args, **kwargs):
        return {"choices": [{"message": {"content": "mocked"}}],
                "usage": {"total_tokens": 100}}
    monkeypatch.setattr("litellm.acompletion", mock_func)
    yield
```

### temp_db
```python
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Provide clean SQLite database for each test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", db_path)
    # Reset config to pick up new path
    import llm_router.config as config_module
    config_module._config = None
    yield db_path
    # Cleanup automatic via tmp_path
```

### mock_claude_usage (NEW in v7.5)
```python
@pytest.fixture
def mock_claude_usage(monkeypatch):
    """Mock Claude subscription usage API."""
    usage_data = {
        "session_usage_pct": 45,
        "weekly_usage_pct": 60,
        "total_spend_usd": 12.50,
        "reset_time_unix": 1719792000
    }
    monkeypatch.setattr(
        "llm_router.claude_usage.fetch_claude_usage",
        lambda: usage_data
    )
    yield
```

---

## Common Test Patterns

### Testing Routing Chains
```python
@pytest.mark.asyncio
async def test_fallback_to_next_model(mock_acompletion, monkeypatch):
    # Make first model fail
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)

    with patch("litellm.acompletion", side_effect=RuntimeError("Fail")):
        resp = await route_and_call(TaskType.CODE, "write code")
        assert resp.provider == "codex"  # Second model tried
        assert resp.success
```

### Testing Budget Pressure
```python
@pytest.mark.asyncio
async def test_downgrade_on_pressure(mock_env, mock_claude_usage, monkeypatch):
    # Mock high pressure (≥95%)
    monkeypatch.setattr("llm_router.claude_usage.get_pressure",
                       return_value=0.97)

    resp = await route_and_call(TaskType.CODE, "implement",
                               profile=RoutingProfile.BALANCED)
    # Should use cheaper model (Ollama/Codex) due to pressure
    assert resp.provider in ("ollama", "codex")
```

### Testing Policy Enforcement
```python
def test_policy_blocks_model(mock_env, monkeypatch, temp_db):
    # Set org policy that blocks o3
    policy = Policy(blocked_models=["o3"])
    monkeypatch.setattr("llm_router.policy.load_policy",
                       return_value=policy)

    # Route should skip o3 and use next model
    resp = await route_and_call(TaskType.CODE, "complex task")
    assert resp.provider != "o3"
```

### Testing Ollama Isolation
```python
def test_codex_without_ollama(mock_env, monkeypatch, temp_db):
    # Disable Ollama to isolate Claude/Codex chain
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models",
                       lambda: [])

    # Reset config to pick up env changes
    import llm_router.config as config_module
    config_module._config = None

    # Now Claude should be first in chain
    resp = await route_and_call(TaskType.CODE, "write")
    assert resp.provider != "ollama"
```

### Testing Hook Registration (NEW in v7.5)
```python
def test_hook_fires_on_user_prompt():
    # Verify plan-nudge hook detects complex prompts
    hook = PlanNudgeHook()
    prompt = "implement and refactor authentication across all files"
    assert hook.should_fire(prompt) == True  # Multi-file + action verb

    prompt = "what time is it?"
    assert hook.should_fire(prompt) == False  # Simple query
```

---

## Critical Issues Fixed (Recent)

### Config Singleton Test Pollution
**Problem**: Tests pass individually but fail in suite
- Config `_config` cached at import time
- Monkeypatch env vars but config still reads stale value

**Solution**: Autouse fixture resets `config_module._config = None` before/after
- Applied to all test files
- Pattern: `reset_config_singleton` autouse fixture

### Ollama Discovery Cache Interference
**Problem**: Tests disabled Ollama but discovery cache had cached models
- Cache checked before env vars

**Solution**: Mock `llm_router.discover.get_cached_ollama_models()` to return `[]`
- Applied to codex routing tests
- Pattern: disable env + mock cache + reset config

### Hook Deadlock Prevention (NEW in v7.5)
**Problem**: enforce-route hook blocking Read/Edit/Bash created unresolvable deadlock
- Claude tries to fix hook
- Hook blocks Read/Edit/Bash
- Cannot read/fix hook source

**Solution**: Added test `test_enforce_route_safety.py`
- Verifies core tools never blocked simultaneously
- Early file-op detection exits BEFORE blocklist
- Auto-pivot downgrades blocking to soft warnings after 2 violations

---

## Test File Organization

| File | Purpose | Tests | Markers |
|------|---------|-------|---------|
| `test_router.py` | Main routing logic | 20+ | fast |
| `test_route.py` | Full route_and_call chain | 15+ | fast |
| `test_classifier.py` | Complexity classification | 10+ | fast |
| `test_codex_routing.py` | Codex fallback chains | 15 | fast |
| `test_cost.py` | Cost calculation | 8 | fast |
| `test_budget.py` | Budget enforcement | 10 | fast |
| `test_policies.py` | Routing policies | 12 | fast |
| `test_freemium.py` | Tier/cap enforcement | 28 | fast |
| `test_hooks.py` | Hook system | 20+ | fast |
| `test_enforce_route_safety.py` | Hook deadlock prevention | 8 | fast |
| `test_integration.py` | Provider APIs | 15 | integration, slow |
| `test_orchestrator.py` | Multi-step pipelines | 12 | slow |
| (70+ more) | All subsystems | 150+ | mixed |

---

## Development Workflow

### Adding a New Feature

1. **Write test first** (TDD)
   ```bash
   touch tests/test_my_feature.py
   # Add failing test
   ```

2. **Implement code**
   ```bash
   # Edit src/llm_router/my_module.py
   ```

3. **Run single test** (verify)
   ```bash
   uv run pytest tests/test_my_feature.py -xvs
   ```

4. **Run full suite** (before commit)
   ```bash
   uv run pytest tests/ -m "" -q
   ```

5. **Check linting**
   ```bash
   uv run ruff check src/ tests/
   ```

### Debugging Test Failures

```bash
# Show full traceback + local variables
uv run pytest tests/test_file.py::test_name -xvs --tb=long --showlocals

# Interactive debugger
uv run pytest tests/test_file.py::test_name -xvs --pdb

# Run without capturing output (print() visible)
uv run pytest tests/test_file.py -xvs --capture=no
```

### Checking Test Coverage

```bash
# Generate HTML report
uv run pytest tests/ -m "" --cov=src/llm_router --cov-report=html -q
open htmlcov/index.html

# Show missing lines
uv run pytest tests/ -m "" --cov=src/llm_router --cov-report=term-missing -q
```

---

## Common Pitfalls & Solutions

| Issue | Cause | Fix |
|-------|-------|-----|
| Test passes alone, fails in suite | Config singleton cached | Add `config_module._config = None` after monkeypatch |
| Ollama models unexpectedly in chain | Discovery cache bypass | Mock `get_cached_ollama_models()` to `[]` |
| Monkeypatch env var ignored | Config reads stale value | Reset config after ALL monkeypatches |
| Mock response missing fields | Structure mismatch | Copy from `mock_litellm_response` fixture |
| Async test hangs/timeout | Missing `@pytest.mark.asyncio` | Add marker or set asyncio_mode=auto |
| Database locked | Test didn't clean up | Use `temp_db` fixture |
| Test flaky/intermittent | Race condition in async | Add `await asyncio.sleep(0.1)` sync point |
| Provider test fails | Real API called instead of mock | Verify mock patches before import |

---

## Test Execution Performance

| Scenario | Time | Command |
|----------|------|---------|
| Single test | <1s | `pytest tests/test_router.py::test_one -xvs` |
| One file | 3–5s | `pytest tests/test_router.py -q` |
| Fast suite (50 tests) | 10–15s | `pytest tests/ -q` |
| Full suite (250 tests) | 30–45s | `pytest tests/ -m "" -q` |
| Full suite parallel (4 workers) | 12–18s | `pytest tests/ -n 4 -m "" -q` |
| Coverage + full suite | 45–60s | `pytest tests/ -m "" --cov=... --cov-report=html` |

---

## CI/CD Testing (GitHub Actions)

```yaml
# .github/workflows/ci.yml
- name: Run tests
  run: |
    uv run pytest tests/ -m "not slow and not requires_ollama" -q --tb=short

- name: Check linting
  run: uv run ruff check src/ tests/
```

Skipped in CI: `@pytest.mark.slow`, `@pytest.mark.requires_ollama`, `@pytest.mark.requires_api_keys`, `@pytest.mark.requires_codex`

