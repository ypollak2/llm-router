# LLM Router Architecture

> How llm-router routes requests to the best available LLM while managing costs, budgets, and provider health.

## System Overview

LLM Router is a **request routing layer** that sits between your applications and external LLM providers (OpenAI, Gemini, Perplexity, etc.). It intelligently selects the best model for each task based on:

- **Complexity classification** (simple → cheap, complex → capable)
- **Provider availability** (API keys, circuit breaker status)
- **Budget constraints** (per-call, daily, monthly limits)
- **Cost efficiency** (prefer cheaper models when possible)
- **Fallback chains** (emergency BUDGET fallback when primary fails)

## Core Architecture Layers

```
┌─────────────────────────────────────────────────────────────┐
│  MCP Tools Layer (tools/*.py)                               │
│  ├─ llm_query, llm_code, llm_analyze, llm_generate          │
│  ├─ llm_route, llm_classify, llm_usage, llm_setup          │
│  └─ llm_check_usage, llm_health, llm_providers             │
└────────────────┬────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────┐
│  Router Core (router.py)                                    │
│  ├─ route_and_call(): Main entry point                     │
│  ├─ _resolve_profile(): Determine routing table             │
│  ├─ _build_and_filter_chain(): Model selection             │
│  ├─ _dispatch_model_loop(): Execute with fallback          │
│  └─ _call_text()/_call_media(): Provider dispatch          │
└────────────────┬────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────┐
│  Support Systems                                            │
│  ├─ Profiles (profiles.py): BUDGET/BALANCED/PREMIUM chains │
│  ├─ Budget Tracking (budget.py): Monthly/daily spend       │
│  ├─ Health Tracking (health.py): Circuit breaker           │
│  ├─ Cost Logging (cost.py): Usage→SQLite persistence       │
│  ├─ Classification (classifier.py): Complexity scoring     │
│  └─ Provider Drivers (providers.py): API-specific code     │
└────────────────┬────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────┐
│  External LLM Providers                                     │
│  ├─ OpenAI (GPT-4o, GPT-4o-mini, o3)                       │
│  ├─ Google (Gemini Pro, Gemini Flash)                      │
│  ├─ Perplexity (Sonar, web-grounded)                       │
│  ├─ DeepSeek (chat, reasoner)                              │
│  ├─ Mistral                                                 │
│  ├─ Groq (free, fast)                                       │
│  ├─ Local (Ollama, Codex CLI)                              │
│  └─ LiteLLM (unified SDK)                                   │
└─────────────────────────────────────────────────────────────┘
```

## Request Flow Diagram

```
User Request
    ↓
[Budget Check]  ← Monthly/daily limits, _pending_spend tracking
    ↓
[Complexity Classification]  ← Heuristic, Ollama, or API-based
    ↓
[Profile Resolution]  ← Simple→BUDGET, Moderate→BALANCED, Complex→PREMIUM
    ↓
[Model Chain Selection]  ← Profiles.py routing tables
    ↓
[Health & Budget Filtering]  ← Circuit breaker, provider quotas
    ↓
[Semantic Cache Check]  ← Skip call if recently answered (Ollama required)
    ↓
[Dispatch Loop]  ← Try models in order
    │
    ├→ [Provider API Call]  ← _call_text or _call_media
    │   ├─ Success → Log cost, return response
    │   └─ Failure → Record in health tracker, try next
    │
    └→ [Emergency BUDGET Fallback]  ← If primary chain exhausts
        ├─ Success → Return cheap model response
        └─ Failure → RuntimeError("All models failed")
```

## Key Design Principles

### 1. Complexity → Profile Mapping (Foundational Rule)

Every request maps to a profile based on complexity:

```python
COMPLEXITY_TO_PROFILE = {
    Complexity.SIMPLE: RoutingProfile.BUDGET,           # Haiku/Flash
    Complexity.MODERATE: RoutingProfile.BALANCED,       # Sonnet/GPT-4o
    Complexity.COMPLEX: RoutingProfile.PREMIUM,         # Opus/o3
    Complexity.DEEP_REASONING: RoutingProfile.PREMIUM,  # Extended thinking
}
```

This is **never overridden** except by explicit `profile=` parameter (power user escape hatch).

### 2. Provider Chains with Free-First Ordering

Each profile has a fallback chain that tries models in order:

```
BUDGET:     Ollama → Codex/gpt-5.4 → Gemini Flash → Groq → GPT-4o-mini
BALANCED:   Ollama → Codex/gpt-5.4 → GPT-4o → Gemini Pro → DeepSeek
PREMIUM:    Ollama → Codex/o3 → o3 → Gemini Pro
```

**Free models (Ollama, Codex) are ALWAYS injected before paid externals.** This minimizes API costs.

### 3. Pressure Cascade for Budget-Aware Reordering

When provider budgets are tight, models are reordered dynamically:

```
Pressure 0.0-0.5:  Use configured chain as-is
Pressure 0.5-0.8:  Warn in logs, keep trying
Pressure 0.8-1.0:  Skip provider, fall back to next
Pressure ≥ 1.0:    Provider fully exhausted, cannot use
```

Example: If Sonnet hits 90% weekly quota, the BALANCED chain reorders to try cheaper models first.

### 4. Atomic Budget Enforcement with _pending_spend

Global state tracks in-flight estimated costs:

```python
async with _budget_lock:
    # Atomic check: actual spent + pending reserved >= limit?
    if (monthly_spend + _pending_spend) >= budget:
        raise BudgetExceededError(...)
    
    # Reserve estimated cost for this call
    _pending_spend += _estimate_cost(top_model, prompt_len, 500)
```

This prevents TOCTOU race conditions: concurrent callers see the full picture.

### 5. Emergency Fallback Chain

When the primary profile exhausts all models, try BUDGET as last resort:

```
Primary chain fails (e.g., PREMIUM)
    ↓
"Try BUDGET emergency fallback"
    ↓
BUDGET chain succeeds
    ↓
Return cheap model response (better than nothing)
```

This ensures the system never returns "no models available" if any model can be reached.

## Critical Files Reference

| File | Responsibility |
|------|---|
| `router.py` | Main routing logic: budget checks, model selection, dispatch loop, fallback chains |
| `profiles.py` | Provider chains per profile/task_type; model eligibility rules |
| `budget.py` | Monthly/daily spend tracking, provider pressure calculation |
| `health.py` | Circuit breaker per provider (failure tracking, open/closed state) |
| `classifier.py` | Complexity scoring via Ollama/API fallback |
| `cost.py` | Cost logging to SQLite, session spend calculation |
| `config.py` | Config from env vars (API keys, limits, mode) |
| `codex_agent.py` | Local Codex CLI integration (binary detection, execution) |
| `tools/*.py` | MCP tool implementations (llm_route, llm_query, etc.) |
| `hooks/` | Claude Code hook scripts (auto-route classifier, usage refresh) |

## v5.3.0 Critical Fixes

### TOCTOU Budget Enforcement

**Problem**: Concurrent calls could exceed budget because budget checks weren't atomic.

**Solution**: `_pending_spend` global tracks in-flight costs. All budget checks happen inside `_budget_lock`, and estimated cost is reserved atomically before dispatch.

```python
# Before: Concurrent calls both see $0.95 spent, both think they fit in $1.00 budget
# After: First call reserves $0.03, second call sees $0.95 + $0.03 = $0.98, knows it won't fit
```

### Emergency BUDGET Fallback

**Problem**: When all models in a profile fail (e.g., all external APIs down), user got "no models available" even though cheap local models existed.

**Solution**: After primary chain exhausts, automatically try BUDGET profile as emergency fallback.

```python
if profile != BUDGET and primary_chain_fails:
    emergency_chain = await _build_and_filter_chain(BUDGET)
    # Try cheap models (Ollama, Codex, etc.)
```

### Async/Event Loop Safety

**Problem**: Synchronous filesystem I/O (`.exists()`, `.stat()`, `.read_text()`) blocked the entire asyncio event loop, causing tool hangs.

**Solution**: Two-tier fix:
1. **Permanent**: Pre-compute expensive checks at module import time (Codex binary caching)
2. **Practical**: Use `asyncio.to_thread()` for unavoidable runtime I/O

```python
# Before: blocking I/O on every request
if Path(db_path).exists():  # BLOCKS EVENT LOOP ❌

# After: non-blocking
exists = await asyncio.to_thread(Path(db_path).exists)  # Free event loop ✅
```

### Correlation ID Tracking

**Problem**: Requests couldn't be traced through logs when debugging issues.

**Solution**: Generate unique 8-char correlation ID per request, pass through entire routing chain, log on every decision point.

## Testing Strategy

- **Unit Tests** (`tests/test_router.py`): Budget enforcement, model selection, fallback
- **Audit Tests** (`tests/test_audit_v5_fixes.py`): v5.0-v5.3 fix coverage
- **Integration Tests** (`tests/test_budget.py`): SQLite persistence, provider tracking
- **Dashboard Tests** (`tests/test_dashboard_auth.py`): Web UI authentication

## Configuration

All behavior is controlled by environment variables:

```bash
# Budget enforcement
LLM_ROUTER_MONTHLY_BUDGET=100.00          # Stop if spent > $100
LLM_ROUTER_DAILY_SPEND_LIMIT=10.00        # Stop if spent > $10 today UTC

# Mode
LLM_ROUTER_MODE=subscription               # Claude subscription only
LLM_ROUTER_MODE=api                        # Mixed API keys + subscription
LLM_ROUTER_ENFORCE=off|soft|smart|hard     # Hook enforcement level

# Features
OLLAMA_BASE_URL=http://localhost:11434    # Enable local Ollama
LLM_ROUTER_ENABLE_SEMANTIC_CACHE=true     # Enable cache hit optimization

# API Keys (required for external providers)
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
PERPLEXITY_API_KEY=...
```

## Performance Characteristics

| Operation | Latency |
|-----------|---------|
| Budget check (cached) | <1ms |
| Complexity classification (heuristic) | <1ms |
| Complexity classification (Ollama) | 1-3s |
| Provider API call (typical) | 0.5-3s |
| Emergency fallback (if needed) | +0.5-3s |

Cache hits (semantic similarity match) bypass the entire chain and return instantly (<1ms).

## Future Improvements

1. **Persistent routing decisions table** — Track which models succeed for which tasks
2. **Latency-aware routing** — Prefer fast models for time-sensitive tasks
3. **User-specific profiles** — Learn preferences per user/team
4. **Real-time cost optimization** — Adjust chains based on live provider pricing
5. **Multi-region failover** — Geographic distribution for lower latency
