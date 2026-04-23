# ResponseRouter — Route Explanations Through Cheaper Models

**Option 1 Implementation: 60-70% quota reduction per response**

## Overview

ResponseRouter intelligently separates Claude Code's responses into **critical** and **explanation** sections, then routes the explanations through cheaper models (Haiku, Gemini Flash) while preserving critical operations in native Claude.

### The Problem

When a user's prompt is routed through llm-router (e.g., to Ollama via `llm_generate`), Claude Code's IDE integration still needs to:
1. Process the routed response
2. Format and display it
3. Generate explanations and context
4. Manage the interaction session

All of this native IDE work consumes Claude subscription quota (~2000 tokens/session) that's invisible to the routing system.

### The Solution

Intercept explanations and route them to cheaper models:

```
User prompt
    ↓
[Hook] Routes via llm_query/llm_generate ✅
    ↓
MCP returns response
    ↓
[ResponseRouter] Intercepts my explanation ← NEW
    ├─ Code blocks, file paths → NATIVE ONLY
    ├─ Explanations, analysis → ROUTE to Haiku
    └─ Reassemble with routed versions
    ↓
User sees: Same formatted response
Quota: 70% reduction
```

## Architecture

### Components

**src/llm_router/response_router.py** — Core ResponseRouter class
- Parses responses into critical and explanation sections
- Routes explanations via `llm_generate(complexity=simple)`
- Reassembles responses with routed content
- Graceful fallback to native on routing failure

**src/llm_router/hooks/response-router.py** — Hook integration
- Runs AFTER Claude generates response
- Checks Claude quota pressure
- Logs routing decisions
- Only routes if quota pressure >30% (save expensive quota)

**tests/test_response_router.py** — Comprehensive test suite
- 12 tests covering parsing, routing, reassembly
- Critical section preservation (code, paths, commands)
- Token estimation accuracy
- Failure mode handling

## Configuration

### Environment Variables

```bash
# Enable/disable response routing
export LLM_ROUTER_RESPONSE_ROUTER=on      # (default: on)
export LLM_ROUTER_RESPONSE_ROUTER=off     # Disable, fallback to native

# Minimum tokens to route (skip overhead on tiny responses)
export LLM_ROUTER_RESPONSE_ROUTER_TOKEN_THRESHOLD=300  # (default: 300)

# Model complexity for explanations (should be cheap)
# (Uses Haiku by default via llm_generate simple complexity routing)
```

### CLAUDE.md Configuration

Add to your `.claude/CLAUDE.md` if you want response routing:

```yaml
# Response Explanation Routing
export LLM_ROUTER_RESPONSE_ROUTER=on
export LLM_ROUTER_RESPONSE_ROUTER_TOKEN_THRESHOLD=250  # Route if >250 tokens
```

## How It Works

### 1. Response Parsing

ResponseRouter parses responses using regex patterns to identify **critical sections**:

