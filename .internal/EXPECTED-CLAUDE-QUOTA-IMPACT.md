# Expected Claude Quota Impact — Different Routing Scenarios

**Purpose**: Show what REAL Claude quota tracking should look like, and how different routing decisions affect quota consumption.

**Note**: This is a **fictional but realistic example** using correct data structures. The actual system cannot produce this currently due to data quality issues.

---

## Scenario: 15-Minute Coding Session (Real Data)

### Session Setup
- Time: 2026-04-23, 15:00–15:15 GMT+1
- LLM_ROUTER_CLAUDE_SUBSCRIPTION=true
- Claude quota at session start: 45,000 tokens available (session limit)
- Weekly quota at session start: 180,000 available

---

## Actual Routing Decisions (What You Should See)

### Decision #1: Simple Code Generation

```
Prompt: "Write a hello world function in Python"
Classification:
  Type: code
  Complexity: simple
  Confidence: 94%
  
Routing Chain Evaluation:
  1. Ollama/qwen3.5 [87% match] ← SELECTED (0 cost)
     Reason: Simple generation, local model sufficient
     
Routing NOT selecting Claude:
  2. Claude/haiku [85% match] — skipped
     Reason: Lower confidence, but free alternative available
  3. Claude/sonnet [91% match] — not needed
  4. OpenAI/gpt-4o-mini [89% match] — not needed

Result:
  Model selected: ollama/qwen3.5
  Tokens used: 0 (local model)
  Cost: $0
  Claude quota change: 0 tokens (not used)
  Savings vs Opus: $0.0023
```

**Claude Quota Status**: 45,000 → 45,000 (unchanged ✓)

---

### Decision #2: Code Analysis (Moderate Complexity)

```
Prompt: "Analyze this async iterator pattern for efficiency issues"
Classification:
  Type: analyze
  Complexity: moderate
  Confidence: 68%
  
Routing Chain Evaluation:
  1. Ollama/qwen3.5 [61% match] — insufficient confidence
  2. Claude/haiku [73% match] — marginal, but available
  3. Claude/sonnet [85% match] ← SELECTED
     Reason: Confidence 68%, analysis task needs quality
     Claude subscription cheaper than OpenAI
     
Not considered further:
  4. OpenAI/gpt-4o [91% match] — more expensive
  5. Claude/opus [89% match] — overkill for this task

Result:
  Model selected: claude/sonnet
  Tokens used: 2,400 (estimated input 800 + output 1,600)
  Cost: -$0.0045 vs Opus baseline
  Claude quota change: 2,400 tokens (FROM subscription)
  Reasoning: Quality required, sonnet balanced cost/capability
```

**Claude Quota Status**: 45,000 → 42,600 (used 2,400 tokens)  
**Weekly Quota**: 180,000 → 177,600

---

### Decision #3: Simple Question

```
Prompt: "What is the difference between async/await and generators?"
Classification:
  Type: query
  Complexity: simple
  Confidence: 92%
  
Routing Chain Evaluation:
  1. Ollama/qwen3.5 [89% match] ← SELECTED (0 cost)
     Reason: High confidence, local model sufficient
     
Not selected:
  2. Claude/haiku [88% match] — local alternative exists
  3. OpenAI/gpt-4o-mini [87% match] — not needed

Result:
  Model selected: ollama/qwen3.5
  Tokens used: 0
  Cost: $0
  Claude quota change: 0 tokens
  Savings vs Opus: $0.0018
```

**Claude Quota Status**: 42,600 → 42,600 (unchanged ✓)

---

### Decision #4: Complex Problem Solving

```
Prompt: "Design an event-driven architecture for a real-time collaboration system. Include: event sourcing, eventual consistency, conflict resolution, scaling considerations"
Classification:
  Type: analyze
  Complexity: complex
  Confidence: 52%
  
Routing Chain Evaluation:
  1. Ollama/qwen3.5 [41% match] — confidence too low
  2. Claude/haiku [58% match] — risky for complex task
  3. Claude/sonnet [72% match] — moderate, might struggle
  4. Claude/opus [91% match] ← SELECTED
     Reason: Complex task, low confidence (52%), needs best reasoning
     
Not considered:
  5. OpenAI/o3 [94% match] — more expensive than Opus

Result:
  Model selected: claude/opus
  Tokens used: 4,800 (estimated input 1,200 + output 3,600)
  Cost: $0 (baseline used for comparison)
  Claude quota change: 4,800 tokens (FROM subscription)
  Reasoning: Complex architectural decision, low confidence forced escalation
```

**Claude Quota Status**: 42,600 → 37,800 (used 4,800 tokens)  
**Weekly Quota**: 177,600 → 172,800

---

### Decision #5: Code Refactoring with Quality Focus

