# Audit Remediation Plan — v5.2.0

> Generated from: Senior Engineering Audit (2026-04-14)
> Target release: v5.2.0 (all phases)

---

## Summary

14 actionable findings across three urgency tiers. 5 are security/data-integrity issues
fixable in under an hour each. 4 are reliability issues requiring hook changes and new
tests. 4 are longer-horizon engineering investments.

**Explicitly out of scope** (low ROI, do not implement):
- SHA-256 collision detection in cache
- LFU vs LRU cache eviction
- SQLite foreign key constraints
- HuggingFace free-tier hardcoded list
- Temperature clamping in providers.py

---

## Phase 1 — Security + Data Integrity (Week 1)

Five self-contained fixes. Each is independently committable.
Estimated total: 4–6 hours.

---

### Task 1.1 — Whitelist `extra_params` in router.py

**Problem**: LiteLLM kwargs are passed directly from MCP tool call parameters with no
filter. A caller can inject `api_key`, `base_url`, or `headers`, redirecting API calls
to an attacker-controlled endpoint.

**File**: `src/llm_router/router.py`

**Approach**:
1. Define `_ALLOWED_EXTRA_PARAMS` at module top:
   ```python
   _ALLOWED_EXTRA_PARAMS = frozenset({
       "temperature", "max_tokens", "stop", "top_p", "seed",
       "presence_penalty", "frequency_penalty", "logit_bias",
   })
   ```
2. In `route_and_call()` (or wherever `extra_params` is merged into kwargs), filter before use:
   ```python
   safe_params = {k: v for k, v in (extra_params or {}).items()
                  if k in _ALLOWED_EXTRA_PARAMS}
   ```
3. Log a warning if any key was stripped: `# keys in extra_params but not in allowlist`

**Test**: `tests/test_router.py` — add `test_extra_params_strips_api_key_injection`:
- Pass `extra_params={"api_key": "evil", "temperature": 0.5}`
- Assert the LiteLLM call receives `temperature=0.5` and no `api_key`

**Acceptance criteria**: No key outside `_ALLOWED_EXTRA_PARAMS` ever reaches LiteLLM.

---

### Task 1.2 — Thread-safe state.py with asyncio.Lock

**Problem**: `_active_profile`, `_last_usage`, `_active_agent` are module globals mutated
by `set_*()` functions with no locking. Concurrent MCP tool calls (which Claude Code
sends in rapid succession) race on these values.

**File**: `src/llm_router/state.py`

**Approach**:
1. Add a module-level lock:
   ```python
   import asyncio
   _lock = asyncio.Lock()
   ```
2. Wrap all setters:
   ```python
   async def set_active_profile(profile: str) -> None:
       async with _lock:
           global _active_profile
           _active_profile = profile
   ```
3. Getters do not need the lock (Python GIL protects simple reads), but document this.
4. `get_last_usage()` must return a copy, not the reference:
   ```python
   def get_last_usage():
       return dataclasses.replace(_last_usage) if _last_usage else None
   ```

**Test**: `tests/test_state.py` — add `test_concurrent_profile_set_no_race`:
- Use `asyncio.gather()` to call `set_active_profile()` 100 times concurrently with
  different values; assert final state is one of the valid values (not corrupted/None)

**Acceptance criteria**: No `AttributeError` or `None` return from getters under
concurrent load.

---

### Task 1.3 — Idempotent schema migrations in cost.py

**Problem**: `ALTER TABLE usage ADD COLUMN cost_usd REAL` raises `OperationalError:
duplicate column name` if the process crashes mid-migration and restarts. The DB becomes
unusable on the next startup.

**File**: `src/llm_router/cost.py`

**Approach**: Wrap every `ALTER TABLE ADD COLUMN` with a column-existence check:
```python
def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(f"SELECT name FROM pragma_table_info('{table}') WHERE name=?", (column,))
    return cursor.fetchone() is not None

# Replace every: cursor.execute("ALTER TABLE usage ADD COLUMN cost_usd REAL")
# With:
if not _column_exists(cursor, "usage", "cost_usd"):
    cursor.execute("ALTER TABLE usage ADD COLUMN cost_usd REAL DEFAULT 0.0")
```

