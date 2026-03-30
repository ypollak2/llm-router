# Plan: Post-Response Validation + Provider Hints

> **Vision items 5 & 7** from docs/VISION.md
> **Status**: Not started. Eliminates silent failures.

---

## 5a — Post-Response Validation

### Goal

If a model returns HTTP 200 but with empty content, a single word, or garbled output,
the router reports success. Users see garbage. The fix: validate before returning.

### Validation Rules

```python
def validate_response(content: str, task_type: TaskType) -> tuple[bool, str]:
    stripped = content.strip()

    if len(stripped) < 10:
        return False, "response too short (likely empty or error)"

    if task_type == TaskType.CODE:
        if "```" not in stripped and len(stripped) < 50:
            return False, "code task returned no code block"

    if task_type == TaskType.RESEARCH:
        # Perplexity sometimes returns "I cannot browse" type responses
        REFUSALS = ["i cannot", "i'm unable", "i don't have access"]
        if any(r in stripped.lower() for r in REFUSALS) and len(stripped) < 200:
            return False, "research model refused to answer"

    return True, "ok"
```

Failed validation → retry on next model in fallback chain (not the same model).
Log `validation_failed=True` in `routing_decisions`.

### Implementation

In `router.py`, after receiving a response, call `validate_response()` before returning.
If invalid: pop current model from chain, retry with next.

Max 1 validation retry per call (prevent infinite loop on broken chains).

---

## 5b — Provider-Specific Optimization Hints

### Goal

Each model has unique parameters that can improve quality or reduce cost.
Currently we pass a uniform request to every model.

### Parameter Map

```python
PROVIDER_HINTS: dict[str, dict] = {
    "openai/o3":                  {"reasoning_effort": "medium"},
    "openai/o3-mini":             {"reasoning_effort": "low"},
    "perplexity/sonar-pro":       {"search_recency_filter": "week"},
    "perplexity/sonar":           {"search_recency_filter": "month"},
    "gemini/gemini-2.5-pro":      {"thinking_budget": 8000},
    "gemini/gemini-2.5-flash":    {"thinking_budget": 2000},
    "anthropic/claude-opus-4-6":  {},  # no special params needed
    "anthropic/claude-sonnet-4-6": {},
}
```

### User Override

Per-call override via routing profile config:
```python
# In routing profile or env var
PROVIDER_HINTS_OVERRIDE = {
    "perplexity/sonar-pro": {"search_recency_filter": "day"}  # fresher for my use case
}
```

### Implementation

In `router.py`, before calling provider, merge `PROVIDER_HINTS[model]` into request params.
User overrides from `config.py` win over defaults.

---

## Files to Modify

| File | Change |
|------|--------|
| `src/llm_router/router.py` | Add `validate_response()` call + retry logic |
| `src/llm_router/router.py` | Inject `PROVIDER_HINTS` per model call |
| `src/llm_router/config.py` | Add `PROVIDER_HINTS_OVERRIDE` env var |
| `src/llm_router/cost.py` | Add `validation_failed` column to `routing_decisions` |
| `tests/test_router.py` | Tests for validation retry behavior |

---

## Success Metric

Empty/garbage responses drop to 0% from models known to occasionally return them
(o3-mini, Perplexity Sonar). Verified by `validation_failed` rate in quality report.
