# Routing Decision Examples

Real-world examples of how llm-router classifies and routes different tasks. Use these to understand routing behavior in your own workflows.

---

## Quick Reference: Task → Model

```
┌─────────────────────────────────────────────────────────────┐
│ Input: "explain what REST means"                            │
│                                                             │
│ Classifier detects: simple factual question                 │
│ Routed via: llm_query(complexity="simple")                 │
│ Model: Haiku ($0.00001)                                    │
│ Time: <1s                                                  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Input: "debug why my async code times out randomly"         │
│                                                             │
│ Classifier detects: moderate debugging task                 │
│ Routed via: llm_analyze(complexity="moderate")             │
│ Model: Claude Sonnet ($0.003)                              │
│ Time: ~3s                                                  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Input: "design a distributed consensus algorithm"           │
│                                                             │
│ Classifier detects: complex architecture/design             │
│ Routed via: llm_analyze(complexity="complex")              │
│ Model: Claude Opus ($0.015)                                │
│ Time: ~8s                                                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Example 1: Simple Factual Question

**Prompt**: "What is the capital of France?"

### Classification Flow

```
Input Text
    ↓
Heuristic Check (instant)
├─ Keywords: "what is", "capital"
├─ Length: 5 words
├─ Complexity signals: none
    ↓
Result: SIMPLE (99% confidence)
    ↓
Route Decision
├─ Tool: llm_query
├─ Complexity: simple
├─ Budget pressure: 0.2 (plenty of budget)
    ↓
Model Selection Chain
├─ Try: Ollama (qwen3.5) — Available ✅
├─ Return: Haiku answer
    ↓
Response: "Paris" (~0.1s, $0.00001)
```

### Cost Impact

| Approach | Model | Cost | Time |
|----------|-------|------|------|
| Direct (no routing) | Opus | $0.015 | 2s |
| Routed (this example) | Haiku | $0.00001 | <1s |
| **Savings** | - | **99.93%** | **75%** |

### Confidence Signals

- ✅ Starts with "What is" → simple factual question
- ✅ Short prompt (< 20 words) → low complexity
- ✅ No design/architecture keywords → not advanced
- ✅ No code or technical depth → basic lookup

---

## Example 2: Moderate Debugging Task

**Prompt**: "I have an async function that times out randomly. It calls 3 external APIs in parallel, but sometimes it gets stuck. How do I debug this?"

### Classification Flow

```
Input Text
    ↓
Heuristic Check
├─ Keywords: "debug", "async", "times out"
├─ Length: 40 words
├─ Complexity signals: async, parallel APIs, troubleshooting
    ↓
Initial Result: MODERATE (75% confidence)
    ↓
Ollama Classifier (if heuristic < 80%)
├─ Local qwen3.5 refines classification
├─ Detects: real-world debugging scenario
    ↓
Final Result: MODERATE (87% confidence)
    ↓
Route Decision
├─ Tool: llm_analyze
├─ Complexity: moderate
├─ Budget pressure: 0.4 (room to spend)
    ↓
Model Selection
├─ Try: Ollama → too weak for debugging
├─ Try: GPT-4o → Available ✅
├─ Return: Detailed debugging guide
    ↓
Response: ~500 tokens, $0.003, ~3s
```

### Cost Impact

| Approach | Model | Cost | Time |
|----------|-------|------|------|
| Direct (no routing) | Opus | $0.015 | 5s |
| Routed (this example) | Sonnet | $0.003 | 3s |
| **Savings** | - | **80%** | **40%** |

### Confidence Signals

- ✅ Contains "debug" keyword → debugging task
- ✅ Mentions concurrency concerns → moderate complexity
- ✅ Specific technical context (APIs, timeouts) → real-world problem
- ⚠️ Not simple lookup, but doesn't need expert reasoning → moderate

---

## Example 3: Complex Architecture Task

**Prompt**: "Design a distributed consensus algorithm that handles Byzantine failures, recovers from network partitions, and guarantees consistency within 100ms. How would you implement this from scratch?"

### Classification Flow

```
Input Text
    ↓
Heuristic Check
├─ Keywords: "design", "algorithm", "distributed", "Byzantine"
├─ Length: 35 words
├─ Complexity signals: ★★★ (very advanced)
    ↓
Result: COMPLEX (98% confidence) — skips Ollama classifier
    ↓
Route Decision
├─ Tool: llm_analyze
├─ Complexity: complex
├─ Budget pressure: 0.1 (under 10% usage)
    ↓