Apply this pattern to every `ALTER TABLE` statement in the migration block.

**Test**: `tests/test_cost.py` — add `test_migration_idempotent`:
- Run `_initialize_db()` twice on the same DB file
- Assert second run completes without raising `OperationalError`

**Acceptance criteria**: `_initialize_db()` is safe to call multiple times on an
existing database.

---

### Task 1.4 — Token authentication for dashboard API

**Problem**: `GET /api/budget`, `POST /api/budget/set`, `GET /metrics` accept any
request from any process on the machine. Budget caps can be modified by any local
process; spend data readable by anything.

**Files**: `src/llm_router/dashboard/server.py`, `src/llm_router/budget_store.py`

**Approach**:
1. On first dashboard start, generate and persist a random token:
   ```python
   # In dashboard/server.py or budget_store.py
   _TOKEN_FILE = Path.home() / ".llm-router" / "dashboard.token"

   def _get_or_create_token() -> str:
       if _TOKEN_FILE.exists():
           return _TOKEN_FILE.read_text().strip()
       token = secrets.token_urlsafe(32)
       _TOKEN_FILE.write_text(token)
       _TOKEN_FILE.chmod(0o600)
       return token
   ```
2. Add an aiohttp middleware that checks `X-Dashboard-Token` header or `?token=` query
   param against the stored token:
   ```python
   @web.middleware
   async def auth_middleware(request, handler):
       if request.path in ("/", "/favicon.ico"):  # allow dashboard HTML
           return await handler(request)
       token = request.headers.get("X-Dashboard-Token") or request.rel_url.query.get("token")
       if token != _get_or_create_token():
           raise web.HTTPUnauthorized()
       return await handler(request)
   ```
3. Inject the token into all dashboard JS `fetch()` calls as a header.
4. Print the token URL on dashboard start: `Dashboard: http://localhost:7337?token=<token>`

**Test**: `tests/test_dashboard.py` — add `test_api_rejects_unauthenticated_request`:
- Make a `POST /api/budget/set` without the token header
- Assert HTTP 401

**Acceptance criteria**: All `/api/*` and `/metrics` routes return 401 without valid token.

---

### Task 1.5 — Refetch budget pressure per model in routing chain

**Problem**: `router.py:route_and_call()` calls `get_budget_state()` once at the start.
Chain walks can take 30–45 seconds (8 models × ~5s timeout). Budget can exhaust
mid-walk, but the router continues routing to expensive models with stale pressure.

**File**: `src/llm_router/router.py`

**Approach**:
1. Remove the single upfront pressure fetch (or keep it only for the initial
   chain-ordering decision).
2. Before each model call in the retry loop, re-fetch pressure for that provider:
   ```python
   for model_id in chain:
       provider = model_id.split("/")[0]
       pressure = await get_budget_state(provider)
       if pressure.pressure >= 1.0:
           # skip — budget exhausted since chain was built
           continue
       # proceed with call
   ```
3. The budget cache TTL (60s) means this adds at most one cache read per model, not a
   real DB query each time.

**Test**: `tests/test_router.py` — add `test_skips_model_when_budget_exhausts_mid_chain`:
- Mock a 3-model chain where model[0] fails, then budget for model[1]'s provider
  hits 1.0 pressure after the first failure
- Assert model[1] is skipped and model[2] is tried

**Acceptance criteria**: Router never calls a provider after its budget reaches 1.0,
even if pressure was 0.0 when the chain was assembled.

---

## Phase 2 — Reliability + Test Coverage (Weeks 2–3)

Four higher-effort fixes. Can be parallelized across developers.
Estimated total: 2–3 days.

---

### Task 2.1 — Atomic hook IPC with file locking

