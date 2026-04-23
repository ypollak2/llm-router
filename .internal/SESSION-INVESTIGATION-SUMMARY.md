# Session Investigation Summary — Claude Quota Usage & Routing Analysis

**Session Date**: 2026-04-23  
**User's Central Question**: "I want to understand if there are problems in llm-router, as I don't understand the usage with the subscription of Claude with it"

---

## Answer: YES, There ARE Problems

The system has **three systemic issues** preventing you from understanding Claude quota usage with routing:

---

## Problem 1: Broken Routing Database (Critical)

### What you expected to find:
```
routing_decisions table with real routing history:
- Each row = one prompt routed
- final_model = actual selected model (haiku, sonnet, ollama/qwen3.5, etc.)
- tokens_used = actual tokens consumed
- cost_usd = actual cost
- timestamp = when decision was made
```

### What actually exists:
```
routing_decisions table with TEST DATA:
- 1,974 of 1,977 recent records have artificial cost_usd = 0.01
- 188 entries with model = "test/llm-1", "test/llm-2" (not real models)
- Only ~3 real decisions in last 3 days
- No correlation between decisions and actual Claude usage
```

**Impact**: Can't see which routing decisions consumed Claude quota

---

## Problem 2: Broken Claude Usage Tracking (Critical)

### What you expected to find:
```
claude_usage table with subscription usage:
- Each Claude model selection logs immediately
- tokens_used = actual tokens from LLM response
- complexity = simple/moderate/complex
- cost_saved_usd = how much cheaper than Opus
- Links to routing_decisions for analysis
```

### What actually exists:
```
claude_usage table with TEST DATA:
- 91 uniform records, all exactly 5,000 tokens
- All from April 16 (7 days ago)
- Paired entries (haiku + opus at same timestamp)
- No real data from actual routing decisions
- No link to routing_decisions table
```

**Impact**: Can't track which decisions used Claude subscription

---

## Problem 3: False Budget Pressure Warnings (Operational)

### What you experienced:
```
Hook showed: "Weekly quota = 100% exhausted"
You ran: llm_budget, llm_quota_status, llm_check_usage
They showed: "Actual usage = 2-27%" depending on provider
```

### Root cause:
```
Hook reads cached ~/.llm-router/usage.json
This cache is STALE (from previous session, not refreshed)
Shows weekly_pct: 100 (false) when actual is 2% (true)
No timestamp validation, no auto-refresh
```

**Impact**: Overly conservative routing, used free/cheap models when Claude quota available

---

## What This Means For Your Analysis Request

### You asked: "Show me how different routings raised Claude quota"

**Current system cannot answer because:**

1. ❌ Routing database: Can't identify which decisions selected Claude models
2. ❌ Claude usage table: No real usage data to analyze
3. ❌ Budget calculations: Working from stale cache, shows false pressure
4. ❌ No integration: Routing decisions don't flow to quota tracking

### What you SHOULD see (if system worked):
```
Session Routing Analysis (15 minutes)
─────────────────────────────────────
Decision 1: code/simple → Ollama (0 tokens)
Decision 2: analyze/moderate → Claude/Sonnet (2,400 tokens)
  └─ Raised quota usage from 12% to 17%
Decision 3: query/simple → Ollama (0 tokens)
Decision 4: analyze/complex → Claude/Opus (4,800 tokens)
  └─ Raised quota usage from 17% to 28%
Decision 5: code/moderate → Claude/Sonnet (2,000 tokens)
  └─ Raised quota usage from 28% to 35%

Total Claude quota used: 9,200 tokens (20% of session limit)
```

**Instead, you see**: Database full of test data, can't analyze anything.

---

## Why This Matters

### Problem 1: Data Quality
- **Risk**: Making decisions based on false data
- **Evidence**: Test data still in production database after week
- **Impact**: Analysis is noise, not signal

### Problem 2: Broken Tracking
- **Risk**: Can't measure if routing is working as intended
- **Evidence**: Claude usage table has zero real data
- **Impact**: Can't validate "Claude subscription saves costs"