Model Selection
├─ Budget pressure low → can afford premium
├─ Task complexity: very high
├─ Model chain: skip Ollama → skip Sonnet
├─ Select: Claude Opus ✅
├─ Return: Comprehensive architecture with code examples
    ↓
Response: ~2000 tokens, $0.015, ~8s
```

### Cost Impact

| Approach | Model | Cost | Time |
|----------|-------|------|------|
| Direct (no routing) | Opus | $0.015 | 8s |
| Routed (this example) | Opus | $0.015 | 8s |
| **Savings** | - | **0%** | **0%** |
| *Note* | - | Same cost, but routed via profile | - |

### Why This Wasn't Downgraded

- ❌ "Design from scratch" requires deep expertise
- ❌ "Byzantine failures" + "consistency guarantees" = expert-level knowledge
- ❌ Budget pressure allows premium routing
- ✅ Correct to use Opus for this task

---

## Example 4: Edge Case — Code Generation

**Prompt**: "Write a function to validate email addresses in Python"

### Classification Flow

```
Input Text
    ↓
Heuristic Check
├─ Keywords: "write", "function", "Python" → Code generation
├─ Length: 12 words
├─ Complexity signals: moderate (not trivial, but pattern exists)
    ↓
Initial Result: MODERATE (82% confidence)
    ↓
Route Decision
├─ Tool: llm_code (code generation)
├─ Complexity: moderate
├─ Budget pressure: 0.35
    ↓
Model Selection
├─ Try: Ollama → can handle, but GPT models better for code
├─ Try: GPT-4o → Available ✅
├─ Return: Well-structured regex + validation function
    ↓
Response: ~300 tokens, $0.0006, ~2s
```

### Cost Impact

| Approach | Model | Cost | Time |
|----------|-------|------|------|
| Direct (no routing) | Opus | $0.015 | 4s |
| Routed (this example) | GPT-4o | $0.0006 | 2s |
| **Savings** | - | **96%** | **50%** |

### Decision Logic

Even though this is "code generation", it's a well-known pattern:
- Email validation regex is established knowledge
- GPT-4o is excellent at this level of code
- No need for Opus's deep reasoning
- Routed to cheaper model ✅

---

## Example 5: Budget Pressure Effect

**Prompt**: Same as Example 2 (async debugging), but with high budget pressure

### Scenario: User has hit 85% of weekly Claude subscription

```
Input Text: "I have an async function that times out..."
    ↓
Classification: MODERATE (87% confidence) — same as before
    ↓
Route Decision
├─ Tool: llm_analyze
├─ Complexity: moderate
├─ Budget pressure: 0.85 ⚠️ (WARNING: very high)
    ↓
Budget-Aware Selection
├─ Normal chain: Ollama → GPT-4o → Sonnet
├─ Under pressure: Try cheaper models first
├─ Pressure-aware reordering:
│   ├─ Try: Ollama (free) → Available ✅
│   └─ Return: Local 3-5 min response
    ↓
Response: ~400 tokens, $0.00 (free), ~4 min
```

### Cost Impact with Budget Pressure

| Scenario | Model | Cost | Time | Trade-off |
|----------|-------|------|------|-----------|
| Normal budget | Sonnet | $0.003 | 3s | Best quality/cost |
| High pressure (85%) | Ollama | $0.00 | 4m | Slower but free ✅ |
| **Savings** | - | **100%** | - | Spend saved for other tasks |

### When Budget Pressure Applies

- Weekly Claude subscription limit approaching
- OpenAI account balance low
- Explicit `--profile=aggressive` mode enabled
- API keys rate-limited

---

## Example 6: Routing with Research

**Prompt**: "What are the latest advances in LLM inference optimization as of 2026?"

### Classification Flow

```
Input Text
    ↓
Type Detection
├─ Keyword: "latest" → Current information required
├─ Topic: "advances" → Research task
    ↓
Route Decision
├─ Tool: llm_research (web search)
├─ Requires: Real-time data
    ↓
Model Selection
├─ Always uses: Perplexity (web-grounded)
├─ Perplexity fetches latest from web
├─ Returns: Recent papers, benchmarks, news
    ↓