**Problem**: `auto-route.py` writes `~/.llm-router/last_route_{session_id}.json`.
`enforce-route.py` reads the same file on every tool call. No file lock. A partial
read during write causes `json.JSONDecodeError`, which is caught silently — the
exception path allows the blocked tool call through. The enforcement mechanism can be
defeated by write-read timing.

**Files**: `src/llm_router/hooks/auto-route.py`, `src/llm_router/hooks/enforce-route.py`

**Approach** — use temp-file-rename for writes:
```python
# In auto-route.py, replace direct write with:
import os, tempfile

def _write_atomic(path: str, data: dict) -> None:
    dir_ = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)   # atomic on POSIX
    except Exception:
        os.unlink(tmp)
        raise
```

For reads in `enforce-route.py`, add retry on `json.JSONDecodeError` (up to 3 times,
10ms sleep) before treating as missing:
```python
def _read_pending(path: str) -> dict | None:
    for _ in range(3):
        try:
            return json.loads(Path(path).read_text())
        except json.JSONDecodeError:
            time.sleep(0.01)
        except FileNotFoundError:
            return None
    return None   # still corrupt after retries — treat as missing
```

**Test**: Add `tests/test_hooks.py` with `test_atomic_write_not_readable_mid_write`:
- Simulate concurrent write + read of pending state
- Assert reads never return partial JSON

**Acceptance criteria**: `enforce-route.py` never silently allows a tool call due to
JSON parse failure.

---

### Task 2.2 — Test coverage for auto-route hook

**Problem**: The UserPromptSubmit hook is the most user-facing component — it fires on
every single prompt. It has zero test coverage. Classifier logic, routing chain selection,
continuation detection, and short code follow-up detection are all untested.

**Approach**:
1. Extract the pure classification logic from `auto-route.py` into a testable module
   `src/llm_router/hooks/_route_logic.py`:
   ```python
   # Pure functions, no subprocess dependencies
   def classify_prompt(prompt: str, session_id: str, ...) -> RouteDirective: ...
   def is_continuation(prompt: str) -> bool: ...
   def is_short_code_followup(prompt: str, last_route: dict) -> bool: ...
   def build_directive(task_type, complexity, method) -> str: ...
   ```
2. The hook script (`auto-route.py`) becomes a thin wrapper that reads stdin, calls
   `classify_prompt()`, and writes the directive.

3. Add `tests/test_hook_route_logic.py` with golden prompt fixtures:
   ```python
   GOLDEN = [
       ("What does os.path.join do?",       "query",    "simple"),
       ("Fix the auth bug in login.py",      "code",     "moderate"),
       ("Design the full auth system",       "code",     "complex"),
       ("Research latest AI funding rounds", "research", "moderate"),
       ("yes",                               None,       None),   # continuation
       ("ok go ahead",                       None,       None),   # continuation
       ("Write a hero section for our site", "generate", "simple"),
       # ... 20+ more
   ]

   @pytest.mark.parametrize("prompt,expected_task,expected_complexity", GOLDEN)
   def test_golden_prompt_routing(prompt, expected_task, expected_complexity):
       result = classify_prompt(prompt, session_id="test")
       assert result.task_type == expected_task
       assert result.complexity == expected_complexity
   ```

**Acceptance criteria**: 20+ golden prompts tested; hook logic refactored to be
import-safe and independently testable without subprocess.

---

### Task 2.3 — Re-enable integration tests