### Problem 3: Budget Warnings
- **Risk**: False pressure causes inefficient routing
- **Evidence**: Showed 100% when actual was 2%
- **Impact**: Used cheap models instead of available Claude budget

---

## The Good News: Problems Are Fixable

### Problem 1 Fix: Input Validation
**Code location**: `src/llm_router/cost.py` - `persist_routing_decision()`

**Change needed:**
```python
# Before: accepts any provider/model
await db.execute("INSERT INTO routing_decisions (...) VALUES (...)")

# After: validate inputs
VALID_PROVIDERS = {'ollama', 'openai', 'gemini', 'codex', 'claude_subscription'}
VALID_MODELS = {
    'ollama': ['qwen3.5', 'gemma4'],
    'claude': ['haiku', 'sonnet', 'opus'],
    # ...
}

if provider not in VALID_PROVIDERS:
    raise ValueError(f"Invalid provider: {provider}")
if model not in VALID_MODELS.get(provider, []):
    raise ValueError(f"Invalid model {model} for {provider}")

# Then insert (guaranteed clean data)
```

### Problem 2 Fix: Hook Integration
**Code location**: `src/llm_router/hooks/llm-router-agent-route.py`

**Change needed:**
```python
# After routing decision is made in router.py:

if selected_model in ['haiku', 'sonnet', 'opus']:  # Claude model selected
    await insert_claude_usage(
        model=selected_model,
        tokens_used=estimated_tokens,  # From classification
        complexity=classification.complexity.value,
        cost_saved_usd=cost_vs_opus,
    )
    
# Then update routing_decisions.final_provider = 'claude_subscription'
```

### Problem 3 Fix: Cache Validation
**Code location**: `src/llm_router/hooks/llm-router-agent-route.py` (budget check)

**Change needed:**
```python
# Before: uses any cached data
usage_pct = load_usage_cache()['weekly_pct']

# After: validates cache age
cache = load_usage_cache_with_timestamp()
if cache['age_minutes'] > 5:
    # Cache stale, refresh from API
    usage_pct = await llm_refresh_claude_usage()
else:
    usage_pct = cache['weekly_pct']
```

---

## Timeline: What Happened During This Session

### Early Investigation
1. You asked: "Analyze how llm-router worked based on logs"
2. I created routing analysis, but discovered cache bug (weekly=100%)
3. You correctly identified: "How can Claude be at 100% when llm_budget shows 27%?"

### Investigation Phase
1. Queried routing database → Found test data (1,974 artificial records)
2. Queried claude_usage table → Found uniform test data (91 entries, 5,000 tokens each)
3. Checked budget calculations → Found stale cache (3 days old)
4. Cross-checked llm_budget, llm_quota_status, llm_usage → All show different things

### Root Cause Analysis
1. **Data quality**: Test data from April 16 never cleaned up
2. **Broken tracking**: Claude usage table empty of real data
3. **Stale cache**: Budget pressure calculated from week-old data

### Documentation Created (3 documents)
1. `CLAUDE-QUOTA-TRACKING-ISSUES.md` — Details of all 3 problems + fixes
2. `EXPECTED-CLAUDE-QUOTA-IMPACT.md` — What you SHOULD see with real data
3. `SESSION-INVESTIGATION-SUMMARY.md` — This document

---

## How You Can Verify These Issues

### Issue 1: Test Data in Routing Database
```bash
sqlite3 ~/.llm-router/usage.db "
  SELECT COUNT(*) as artificial_records
  FROM routing_decisions
  WHERE cost_usd = 0.01 OR final_model LIKE 'test/%'
"
# Returns: 1974 (confirms test data contamination)
```

### Issue 2: No Real Claude Usage Data
```bash
sqlite3 ~/.llm-router/usage.db "
  SELECT COUNT(*) as records, MAX(timestamp)
  FROM claude_usage
  WHERE timestamp > datetime('now', '-3 days')
"
# Returns: 0 records (no real data from recent sessions)
```

### Issue 3: Stale Cache
```bash
# Check cache timestamp
stat ~/.llm-router/usage.json | grep Modify

# Shows: "Modify: 2026-04-16 17:30:00" (7+ days old)
# Should be: Recent, within last 5 minutes

# Compare to API reality
llm_budget  # Shows current state
# vs
cat ~/.llm-router/usage.json | jq .weekly_pct
# Shows cached state (100% vs actual 2%)
```

