# Claude Subscription Quota Tracking Issues in LLM Router

**Date**: 2026-04-23  
**User Concern**: "I want to understand if there are problems in llm-router, as I don't understand the usage with the subscription of Claude with it"

---

## Executive Summary

The routing system has **systemic data quality issues** preventing real analysis of how Claude subscription quota is consumed. The problems fall into three categories:

1. **Broken routing database** — contains test data instead of real routing decisions
2. **Broken Claude tracking** — stores test entries instead of real usage
3. **Broken budget calculation** — reads stale cache, shows false 100% pressure

**Impact**: Cannot distinguish real routing behavior from test artifacts, making quota management impossible.

---

## Problem 1: Routing Database Contamination

### What the data shows:

```bash
$ sqlite3 ~/.llm-router/usage.db
SELECT COUNT(*) as total, 
       COUNT(CASE WHEN cost_usd = 0.01 THEN 1 END) as artificial,
       COUNT(CASE WHEN final_model LIKE 'test/%' THEN 1 END) as test_models
FROM routing_decisions
WHERE timestamp > datetime('now', '-3 days');

total=1977, artificial=1974, test_models=188
```

**Interpretation**: 
- 1,974 of 1,977 records (99.8%) have artificial cost_usd = 0.01
- 188 entries have model names like "test/llm-1", "test/llm-2" (not real models)
- Only ~3 real routing decisions in last 3 days

### Why this matters:

The routing_decisions table is supposed to be the source of truth for:
- Which models were selected for which tasks
- How many tokens were consumed
- How much each decision cost
- Whether Claude was routed vs external models

**Instead**, it's filled with uniform, artificial test data that makes analysis impossible.

### Root cause:

The HUD layer (`statusline_hud.py`) calls `record_routing_decision()` but this **only updates in-memory state**, not the database. The actual database write happens in `cost.py`'s `persist_routing_decision()` function, which appears to:
- Not be called by the routing hooks
- Not distinguish test calls from real calls
- Default to artificial cost values (0.01) when real cost isn't provided

### Real routing decisions look like:
```
timestamp='2026-04-23T15:43:22Z'
task_type='code'
complexity='simple'
final_model='ollama/qwen3.5'  ← Local model, no cost
final_provider='ollama'
cost_usd=0.0
success=1
```

**vs test data looks like:**
```
timestamp='2026-04-16T14:54:31Z'
task_type='test/model'
final_model='test/llm-1'
final_provider='test'
cost_usd=0.01  ← Uniform artificial cost
success=1
```

---

## Problem 2: Claude Usage Not Tracked per Routing

### What the data shows:

```bash
$ sqlite3 ~/.llm-router/usage.db
SELECT timestamp, model, tokens_used FROM claude_usage ORDER BY timestamp DESC LIMIT 5;

2026-04-16 17:27:30|haiku|5000
2026-04-16 17:27:30|opus|5000
2026-04-16 17:23:13|haiku|5000
2026-04-16 17:23:13|opus|5000
2026-04-16 17:20:59|haiku|5000
```

**Interpretation**:
- All 91 claude_usage records have exactly 5,000 tokens
- They appear in paired haiku/opus entries with identical timestamps
- Only test data from April 16 (7 days ago)
- No correlation with actual routing decisions

### Why this matters:

When `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`, the router should log:
- Which prompts were handled by Claude subscription models
- How many tokens each consumed
- How this affected quota pressure

**Instead**, the claude_usage table contains only test data with no connection to real routing.

### What's missing:

The routing system should insert to `claude_usage` whenever a Claude model (haiku/sonnet/opus) is selected:

```python
# In router.py, after selecting a Claude model:
await insert_claude_usage(
    model=selected_model,  # 'haiku', 'sonnet', 'opus'
    tokens_used=estimated_tokens,  # From classification input
    complexity=classification.complexity.value,  # 'simple', 'moderate', 'complex'
    cost_saved=cost_vs_opus,  # How much cheaper than Opus
)
```

This never happens — there's no hook connecting routing decisions to Claude usage tracking.

---

## Problem 3: Budget Pressure Calculation Uses Stale Cache

### What happened:

When you ran routing decisions, the hook checked `~/.llm-router/usage.json` and found:

```json
{
  "session_pct": 7,
  "weekly_pct": 100,  ← FALSE! Said exhausted
  "monthly_pct": 24,
  "sonnet_pct": 0
}
```

But the actual data showed:
```bash
$ llm_budget  # Real API call to Claude
Gemini: 27% used ($1.34 of $5)
OpenAI: 11% used ($2.12 of $20)
Claude: 12% session pressure (FALSE weekly=100% cache)
```

### Root cause:

