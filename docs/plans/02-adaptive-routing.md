# Plan: Adaptive Routing — Learn From Usage

> **Vision item 1** from docs/VISION.md
> **Status**: Not started. Depends on #1 (routing enforcement) — need actual routing
> decisions in the DB before personalization can work.

---

## Goal

The router currently knows which models are globally best (from Arena Hard, Aider, HF
benchmarks). It doesn't know which models are best *for you* — your codebase, your prompts,
your quality bar.

**Target**: After 2 weeks of usage, routing decisions are better than static benchmarks
because they reflect your actual acceptance rate per model per task type.

---

## Data Already Available

The `routing_decisions` SQLite table already logs:
- `model` — which model was selected
- `task_type` — query / code / research / generate / analyze
- `complexity` — simple / moderate / complex
- `latency_ms` — response time
- `tokens_used` — output size (proxy for quality)
- `timestamp`

**Missing**: outcome signal — did the user accept the response or re-prompt?

---

## Implementation Plan

### Step 1 — Capture outcome signal (2 days)

Add `response_accepted` boolean to `routing_decisions`. Proxy signals:
- **Positive**: User sends next unrelated prompt (accepted and moved on)
- **Negative**: User immediately re-prompts the same topic, or prefixes with "no", "wrong",
  "that's not right", "try again"

Detect via UserPromptSubmit hook: compare current prompt to previous. If semantic overlap
> 0.7 and starts with negation → mark previous decision as `response_accepted=False`.

```python
REJECTION_SIGNALS = ["no,", "wrong", "that's not", "not right", "try again",
                     "that didn't", "doesn't work", "incorrect"]

def classify_outcome(prev_prompt: str, curr_prompt: str) -> bool | None:
    if any(curr_prompt.lower().startswith(s) for s in REJECTION_SIGNALS):
        return False
    # If highly similar topic but no rejection signal, assume accepted
    return None  # ambiguous — don't log
```

### Step 2 — Per-model acceptance rate tracking (1 day)

Add `model_outcomes` table:
```sql
CREATE TABLE model_outcomes (
    model TEXT,
    task_type TEXT,
    complexity TEXT,
    accepted INTEGER,  -- 1 = yes, 0 = no
    timestamp TEXT
);
```

Weekly aggregation job (cron or triggered by `llm_quality_report`):
```sql
SELECT model, task_type, COUNT(*) as total,
       SUM(accepted) * 1.0 / COUNT(*) as acceptance_rate
FROM model_outcomes
GROUP BY model, task_type
```

### Step 3 — Bayesian score update (2 days)

Current routing uses `base_benchmark_score`. Add personalization factor:

```python
def personalized_score(model: str, task_type: TaskType) -> float:
    base = get_benchmark_score(model)           # global prior
    acceptance = get_acceptance_rate(model, task_type)  # your data
    n = get_sample_count(model, task_type)

    if n < 10:
        return base  # not enough data, trust global benchmark

    # Bayesian blend: weight personal data more as n grows
    weight = min(n / 50, 0.6)  # cap at 60% personal influence
    return base * (1 - weight) + acceptance * weight
```

### Step 4 — Routing table reweight (1 day)

`model_selector.py` already calls `get_benchmark_data()`. Replace with
`get_personalized_scores()` that wraps the benchmark data with the Bayesian adjustment.

Zero behavior change when no personal data exists. Progressive improvement as data accumulates.

---

## Files to Modify

| File | Change |
|------|--------|
| `src/llm_router/cost.py` | Add `model_outcomes` table, acceptance logging |
| `src/llm_router/hooks/auto-route.py` | Detect rejection signals from previous turn |
| `src/llm_router/model_selector.py` | Use `get_personalized_scores()` |
| `src/llm_router/quality.py` | Add personalization stats to quality report |

---

## Success Metric

After 4 weeks: acceptance rate for routed responses ≥ 80% (vs ~65% with static benchmarks
for complex queries that get downshifted to cheaper models).
