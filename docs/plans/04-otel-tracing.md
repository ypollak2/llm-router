# Plan: OpenTelemetry Tracing

> **Vision item 3** from docs/VISION.md
> **Status**: Not started. Table stakes for production use.

---

## Goal

When a request tries model A (fails), model B (fails), succeeds on model C — the logs are
three separate SQLite rows. No way to correlate them. Debugging a failed routing chain is
archaeology.

**Target**: A single `trace_id` ties together the full classify → attempt → fallback chain.
Exportable to Jaeger, Datadog, or any OTLP endpoint.

---

## Target Trace Structure

```
trace_id: abc123  prompt_hash: sha256[:8]
  span[0]: router.classify          2ms    → code/complex  91% confidence
  span[1]: model.anthropic/claude-sonnet  attempt 1  FAILED  timeout 5001ms
  span[2]: model.openai/gpt-4o           attempt 2  FAILED  rate_limit 120ms
  span[3]: model.gemini/gemini-2.5-flash attempt 3  SUCCESS 1840ms  847 tokens
  ─────────────────────────────────────────────────────────────────────
  total: 6963ms  |  2 failures  |  1 success  |  cost: $0.004
```

---

## Implementation Plan

### Step 1 — trace_id generation (half day)

In `router.py`, generate a `trace_id = uuid4().hex[:16]` at the top of each `route()`
call. Pass it through to every model attempt.

Add `trace_id TEXT` column to `routing_decisions`. Each row (attempt) gets the same
`trace_id` — they're linked by it.

### Step 2 — span logging (1 day)

Add `RoutingSpan` dataclass:
```python
@dataclass(frozen=True)
class RoutingSpan:
    trace_id: str
    span_index: int
    operation: str        # "classify" | "model_attempt"
    model: str | None
    duration_ms: int
    status: str           # "success" | "failed" | "skipped"
    failure_reason: str | None
    tokens: int | None
    cost_usd: float | None
```

Log spans to `routing_spans` table. `routing_decisions` keeps summary row (final result).

### Step 3 — OTLP export (2 days)

Add optional `OTEL_EXPORTER_OTLP_ENDPOINT` env var. If set, emit spans using
`opentelemetry-sdk` (optional dep, install with `pip install llm-router[otel]`).

Map `RoutingSpan` → OTLP span attributes. Standard OTLP format means zero custom
Jaeger/Datadog config — any OTLP-compatible backend works out of the box.

### Step 4 — `llm_route` trace output (half day)

When `trace=True` passed to `llm_route`, return the full trace as part of the response:
```
✓ Routed to gemini/gemini-2.5-flash after 2 failures (6963ms total)
  Trace: abc123 | classify: 2ms | 3 attempts | $0.004
```

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `src/llm_router/tracing.py` | New — `RoutingSpan`, span logging, OTLP export |
| `src/llm_router/router.py` | Generate trace_id, log spans per attempt |
| `src/llm_router/cost.py` | Add `routing_spans` table |
| `src/llm_router/server.py` | Expose `trace=True` param on `llm_route` |
| `pyproject.toml` | Add `[otel]` optional dep group |

---

## Success Metric

A 3-model fallback chain produces a single trace viewable in Jaeger with correct
span hierarchy and timing in under 5 minutes of setup.