The hook reads the cached usage.json without validating its age:
1. Previous session (or earlier) left cache at weekly_pct=100
2. Hook doesn't check timestamp or re-fetch from API
3. Displays false "exhausted" state, triggering conservative routing
4. User gets Ollama/Gemini instead of available Claude quota

### Fallback chain affected:

When budget pressure shows 100%:
```
Routing chain: Ollama → Gemini Flash (cheap) → ... → Claude (expensive)
Actual state:  Ollama → Gemini Flash (cheap) → ... → Claude (AVAILABLE)

Result: Cheap model used, expensive quota wasted
Cost: $2.50 API saved vs $100+ efficiency lost
```

---

## How Claude Quota SHOULD Be Tracked

### Current flow (broken):

```
router.py → statusline_hud.py (record in memory only)
         ↓
     No database write
         ↓
     No claude_usage table entry
         ↓
     Budget calculation reads stale cache
         ↓
     Shows false 100% pressure
```

### Correct flow (what needs to happen):

```
1. User sends prompt → Claude Code receives it
2. router.py classifies → determines if Claude model needed
3. Claude model selected (haiku/sonnet/opus) →
4. Hook intercepts call in auto-route.py
5. Write to claude_usage table:
   - timestamp: now
   - model: 'haiku' | 'sonnet' | 'opus'
   - tokens_used: estimated from prompt
   - complexity: 'simple' | 'moderate' | 'complex'
   - cost_saved_usd: cost_opus - cost_selected
6. After call completes, update routing_decisions:
   - final_model: selected model
   - tokens_used: actual consumed (from LLM response)
   - cost_usd: actual cost from Claude API
   - final_provider: 'claude_subscription'
7. At session end, compute:
   - Total Claude tokens this session
   - Tokens used of weekly quota
   - Which routing decisions contributed to quota consumption
```

### Example output (what you SHOULD see):

```
Routing Analysis — How Different Models Consumed Claude Quota
──────────────────────────────────────────────────────────────

Session: 2026-04-23, 07:00–07:15 GMT+1
Claude Quota Before: 12% used (session)
Claude Quota After:  18% used (session)
Tokens Consumed This Session: 6,000

Routing Decisions:
┌────┬───────────────────┬──────────┬────────┬────────────┬──────────┐
│ ID │ Task              │ Selected │ Tokens │ Cost (USD) │ Reason   │
├────┼───────────────────┼──────────┼────────┼────────────┼──────────┤
│  1 │ code/simple       │ haiku    │ 1,200  │ -$0.0008   │ Budget   │
│  2 │ analyze/moderate  │ sonnet   │ 2,500  │ -$0.0045   │ Quality  │
│  3 │ query/simple      │ ollama   │    0   │ $0.0000    │ Free ✓   │
│  4 │ code/moderate     │ sonnet   │ 2,300  │ -$0.0041   │ Quality  │
└────┴───────────────────┴──────────┴────────┴────────────┴──────────┘

Claude Subscription Summary:
  Haiku:   1 call,   1,200 tokens, saved $0.0008
  Sonnet:  2 calls,  4,800 tokens, saved $0.0086
  Opus:    0 calls,      0 tokens, saved $0.0000
  ─────────────────────────────────────────
  TOTAL:   3 calls,  6,000 tokens, saved $0.0094

Why Quota Went Up:
  This session consumed 6,000 tokens from subscription quota
  Equivalent to 0.6 Opus prompts (10K token avg)
  Weekly capacity ~45,000 tokens → 13% of weekly remaining
```

---

## Why Test Data Is In The System

### Hypothesis:

1. **Development/testing** — Earlier sessions created mock routing decisions with test data
2. **No cleanup** — Test data was never removed from production database
3. **No schema validation** — Routing inserts don't validate provider/model names
4. **No filtering in analysis** — `llm_budget`, `llm_usage` commands don't exclude test data

### Evidence:

```bash
# Test entries all from April 16 (older, concentrated in time)
$ sqlite3 ~/.llm-router/usage.db \
  "SELECT DATE(timestamp), COUNT(*) FROM routing_decisions \
   WHERE final_model LIKE 'test/%' GROUP BY DATE(timestamp);"

2026-04-16|188
2026-04-17|0
2026-04-18|0
2026-04-19|0
2026-04-20|0
2026-04-21|0
2026-04-22|0
2026-04-23|0

# Then regular artificial data started (uniform 0.01 cost)
$ sqlite3 ~/.llm-router/usage.db \
  "SELECT DATE(timestamp), COUNT(*) FROM routing_decisions \
   WHERE cost_usd = 0.01 AND final_model NOT LIKE 'test/%' \
   GROUP BY DATE(timestamp) ORDER BY DATE(timestamp);"

2026-04-16|1786
2026-04-17|0
...
```

The test data from April 16 is old, but the artificial 0.01-cost entries continued.

---