**Problem**: `tests/test_integration.py` is permanently excluded from CI with
`--ignore=tests/test_integration.py`. The root cause was an aiosqlite thread hang
after tests complete (the event loop doesn't drain cleanly).

**Approach**:
1. Identify the hang: likely an `aiosqlite` connection not awaited on close. Add
   explicit `await asyncio.sleep(0)` after every `aiosqlite.connect()` context manager
   exit, or use `asyncio.wait_for()` with a timeout.
2. If the underlying test makes real network calls, mock them with `respx` or
   `unittest.mock.patch`:
   ```python
   @pytest.mark.asyncio
   async def test_route_integration(respx_mock):
       respx_mock.post("https://api.openai.com/v1/chat/completions").mock(
           return_value=httpx.Response(200, json={...})
       )
       result = await route_and_call("What is 2+2?", task_type="query")
       assert result.model_used is not None
   ```
3. Remove the `--ignore` flag from the test command in `CLAUDE.md` and `pyproject.toml`.

**Acceptance criteria**: `uv run pytest tests/` (without `--ignore`) passes in CI.

---

### Task 2.4 — Chain deduplication in chain_builder.py + spend aggregation strategy

**Part A — Chain dedup** (1 hour):

**File**: `src/llm_router/chain_builder.py`

```python
# In build_chain(), after merging dynamic + static:
seen: set[str] = set()
deduped = []
for model_id in merged_chain:
    if model_id not in seen:
        seen.add(model_id)
        deduped.append(model_id)
return deduped
```

**Part B — Configurable spend aggregation** (2 hours):

**Problem**: `budget.py:_api_provider_state()` uses `max()` across spend sources.
In multi-channel scenarios (traffic through both LiteLLM proxy and direct calls),
`max()` under-reports real spend by up to 2x.

**File**: `src/llm_router/budget.py`, `src/llm_router/config.py`

Add config field:
```python
llm_router_spend_aggregation: Literal["max", "sum"] = "max"
# "max" = conservative dedup (same traffic, multiple trackers)
# "sum" = additive (independent traffic channels)
```

In `_api_provider_state()`:
```python
cfg = get_config()
if cfg.llm_router_spend_aggregation == "sum":
    spend = sum(float(r) for r in results if isinstance(r, (int, float)))
else:
    spend = max((float(r) if isinstance(r, (int, float)) else 0.0) for r in results)
```

Document the trade-off clearly in the config docstring.

**Test**: Add `test_spend_aggregation_sum_mode` and `test_spend_aggregation_max_mode`
to `tests/test_budget.py`.

**Acceptance criteria**: No duplicate models in any returned chain; spend aggregation
mode configurable and tested.

---

## Phase 3 — Observability + Engineering Quality (Month 2)

Four larger investments. These change the day-to-day development experience.
Estimated total: 1–2 weeks.

---

### Task 3.1 — Structured logging with structlog

**Problem**: The entire codebase uses `print()`. No log levels, no correlation IDs,
no aggregation, no filtering. Production debugging is a grep exercise on stdout.

**Files**: All of `src/llm_router/` — global change.

**Approach**:
1. Add `structlog` to `pyproject.toml` dependencies.
2. Create `src/llm_router/logging.py`:
   ```python
   import structlog

   def configure_logging(json_output: bool = False) -> None:
       structlog.configure(
           processors=[
               structlog.stdlib.add_log_level,
               structlog.stdlib.add_logger_name,
               structlog.processors.TimeStamper(fmt="iso"),
               structlog.processors.JSONRenderer() if json_output
               else structlog.dev.ConsoleRenderer(),
           ],
       )

   def get_logger(name: str):
       return structlog.get_logger(name)
   ```
3. Replace `print(f"Routing to {model}")` with:
   ```python
   log = get_logger(__name__)
   log.info("routing_decision", model=model, task_type=task_type,
            complexity=complexity, pressure=pressure, correlation_id=req_id)
   ```
4. Generate a `correlation_id = uuid4().hex[:8]` at the start of each `route_and_call()`
   and pass it through all sub-calls.
5. Log levels:
   - `DEBUG`: cache hits, pressure values, scorer intermediate results
   - `INFO`: routing decisions, model selected, cost recorded
   - `WARNING`: fallback triggered, budget pressure > 0.8, rate limit hit
   - `ERROR`: provider failure, DB write failure, hook file error

**Acceptance criteria**: Every routing decision produces a structured INFO log line with
`model`, `task_type`, `complexity`, `cost_usd`, and `correlation_id`. Zero bare `print()`
calls remain in `src/llm_router/`.

---

### Task 3.2 — OpenTelemetry spans for routing hot path

**Problem**: No way to determine why a specific routing call was slow or which step
added latency. A 5-second `llm_code` call could be slow due to: classifier LLM call,
discovery cache miss, scorer DB queries, or provider latency. Currently indistinguishable.

**Files**: `src/llm_router/router.py`, `src/llm_router/classifier.py`,
`src/llm_router/scorer.py`, `src/llm_router/chain_builder.py`

**Approach**:
1. Add `opentelemetry-sdk` and `opentelemetry-exporter-otlp` as optional dependencies:
   ```toml
   [project.optional-dependencies]
   tracing = ["opentelemetry-sdk>=1.20", "opentelemetry-exporter-otlp-proto-grpc>=1.20"]
   ```
2. Create `src/llm_router/tracing.py` with a no-op tracer when OTLP not configured:
   ```python
   from opentelemetry import trace
   from opentelemetry.sdk.trace import TracerProvider

   _tracer: trace.Tracer | None = None

   def get_tracer() -> trace.Tracer:
       global _tracer
       if _tracer is None:
           _tracer = trace.get_tracer("llm_router")
       return _tracer
   ```
3. Instrument key paths:
   ```python
   # router.py
   with get_tracer().start_as_current_span("route_and_call") as span:
       span.set_attribute("task_type", task_type)
       span.set_attribute("complexity", complexity)
       # ... routing logic ...
       span.set_attribute("model_selected", model_id)
       span.set_attribute("cost_usd", cost)
   ```
   Spans to add: `route_and_call`, `classify_complexity`, `build_chain`,
   `score_all_models`, `provider_call`.

4. When `OTEL_EXPORTER_OTLP_ENDPOINT` env var is set, configure the OTLP exporter.
   Otherwise, use the no-op tracer (zero overhead).

**Acceptance criteria**: When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, Jaeger/Tempo shows
full trace for a routing call with span breakdown by phase.

---

### Task 3.3 — Classifier prompt versioning + eval harness

**Problem**: The classifier LLM prompt is defined inline in `classifier.py`. Changes
are invisible in diffs, there's no way to measure whether a change improved accuracy,
and there's no regression test for the classifier's decision quality.

**Files**: `src/llm_router/classifier.py`, new `scripts/eval_classifier.py`,
new `src/llm_router/prompts/classifier_v1.txt`

**Approach**:
1. Extract the classifier system prompt to `src/llm_router/prompts/classifier_v1.txt`.
   Load at module import:
   ```python
   _PROMPT_PATH = Path(__file__).parent / "prompts" / "classifier_v1.txt"
   _SYSTEM_PROMPT = _PROMPT_PATH.read_text()
   ```
   The version number in the filename makes prompt changes visible in git history.

2. Create `scripts/eval_classifier.py` with 100 golden examples:
   ```python
   GOLDEN_SET = [
       # (prompt, expected_task_type, expected_complexity, note)
       ("What does os.path.join do?", "query", "simple", "basic lookup"),
       ("Fix the null pointer in auth.py line 42", "code", "moderate", "bug fix"),
       ("Design a distributed caching system for 100k rps", "code", "complex", "architecture"),
       # ... 97 more
   ]

   async def run_eval():
       correct = 0
       for prompt, exp_task, exp_complexity, note in GOLDEN_SET:
           result = await classify_complexity(prompt)
           ok = result.task_type == exp_task and result.complexity.value == exp_complexity
           if not ok:
               print(f"FAIL [{note}]: {prompt!r}")
               print(f"  Expected: {exp_task}/{exp_complexity}")
               print(f"  Got:      {result.task_type}/{result.complexity.value}")
           correct += ok
       print(f"\nAccuracy: {correct}/{len(GOLDEN_SET)} ({100*correct/len(GOLDEN_SET):.1f}%)")
   ```

3. Add to CI as a separate non-blocking job (report accuracy, don't fail build unless
   accuracy drops below 80%).

**Acceptance criteria**: `python scripts/eval_classifier.py` runs and reports accuracy
without manual setup. Prompt changes produce measurable accuracy delta.

---

### Task 3.4 — Automated release script

**Problem**: The current release process is a 10-step manual checklist in `CLAUDE.md`.
Steps are regularly missed (e.g., `marketplace.json` version not updated, plugin not
reinstalled). Every release requires remembering 10 distinct commands.

**File**: New `Makefile` at repo root (or `scripts/release.sh`)

**Approach** — create `scripts/release.py`:
```python
#!/usr/bin/env python3
"""Single-command release: python scripts/release.py 5.2.0"""

import sys, subprocess, tomllib, json

def run(cmd, **kwargs):
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, check=True, **kwargs)
    return result

def bump_versions(version: str):
    # pyproject.toml
    content = Path("pyproject.toml").read_text()
    # ... string replacement for version field ...

    # plugin.json
    data = json.loads(Path(".claude-plugin/plugin.json").read_text())
    data["version"] = version
    Path(".claude-plugin/plugin.json").write_text(json.dumps(data, indent=2))

    # marketplace.json — both version fields
    ...

def verify_versions(version: str):
    v1 = tomllib.load(open("pyproject.toml","rb"))["project"]["version"]
    v2 = json.load(open(".claude-plugin/plugin.json"))["version"]
    v3 = json.load(open(".claude-plugin/marketplace.json"))["version"]
    assert v1 == v2 == v3 == version, f"VERSION MISMATCH: {v1} {v2} {v3}"
    print(f"  ✅ All versions: {version}")

def main():
    version = sys.argv[1]
    print(f"\n=== Releasing v{version} ===\n")

    print("1. Running tests...")
    run("uv run pytest tests/ -q --ignore=tests/test_integration.py")

    print("2. Bumping versions...")
    bump_versions(version)
    verify_versions(version)

    print("3. Building...")
    run("rm -rf dist/ && uv build")

    print("4. Committing...")
    run(f'git add -p')  # still manual review of diff
    run(f'git commit -m "chore(v{version}): release"')
    run("git push")

    print("5. Publishing to PyPI...")
    pypi_token = get_pypi_token()
    run(f'uv publish --token "{pypi_token}"')

    print("6. Tagging...")
    run(f"git tag v{version} && git push origin --tags")

    print("7. GitHub release...")
    changelog_entry = extract_changelog_entry(version)
    run(f'gh release create v{version} --title "v{version}" --notes "{changelog_entry}"')

    print("8. Reinstalling plugin...")
    run("claude plugin uninstall llm-router && claude plugin install llm-router")

    print(f"\n✅ Released v{version}")

if __name__ == "__main__":
    main()
```

**Acceptance criteria**: `python scripts/release.py 5.2.0` completes a full release
with zero manual steps after initial `git add -p` review.

---

## Version Target

All three phases ship together as **v5.2.0**. Phases define execution order within the
release, not separate release boundaries.

| Phase | Tasks | ETA |
|-------|-------|-----|
| Phase 1 — Security + Data Integrity | 1.1–1.5 | Week 1 |
| Phase 2 — Reliability + Tests | 2.1–2.4 | Week 2–3 |
| Phase 3 — Observability + Quality | 3.1–3.4 | Month 2 |
| **Release v5.2.0** | all 14 tasks | End of Month 2 |

## Testing Requirement per Phase

Every task must ship with a test that would have caught the original bug. No exceptions.
Tasks without a test are not complete.

## Commit Convention

Each task gets its own commit. Version bump happens once at the end after all 14 tasks pass.

```
fix(security): whitelist extra_params in router.py (#1.1)
fix(reliability): idempotent schema migrations in cost.py (#1.3)
feat(observability): structured logging with structlog (#3.1)
feat(v5.2.0): audit remediation — security, reliability, observability
```
