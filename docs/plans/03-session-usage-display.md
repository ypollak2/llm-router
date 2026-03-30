# Plan: Session Usage Display + Routing Distribution

> Combined vision items: usage visibility, confidence display, latency awareness.
> **Status**: Not started. High UX value, medium complexity.

---

## Goal

Right now llm-router is invisible. Users can't see:
- What % of their prompts were routed vs answered directly by Claude
- Which models handled which tasks
- How much was saved vs all-Opus
- How confident the classifier was

**Target**: After each session, user sees a clear summary. `llm_classify` shows confidence.
`llm_check_usage` shows routing distribution alongside Claude subscription %.

---

## 3a — Routing Distribution in `llm_check_usage`

### Current output
```
Claude subscription: 78% of session limit used
```

### Target output
```
Claude subscription: 78% of session limit
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Session routing (last 24h):
  llm_query    ████████████░░░░  18 calls  Haiku     $0.03
  llm_code     ██████░░░░░░░░░░  10 calls  Sonnet    $0.18
  llm_research ███░░░░░░░░░░░░░   5 calls  Perplx    $0.12
  llm_generate ██░░░░░░░░░░░░░░   4 calls  Flash     $0.04
  direct Opus  ████░░░░░░░░░░░░   8 calls  Opus      $2.40 ← avoidable
  ────────────────────────────────────────────────────────
  Net saved $2.37  |  Routing efficiency: 83%
```

### Implementation

In `cost.py`, add a `session_distribution()` function that queries `routing_decisions`
for the last 24h and computes per-tool counts + estimated costs.

Add to `llm_check_usage` response after the subscription data.

---

## 3b — Confidence Display in `llm_classify`

### Current behavior
`ClassificationResult.confidence` is computed by the multi-layer classifier but the
confidence value is not surfaced in the output string. Users never see it.

### Target behavior
```
[ROUTE: code/moderate via ollama]  ████████░░  81% confident
Recommended: llm_code → Sonnet
```

Thresholds:
- `>= 85%` → route silently (no note)
- `60–84%` → `⚠ Moderate confidence — if this is research, use llm_research instead`
- `< 60%`  → `⚠ Low confidence — multiple task types detected. Review routing.`

### Implementation

In `classifier.py`, `ClassificationResult` already has `confidence: float`. In
`server.py`'s `llm_classify` tool, read the confidence and append the appropriate note
to the output string.

---

## 3c — Latency-Aware Model Selection

### Current behavior
`adjusted_score = base_benchmark_score * (1 - failure_penalty)`

### Target behavior
```python
def latency_factor(model: str) -> float:
    p95 = get_p95_latency(model)  # from routing_decisions
    MAX_ACCEPTABLE = 8000  # ms
    if p95 is None:
        return 1.0  # no data, no penalty
    return 1.0 - (min(p95, MAX_ACCEPTABLE) / MAX_ACCEPTABLE) * 0.1

adjusted_score = base_score * (1 - failure_penalty) * latency_factor(model)
```

10% max weight — latency is a tiebreaker between models with similar benchmark scores,
not a primary signal. Users notice slow responses more than marginal quality differences.

### Implementation

In `model_selector.py`, add `_latency_factor()`. Pull p95 from `routing_decisions` via
a simple percentile query on `latency_ms` per model.

---

## Files to Modify

| File | Change |
|------|--------|
| `src/llm_router/cost.py` | Add `session_distribution()` for last 24h stats |
| `src/llm_router/server.py` | Append distribution to `llm_check_usage` output |
| `src/llm_router/classifier.py` | Surface confidence in output |
| `src/llm_router/model_selector.py` | Add `_latency_factor()` tiebreaker |

---

## Success Metric

Users can answer "what % of my prompts were routed today?" in under 5 seconds via
`llm check_usage`.