## Fixes Needed (Priority Order)

### P0 — Immediate (blocks all analysis):

1. **Validate routing inputs before database insert**
   - Whitelist valid providers: ollama, openai, gemini, codex, claude_subscription
   - Whitelist valid models: haiku, sonnet, opus, gpt-4o, gpt-4o-mini, gemini-2.5-flash, etc.
   - Reject entries with provider='test' or cost_usd=0.01 (unless genuinely free)
   
2. **Add timestamp validation to budget cache**
   - Check age of usage.json before using it
   - If >5 minutes old, refresh from API (via llm_refresh_claude_usage)
   - Fall back to conservative pressure (0.5 = 50%) if can't refresh

3. **Connect routing decisions to claude_usage table**
   - When a Claude model is selected, log to claude_usage immediately
   - Include: timestamp, model, estimated_tokens, complexity
   - Link routing_decisions.id to source prompt

### P1 — High (enables real analysis):

4. **Separate Claude routing from external routing**
   - routing_decisions.final_provider should be 'claude_subscription' for Claude calls
   - Track which decision cascaded to Claude vs stayed on cheaper models

5. **Implement decision audit trail**
   - Store which models were considered but rejected
   - Store why each model was chosen/rejected (budget, confidence, latency)
   - Enable "what if I had used Opus?" analysis

### P2 — Medium (improves UX):

6. **Create quota consumption report**
   - Show per-task-type Claude usage
   - Show savings vs "always use Opus" baseline
   - Show which routing decisions were most efficient

---

## How to Verify Fixes Are Working

### Test 1: Make a real routing decision, verify it's recorded

```bash
# Clear old test data (do NOT do this in production without backup!)
sqlite3 ~/.llm-router/usage.db "DELETE FROM routing_decisions WHERE final_model LIKE 'test/%' OR cost_usd = 0.01;"

# Make a real routing call
curl -X POST http://localhost:8000/api/route \
  -H "Content-Type: application/json" \
  -d '{
    "task": "code",
    "complexity": "simple",
    "prompt": "Write a hello world function"
  }'

# Check it was recorded correctly
sqlite3 ~/.llm-router/usage.db \
  "SELECT task_type, final_model, cost_usd, success FROM routing_decisions ORDER BY id DESC LIMIT 1;"

# Should show:
# code|ollama/qwen3.5|0.0|1  (or similar, NOT test/model or cost=0.01)
```

### Test 2: Verify Claude routing increments claude_usage table

```bash
# Monitor claude_usage table
sqlite3 ~/.llm-router/usage.db \
  "SELECT COUNT(*) as records, \
          MIN(timestamp) as oldest, \
          MAX(timestamp) as newest \
   FROM claude_usage \
   WHERE timestamp > datetime('now', '-1 hour');"

# Make a routing decision that selects Claude
# Should see new record added with current timestamp
```

### Test 3: Verify budget cache is validated

```bash
# Export CLAUDE_FORCE_BUDGET_REFRESH=1
# Run a routing decision
# Check the hook output shows:
# ✓ Cache valid, pressure=XX%
# or
# ↻ Cache stale, refreshed at 2026-04-23T07:10:00Z

# If cache is old, should see:
# ⚠ Cache age > 5m (from 07:05), refreshing...
```

---

## Your Specific Question Answered

**"Show me how different routings raised the Claude quota"**

Currently, this is impossible because:

1. ❌ Routing database has test data, can't identify real Claude selections
2. ❌ Claude usage table is empty (no real data)
3. ❌ Budget cache shows false 100%, prevents real quota insights

Once fixed, you'll see:

```
Routing Decision #42 (code/simple):
  Selected: claude/haiku (12% cheaper than sonnet)
  Tokens: 1,200
  Claude quota before: 12% used
  Claude quota after:  15% used
  Reason: Classification confidence 87%, haiku sufficient for task

Routing Decision #43 (analyze/moderate):
  Selected: claude/sonnet (better quality needed)
  Tokens: 2,500
  Claude quota before: 15% used
  Claude quota after:  19% used
  Reason: Classification confidence 65%, escalated to sonnet for safety

Cost-benefit: Saved $0.0008 on #42, paid $0.0045 on #43
Net: Lost $0.0037, gained better analysis quality on complex task
```

---

## Recommendation

**Before diving deeper into usage analysis**, fix the data quality issues (P0 items). Otherwise, you're analyzing noise, not signal.

The fact that you caught the false 100% quota is a sign the system needs hardening. The root problems are:

1. **No validation** — accepts garbage data
2. **No audit trail** — can't tell test from real
3. **No freshness checks** — serves stale cache as truth
4. **No integration** — routing decisions don't flow to quota tracking

These three issues combined explain why "I don't understand the usage with the subscription of Claude" — the data is broken, not your understanding.