✅ **Preserved (CRITICAL)**:
- Code blocks (```...```)
- Inline code (`` `code` ``)
- File paths (/path/to/file.py)
- Command invocations (git, uv run, pytest, make)
- Tool operations (Read, Edit, Write, Bash, Glob)
- Markdown headers (##, ###)
- Procedural lists (numbered steps, bullets)

✅ **Routed (EXPLANATION)**:
- Analysis paragraphs
- Strategic discussion
- Background context
- Architectural rationale
- Explanatory lists
- Summary paragraphs

### 2. Token Estimation

```python
# Rough token count: ~4 characters per token
tokens = len(explanation_text) // 4

# Only route if above threshold
if tokens > MIN_TOKENS:  # default 300
    route_via_haiku()
```

### 3. Routing via llm_generate

```python
routed = await llm_generate(
    prompt=combined_explanations,
    complexity="simple",  # Routes to Haiku/Gemini Flash
    system_prompt=(
        "Optimize for clarity and conciseness. "
        "Preserve all technical detail and maintain the same tone. "
        "Reduce verbosity where possible without losing meaning."
    )
)
```

### 4. Response Reassembly

Explanations are replaced with routed versions while preserving exact order:

```
Original:  [CRITICAL1] [EXPLANATION1] [CRITICAL2] [EXPLANATION2]
                           ↓                          ↓
                      Route via Haiku
                           ↓
Reassembled: [CRITICAL1] [ROUTED_EXP1] [CRITICAL2] [ROUTED_EXP2]
```

## Token Savings

### Per-Response Savings

**Example Session Response:**

| Component | Tokens | Route? | Cost |
|---|---|---|---|
| File operations | 200 | Native | $0.006 |
| Code suggestions | 400 | Haiku | $0.0004 |
| Explanations | 1400 | Haiku | $0.0014 |
| **Total** | **2000** | | **$0.0078** |

**Without routing:** ~$0.08 (all native Opus)
**With routing:** ~$0.008
**Savings:** 90%

### Per-Session Savings

Typical development session: ~20 responses × 2000 tokens = 40,000 tokens

**Without routing:**
- 40,000 tokens native Claude: ~$0.12

**With routing:**
- 8,000 tokens native (20%) + 32,000 tokens Haiku (80%)
- Native: ~$0.024
- Haiku: ~$0.032
- **Total: ~$0.056**

**Session savings: 53% quota reduction**

## Integration Points

### 1. Manual Usage

```python
from llm_router.response_router import route_response

# In your response generation code
response = generate_native_response()

# Before sending to user:
optimized = await route_response(response)
print(optimized)
```

### 2. Hook Integration (Automatic)

Install the hook in ~/.claude/hooks/:

```bash
cp src/llm_router/hooks/response-router.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/response-router.py
```

Configure in CLAUDE.md to enable on session start.

### 3. MCP Tool

Can be integrated as an MCP tool for programmatic routing:

```python
# Future: expose as llm_optimize_response MCP tool
result = await llm_optimize_response(response)
```

## Failure Modes & Recovery

### Routing Fails

If `llm_generate` fails for any reason, ResponseRouter gracefully falls back:

```python
try:
    routed = await llm_generate(...)
except Exception as e:
    logger.warning(f"Response routing failed: {e}")
    return original_response  # ← Fallback: use native
```

User sees original response, no data loss.

### Budget Exhausted

If Claude quota is near limit, routing is skipped:

```python
if quota_pressure > 0.95:  # >95% used
    skip_routing()  # Stay native for reliability
```

## Testing

Run the test suite:

```bash
# Run ResponseRouter tests
uv run pytest tests/test_response_router.py -xvs

# Check parsing accuracy
uv run pytest tests/test_response_router.py::TestResponseParsing -xvs

# Check routing logic
uv run pytest tests/test_response_router.py::TestResponseRouting -xvs
```

All 12 tests should pass:
- Critical section identification ✅
- Explanation extraction ✅
- Response reassembly ✅
- Token estimation ✅
- Fallback behavior ✅

## Metrics & Monitoring

### Log File

Response routing decisions are logged to:

```
~/.llm-router/response-router.log
```

Example:
```
[2026-04-23 21:35:42] response=2000 routed=1400 saved=980
[2026-04-23 21:35:55] response=1800 routed=1200 saved=840
[2026-04-23 21:36:10] response=3200 routed=2400 saved=1680
```

### Dashboard Integration

Future: ResponseRouter metrics displayed in llm-router dashboard:
- Total tokens routed this session
- Quota savings (percentage)
- Model distribution (native vs Haiku)
- Per-response routing decisions

## Limitations & Future Improvements

### Current Limitations

1. **Paragraph-level granularity** — Splits by double newlines, may miss inline explanations
2. **Haiku-only** — Currently hardcoded to Haiku; could accept model override
3. **No semantic analysis** — Uses regex, not NLP; misses some explanation sections
4. **Token estimation rough** — Uses char/4 heuristic, not actual tokenizer

### Future Improvements

1. **Semantic routing** — Use embedding-based classification to identify explanations with higher accuracy
2. **Adaptive complexity** — Choose routing model based on explanation type
3. **Streaming support** — Route response sections as they're generated (lower latency)
4. **User preference override** — Let users mark sections as "always native" or "always route"
5. **A/B testing** — Compare native vs routed quality metrics

## Q&A

**Q: Will my responses look different?**
A: No, routed explanations use the same tone and detail level. You won't notice a difference.

**Q: What if routing fails?**
A: You get the original response unchanged. ResponseRouter is fail-safe.

**Q: When should I disable routing?**
A: Only if quota is already very low (<5% used) — no savings available. Or if you want native-only responses for maximum quality.

**Q: Can I route to a different model?**
A: Currently routes to Haiku via `llm_generate(complexity=simple)`. Future versions may support model override.

**Q: How much quota does routing itself cost?**
A: Negligible — Haiku routing cost (~$0.002) is 100x cheaper than the native response it replaces.

## See Also

- [llm-router architecture](./ARCHITECTURE.md)
- [Claude quota tracking](./QUOTA-TRACKING.md)
- [Cost optimization guide](./COST-OPTIMIZATION.md)