Response: ~1000 tokens, $0.002, ~6s
```

### Why Not a Regular Query?

| Tool | Approach | Result |
|------|----------|--------|
| `llm_query` | Uses only training data (cutoff Feb 2025) | Outdated ❌ |
| `llm_research` | Web search + synthesis | Current info ✅ |

**Key insight**: Routing detects when web access is needed and routes automatically to Perplexity.

---

## Batch Processing Example

**Scenario**: Process 10 related prompts in sequence

```
Batch: 10 user questions over 30 minutes
    ↓
Task 1: "What is REST?" (simple)
├─ Route: Haiku
├─ Cost: $0.00001
└─ Time: 0.5s

Task 2: "Why is REST popular?" (simple)
├─ Route: Haiku
├─ Cost: $0.00001
└─ Time: 0.4s

Task 3: "Design REST API for e-commerce" (moderate)
├─ Route: Sonnet
├─ Cost: $0.003
└─ Time: 2s

Task 4: "Implement async request handling in REST" (moderate)
├─ Route: Sonnet
├─ Cost: $0.003
└─ Time: 2s

Task 5: "Build distributed REST API gateway with load balancing" (complex)
├─ Route: Opus
├─ Cost: $0.015
└─ Time: 5s

... (5 more tasks follow similar pattern)

Batch Total
├─ Always-Opus cost: $0.15 (10 × $0.015)
├─ Smart routed cost: $0.0450 (mixture of models)
├─ Savings: $0.105 (70%)
└─ Total time: ~25s (vs 50s for Opus batch)
```

### Savings Calculation

```
Simple tasks (4):    4 × $0.00001 = $0.00004
Moderate tasks (4):  4 × $0.003   = $0.012
Complex tasks (2):   2 × $0.015   = $0.03
─────────────────────────────────────
Total routed:        $0.0450
Always-Opus:         $0.15
─────────────────────────────────────
Savings:             $0.105 (70%)
```

---

## Classification Confidence Levels

### High Confidence (95%+) — Skip further checks

```
Input: "what time is it?"
├─ Heuristic: "what is" + time query
├─ Confidence: 99%
├─ Action: Route immediately to Haiku
└─ Latency: instant
```

### Medium Confidence (75–95%) — Use Ollama classifier

```
Input: "how can I optimize my database queries?"
├─ Heuristic: "optimize" + "database"
├─ Confidence: 82%
├─ Action: Refine with Ollama (qwen3.5)
└─ Latency: ~1s extra classification
```

### Low Confidence (<75%) — Full LLM classification

```
Input: "Can you tell me about the history of that thing?"
├─ Heuristic: ambiguous/vague
├─ Confidence: 62%
├─ Action: Use Gemini Flash to classify
├─ Cost: ~$0.0001
└─ Latency: ~2–3s classification
```

---

## Common Misconceptions

### ❌ "My prompt seems simple, but got routed to Sonnet"

**Reason 1: Context matters**
```
Input: "Debug this function"
├─ Without context: simple? (maybe)
├─ With context: complex code + specific error = moderate
└─ Router sees full context, not just prompt
```

**Reason 2: Budget pressure**
```
Input: "What is X?" (normally → Haiku)
├─ User budget: 95% of weekly limit
├─ Router upgrades to balance cost/speed
├─ Result: Sonnet (pays token tax, saves budget resets)
└─ Outcome: Longer-term savings
```

### ❌ "Why didn't Opus answer this?"

**Opus is not always best** — it's only used when:
- Task truly needs expert reasoning (architecture, algorithms)
- Budget pressure is low
- Complexity is high

For most tasks, Sonnet/GPT-4o are 95% as capable at 20% the cost.

### ✅ "This looks cheap/expensive — is routing working?"

**Examples of correct routing**:
- 1-word factual question → Haiku ($0.00001) ✅ Cheap and fast
- Debugging async code → Sonnet ($0.003) ✅ Good balance
- Design consensus algorithm → Opus ($0.015) ✅ Worth the cost
- 10 mixed tasks → $0.045 (vs $0.15) ✅ 70% savings

---

## How to Check Your Routing

```bash
# See the last routing decision
llm-router last

# See full routing history
llm-router snapshot

# See routed vs actual cost
llm-router gain

# Open live dashboard
llm-router dashboard
```

---

## Next Steps

- **[TOOL_SELECTION_GUIDE.md](TOOL_SELECTION_GUIDE.md)** — Detailed reference for all 48 MCP tools
- **[HOST_SUPPORT_MATRIX.md](HOST_SUPPORT_MATRIX.md)** — Which routing features work on which hosts
- **[GETTING_STARTED.md](GETTING_STARTED.md)** — 5-minute setup guide
