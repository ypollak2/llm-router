# llm-router — Vision & Roadmap

> Where we're going and why.

---

## The Problem We're Solving

Every developer using Claude Code is burning money and attention on a solved problem: deciding which AI model to use for which task. You end up doing one of two things — either default to the best model for everything (expensive) or manually pick models for every task (exhausting). Neither scales.

The router's job is to make that decision invisible, instantaneous, and smarter than you'd make manually.

---

## Current State (v0.5.x)

The foundation is solid:

- **24 MCP tools** covering text, image, video, audio, streaming, pipelines
- **Multi-layer classifier**: heuristic → Ollama → cheap API → fallback
- **Benchmark-driven routing**: weekly-updated model rankings from Arena Hard, Aider, HuggingFace
- **Full-coverage hook**: every Claude Code prompt is routed, not just explicit ones
- **Ollama integration**: free local routing for budget tasks
- **Cost tracking**: SQLite ledger with savings quantification
- **Hook self-update**: distributed to all users automatically on pip upgrade

What exists is a complete, working system. What's missing is the intelligence layer on top of it.

---

## Next Phase — The Intelligence Layer

### 1. Adaptive Routing (Learn From Your Usage)

**Gap today**: The router knows which models are globally best (from benchmarks). It doesn't know which models are best *for you*.

**What to build**: A lightweight feedback loop.

```
Every routed call → log outcome signal (latency, response length, user accepted/rejected)
Every week → reweight model scores per task type based on your data
Your routing table becomes personalized over time
```

The benchmark data is the prior. Your usage is the posterior. Bayesian update on a per-installation basis.

This is what Martian charges $0.004/request for. We can do it locally, for free, with the data already being collected in `routing_decisions`.

---

### 2. Latency-Aware Model Selection

**Gap today**: `routing_decisions` logs `latency_ms` for every call. We never use it.

**What to build**: Factor p95 latency per model into ordering decisions — not as a primary signal, but as a tiebreaker between models with similar benchmark scores.

```python
# Current: score only
adjusted_score = base_benchmark_score * (1 - failure_penalty)

# With latency:
adjusted_score = base_benchmark_score * (1 - failure_penalty) * latency_factor
```

Where `latency_factor = 1.0 - (model_p95_latency / MAX_ACCEPTABLE_LATENCY) * 0.1`

Small weight, big UX impact. Users notice when responses feel slow.

---

### 3. OpenTelemetry Tracing

**Gap today**: When a request tries model A, fails, tries model B, fails, succeeds on model C — the logs are three separate entries. No way to correlate them into a single trace.

**What to build**: A `trace_id` that ties together the full fallback chain, exportable to Jaeger, Datadog, or any OTLP endpoint.

```
trace_id: abc123
  span: router.classify (2ms)
  span: model.anthropic/claude-sonnet → FAILED (timeout, 5001ms)
  span: model.openai/gpt-4o → FAILED (rate limit, 120ms)
  span: model.gemini/gemini-2.5-flash → SUCCESS (1840ms)
total: 6961ms, 2 failures, 1 success
```

This is table stakes for anyone running llm-router in production. Without it, debugging is archaeology.

---

### 4. Confidence Display on Classifications

**Gap today**: `ClassificationResult.confidence` is computed but thrown away after the routing decision. Users never see it.

**What to build**: Surface low-confidence classifications to the user.

```
[ROUTE: code/moderate via ollama, 61% confidence]
⚠ Uncertain classification — if this is actually a research task, use llm_research directly
```

High confidence (>85%): route silently.
Medium confidence (60–85%): route + note.
Low confidence (<60%): route + suggest override.

This makes the router feel honest and controllable rather than a black box.

---

### 5. Provider-Specific Optimization Hooks

**Gap today**: We pass a uniform request to every model. But each model has unique parameters that improve quality.

**What to build**: Per-provider parameter injection.

```python
PROVIDER_HINTS = {
    "openai/o3": {"reasoning_effort": "medium"},        # $$ savings on o3
    "perplexity/sonar-pro": {"search_recency_filter": "week"},  # fresher research
    "gemini/gemini-2.5-pro": {"thinking_budget": 8000}, # control thinking tokens
    "anthropic/claude-opus-4-6": {},                    # no special params needed
}
```

Users can override per-call. Defaults are set by the routing profile.

---

### 6. Streaming for All Tools (Not Just `llm_stream`)

**Gap today**: `llm_query`, `llm_code`, `llm_analyze` all block until the full response arrives. Only `llm_stream` streams.

**What to build**: Opt-in streaming for every tool via a `stream=True` parameter, backed by the same infrastructure as `llm_stream`. The MCP client decides whether to wait or stream.

---

### 7. Post-Response Validation

**Gap today**: If a model returns a 200 but with empty content, a single word, or garbled JSON, the router reports success.

**What to build**: A lightweight `validate_response()` step.

```python
def validate_response(content: str, task_type: TaskType) -> bool:
    if len(content.strip()) < 10:
        return False  # probably empty/garbage
    if task_type == TaskType.CODE and "```" not in content and len(content) < 50:
        return False  # code response with no code block and very short
    return True
```

Failed validation → retry on next model in chain. This eliminates a whole class of silent failures.

---

## Medium-Term Vision: Team Intelligence

The next step beyond individual routing is **collective intelligence across a team**.

When 10 engineers all use llm-router, their routing decisions and outcomes are siloed in 10 separate SQLite databases. The signal is there — it's just not shared.

A team server mode would:
- Aggregate routing outcomes across all team members
- Identify which models work best for *your codebase* (not just globally)
- Enable team-wide routing policies ("don't use GPT-4o for internal code — data residency")
- Show a shared cost/savings dashboard (who's using what, where the budget is going)

This is the architecture step from single-user tool to team infrastructure.

---

## Long-Term Vision: The Intelligent Routing Fabric

The endgame is a router that knows your entire context:

- **Your codebase**: what language, framework, complexity
- **Your team's preferences**: which models produce output your engineers accept
- **Your cost constraints**: budget thresholds, per-project allocation
- **Your compliance requirements**: which models can see which data
- **The current model landscape**: real-time benchmark updates

And makes routing decisions that are better than any human could make manually — not because it's smarter, but because it has more data and zero cognitive overhead.

The vision: **routing as infrastructure**. Like a load balancer for AI. You don't think about which server handles your HTTP request; you shouldn't think about which model handles your AI task.

---

## What We're NOT Building

To stay focused:

- **Not a cloud proxy** — we stay local/self-hosted. Data residency is a feature, not a limitation.
- **Not a model marketplace** — OpenRouter already exists. We route, we don't sell access.
- **Not a prompt management system** — that's PromptLayer, LangSmith. Different product.
- **Not a fine-tuning platform** — out of scope. We optimize routing, not training.

---

## Roadmap Summary

| Phase | Focus | Key Deliverable |
|-------|-------|-----------------|
| **Now** | Intelligence layer | Adaptive routing, latency signals, confidence display |
| **Next** | Team mode | Shared server, centralized dashboard, team policies |
| **Later** | Enterprise | SSO/RBAC, audit logs, VPC, compliance |
| **Vision** | Routing fabric | Full context-aware, self-improving routing infrastructure |