```
Prompt: "Refactor this payment processor to be more testable. Current: tight coupling to database. Goal: dependency injection, mock-friendly"
Classification:
  Type: code
  Complexity: moderate
  Confidence: 75%
  
Routing Chain Evaluation:
  1. Ollama/qwen3.5 [72% match] — borderline, risky for refactoring
  2. Claude/haiku [76% match] — okay, but...
  3. Claude/sonnet [88% match] ← SELECTED
     Reason: Moderate complexity + refactoring = needs higher quality
     Confidence 75% suggests haiku risky
     Opus unnecessary
     
Not selected:
  4. Claude/opus [86% match] — overkill
  5. OpenAI/gpt-4o [84% match] — more expensive

Result:
  Model selected: claude/sonnet
  Tokens used: 2,000 (estimated input 600 + output 1,400)
  Cost: -$0.0036 vs Opus baseline
  Claude quota change: 2,000 tokens (FROM subscription)
  Reasoning: Moderate + refactoring task benefits from sonnet, sonnet cheaper
```

**Claude Quota Status**: 37,800 → 35,800 (used 2,000 tokens)  
**Weekly Quota**: 172,800 → 170,800

---

## Session Summary (15 minutes)

```
╔═════════════════════════════════════════════════════════╗
║            SESSION QUOTA IMPACT ANALYSIS                 ║
╠═════════════════════════════════════════════════════════╣
║ Total Routing Decisions:      5                          ║
║ Claude Used:                  3 decisions                ║
║ Local/API Used:               2 decisions                ║
╠═════════════════════════════════════════════════════════╣
║ TOKENS CONSUMED THIS SESSION:                            ║
║   Claude/Haiku:    0 calls,     0 tokens (not used)     ║
║   Claude/Sonnet:   2 calls,  4,400 tokens              ║
║   Claude/Opus:     1 call,   4,800 tokens              ║
║   ─────────────────────────────────────────────────    ║
║   TOTAL CLAUDE:    3 calls,  9,200 tokens              ║
║   Ollama:          2 calls,     0 tokens (free)        ║
╠═════════════════════════════════════════════════════════╣
║ QUOTA CONSUMPTION:                                       ║
║   Session quota before: 45,000 tokens                   ║
║   Session quota after:  35,800 tokens                   ║
║   Session used:          9,200 tokens (20.4%)           ║
║                                                          ║
║   Weekly quota before: 180,000 tokens                   ║
║   Weekly quota after:  170,800 tokens                   ║
║   Weekly used so far:    9,200 tokens (5.1%)            ║
╠═════════════════════════════════════════════════════════╣
║ COST ANALYSIS:                                           ║
║   Actual cost:           $0.035 (sonnet $0.027 + opus)  ║
║   If all used Claude:    $0.048 (opus pricing)          ║
║   If all used Haiku:     $0.009 (haiku pricing)         ║
║   ─────────────────────────────────────────────────    ║
║   Savings vs Opus:       $0.013 (27% reduction)         ║
║   Cost vs free:          $0.035 (chose quality)         ║
║                                                          ║
║   ROI: Paid $0.013 extra to get better quality on       ║
║        2 moderate tasks (#2) and 1 complex (#4)         ║
╠═════════════════════════════════════════════════════════╣
║ ROUTING EFFICIENCY:                                      ║
║   Decisions routed locally:       2 (0% cost)           ║
║   Decisions using Claude:         3 ($0.035 cost)       ║
║   Decisions never routed OpenAI:  0 (would be $0.08)    ║
║                                                          ║
║   Efficiency: 60% local, 40% Claude, 0% external API    ║
║   Alternative "always Opus": $0.048 for same session    ║
║   Savings: $0.013 (27%)                                 ║
╚═════════════════════════════════════════════════════════╝
```

---

## Key Insights: How Different Decisions Affected Quota

### 1. Simple Tasks Don't Need Claude
- **Decision #1** (hello world): Routed to Ollama
- **Decision #3** (async/await question): Routed to Ollama
- **Quota impact**: 0 tokens (saved $0.0041)

**Principle**: Confidence >90% on simple tasks → local models are optimal

### 2. Moderate Tasks Benefit from Claude/Sonnet
- **Decision #2** (analyze pattern): 2,400 tokens
- **Decision #5** (refactor code): 2,000 tokens
- **Quota impact**: 4,400 tokens, cost $0.0081

**Principle**: Confidence 68-75% + moderate complexity → Claude/Sonnet balances cost/quality

### 3. Complex Tasks Need Claude/Opus When Confidence Is Low
- **Decision #4** (architecture): 4,800 tokens at 52% confidence
- **Quota impact**: 4,800 tokens, costs more but necessary

**Principle**: Complexity=complex AND confidence<60% → escalate to Opus despite quota cost

### 4. Total Quota Pressure After 15 Minutes
- Started: 45,000 token budget (session limit)
- Used: 9,200 tokens (20.4% of session)
- Remaining: 35,800 tokens
- Weekly: 170,800 remaining (of 180,000)

If this pattern continued for 8 hours:
- Session usage: ~295,000 tokens
- **Result**: Hits session limit (45,000 token max) and refocuses to Ollama/free models

---

## Why This Matters for Your Setup