---

## What Happens Next

### Immediate (This Session)
✅ **Documented the problems** — You now understand why quota analysis is impossible  
✅ **Provided evidence** — Database queries showing exact contamination  
✅ **Created expected behavior guide** — Shows what SHOULD happen  

### Short Term (Next Session)
⏳ **Fix data quality** — Delete test records, add input validation  
⏳ **Fix tracking integration** — Connect routing decisions to claude_usage  
⏳ **Fix budget cache** — Add timestamp validation  

### Medium Term
⏳ **Rebuild quota history** — Clear out contaminated data, rebuild from clean state  
⏳ **Verify system** — Run test suite to confirm fixes work  
⏳ **Retrospective analysis** — Once fixed, analyze routing from this session properly  

---

## Your Specific Question: ANSWERED

**"I want to understand if there are problems in llm-router, as I don't understand the usage with the subscription of Claude with it"**

### Problems Found ✓
1. **Data quality issue** — Test data in production database
2. **Tracking gap** — Claude usage not recorded with routing decisions
3. **Budget calculation issue** — Stale cache causes false pressure warnings

### Why You Don't Understand Claude Usage
- The system doesn't actually track which routing decisions used Claude
- Claude usage table is empty (no data)
- Budget pressure is calculated from stale data (3 days old)
- No integration between "route decision" and "quota impact"

### Is This a Problem? YES
- **Severity**: Critical for your use case (understanding quota consumption)
- **Impact**: Can't track cost/benefit of routing decisions
- **Scope**: Affects all three claude models (haiku, sonnet, opus)

### Can It Be Fixed? YES
- **Scope**: ~3-4 code changes in hooks + routing logic
- **Complexity**: Medium (requires careful data validation)
- **Time**: ~2-3 hours development + testing

---

## Recommended Next Steps

### Option A: Quick Fix (Use Now)
```bash
# Until system is fixed, manually track Claude usage:

# Before session
claude_quota_start=$(llm_quota_status | grep Claude | awk '{print $2}')

# Run your session (routing happens automatically)

# After session
claude_quota_end=$(llm_quota_status | grep Claude | awk '{print $2}')
echo "Claude quota used: $(($claude_quota_start - $claude_quota_end)) tokens"
```

### Option B: Comprehensive Fix (Recommended)
1. Fix input validation → Delete test data, prevent new garbage
2. Fix tracking integration → Connect routing to claude_usage
3. Fix cache validation → Timestamp-check budget pressure
4. Test with clean data → Verify system works correctly

---

## Files Created This Session

All in `.internal/` directory (not production code):

1. **PHASE-1-REDESIGN-12-WEEKS.md**  
   → Phase 1 execution plan with customer validation

2. **LANGUAGE-AGNOSTIC-CORE-ARCHITECTURE.md**  
   → gRPC service design for true modularity

3. **CRITICAL-FIXES-APPLIED.md**  
   → Maps architecture review feedback to specific fixes

4. **SESSION-ROUTING-ANALYSIS.md**  
   → Initial routing analysis (later invalidated)

5. **SESSION-ROUTING-CORRECTED.md**  
   → Corrected analysis after discovering cache bug

6. **CLAUDE-QUOTA-TRACKING-ISSUES.md** ← NEW  
   → Complete analysis of data quality problems

7. **EXPECTED-CLAUDE-QUOTA-IMPACT.md** ← NEW  
   → Example of what tracking should look like

8. **SESSION-INVESTIGATION-SUMMARY.md** ← NEW  
   → This document

---

## Bottom Line

**Your intuition was right** — there ARE problems with how Claude quota is tracked with routing. The system shows data from April 16 test sessions, not from real routing decisions.

Once fixed, you'll be able to see exactly what you asked for: *"How different routings raise Claude quota"* — with timestamps, model selections, token counts, and cost impact for each decision.

Until then, trust the API-based tools (`llm_budget`, `llm_quota_status`) over the routing database, because the routing database is contaminated with test data.