### Current State (Broken)
```
You can't see how Claude quota increases because:
1. routing_decisions table shows test data, not real models
2. claude_usage table is empty
3. Budget cache shows false 100% pressure
4. No link between routing decision and quota impact
```

### What You SHOULD Be Able To See (Once Fixed)
```
Query: "Show me how routing decisions consumed quota"
Response:
  Sonnet: 2 calls (4,400 tokens) on moderate tasks
  Opus: 1 call (4,800 tokens) on complex task
  Haiku: 0 calls (skipped in favor of local)
  
  Total: 9,200 tokens used from subscription
  
  Why quota went up:
  - Decision #2: Chose Sonnet over Ollama (confidence issue)
  - Decision #4: Chose Opus over Sonnet (complexity issue)
  - Decisions #1, #3, #5: Used free/cheap alternatives
```

---

## Expected Quota Growth Patterns

### Scenario A: Lots of Simple Tasks (Ideal)
```
Session with 20 routing decisions, all simple:
  20 decisions → Ollama (0 tokens)
  Claude quota used: 0
  Cost: $0
  Efficiency: 100% local
```

### Scenario B: Mixed Complexity (Common)
```
Session with 20 routing decisions:
  5 simple → Ollama (0 tokens)
  10 moderate → Claude/Sonnet (18,000 tokens)
  3 complex → Claude/Opus (9,600 tokens)
  2 edge cases → OpenAI (for diversity)
  
  Claude quota used: 27,600 tokens
  Cost: $0.095
  Efficiency: 75% had good alternatives, 25% needed escalation
```

### Scenario C: Hard Session (Low Confidence)
```
Session with 20 routing decisions, all <60% confidence:
  20 moderate-to-complex → Claude/Opus (48,000 tokens)
  
  Claude quota used: 48,000 tokens
  Cost: $0.168
  Efficiency: 0% local options available
  
  Weekly quota impact: 48,000/180,000 = 26.7% of weekly used in one session
```

---

## How to Verify This When System Is Fixed

### Check 1: Query quota contribution by model selection

```sql
SELECT 
  final_model,
  COUNT(*) as calls,
  SUM(tokens_used) as tokens,
  SUM(cost_usd) as cost,
  AVG(classifier_confidence) as avg_confidence
FROM routing_decisions
WHERE timestamp > datetime('now', '-7 days')
GROUP BY final_model
ORDER BY tokens DESC;
```

**Expected output after fix:**
```
ollama/qwen3.5   | 147 | 0      | 0.00   | 0.91
claude/sonnet    | 12  | 18600  | 0.068  | 0.72
claude/opus      | 3   | 9800   | 0.034  | 0.51
gemini/flash     | 8   | 4200   | 0.012  | 0.69
openai/gpt4o-mini| 2   | 1200   | 0.009  | 0.55
```

### Check 2: Correlate Claude usage to quota growth

```sql
SELECT 
  timestamp,
  SUM(tokens_used) as tokens_this_hour,
  AVG(classifier_confidence) as avg_confidence,
  COUNT(CASE WHEN final_model LIKE 'claude/%' THEN 1 END) as claude_calls
FROM routing_decisions
WHERE timestamp > datetime('now', '-24 hours')
GROUP BY DATE(timestamp), HOUR(timestamp)
ORDER BY timestamp DESC;
```

**Expected pattern:**
- Hours with low average confidence → more Claude calls → more tokens used
- Hours with high average confidence → more Ollama calls → 0 tokens used
- Quota growth directly proportional to confidence drops

### Check 3: Weekly quota pressure tracking

```sql
SELECT 
  DATE(timestamp) as day,
  SUM(CASE WHEN final_model LIKE 'claude/%' THEN tokens_used ELSE 0 END) as claude_tokens_today,
  (SELECT SUM(tokens_used) FROM claude_usage WHERE DATE(timestamp) = DATE(routing_decisions.timestamp)) as total_claude_today
FROM routing_decisions
WHERE timestamp > datetime('now', '-7 days')
GROUP BY DATE(timestamp)
ORDER BY DATE(timestamp) DESC;
```

---

## Summary: Your Question Answered

**"Show me how different routings raise Claude quota"**

Once the data quality issues are fixed, you'll see:

1. **Every routing decision logs to `claude_usage` table** with model, tokens, complexity
2. **Quota pressure is updated in real-time** as tokens accumulate
3. **You can query which decisions consumed quota**:
   - Simple tasks → Ollama (0 tokens)
   - Moderate + low confidence → Claude/Sonnet (expensive)
   - Complex + low confidence → Claude/Opus (most expensive)
4. **Weekly growth is trackable**: "Added 9,200 tokens this session, weekly at 15% now"
5. **Cost-benefit visible**: "Spent extra $0.013 to get better quality on 2 tasks"

Right now, this insight is **hidden** behind test data and broken tracking.

**Next step**: Fix data quality issues (P0 items in CLAUDE-QUOTA-TRACKING-ISSUES.md), then come back to this analysis with real data.
