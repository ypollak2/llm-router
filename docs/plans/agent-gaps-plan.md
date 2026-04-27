# Agent Gaps Analysis & Implementation Plan

## Overview

Current agent support in llm-router is **foundational but incomplete**. Gap 1 (circuit breaker) prevents infinite loops, but gaps 2–7 address reliability, resource safety, decision quality, and observability.

**Completed:**
- Gap 1: Agent loop circuit breaker ✅

**Planned (this document):**
- Gap 2: Agent error recovery & fallback
- Gap 3: Agent resource budgeting (cost/token/time limits)
- Gap 4: Agent output validation & sanitization
- Gap 5: Smart agent vs. MCP tool delegation
- Gap 6: Agent performance monitoring & metrics
- Gap 7: Agent cost attribution & tracking
- Gap 8: Cross-session context persistence

---

## Gap 2: Agent Error Recovery & Fallback

### Problem

Currently, if an agent fails (times out, OOM, crashes), there's no recovery path. The user sees an error; no fallback to MCP tools.

**Scenarios:**
1. Agent times out after 2 min → error, user repeats manually
2. Agent hits memory limit → error, no retry with different strategy
3. Agent returns unparseable JSON → error, no sanitization attempt

### Solution

**Add agent error handler to agent-route.py (PreToolUse hook)**

When agent-route.py approves an agent call, log the approval in a temp file. When Claude Code reports an agent failure (via new PostToolUse[Agent] hook), read the log and decide:
- **Retrieval failure** → suggest `Read/Grep/Glob` instead
- **Reasoning failure** → route to `llm_*` MCP tool directly
- **Resource limit** → suggest breaking task into smaller chunks

### Implementation

| Phase | Task | Files | Effort |
|-------|------|-------|--------|
| 2a | Add agent call tracking to agent-route.py | hooks/agent-route.py | +30 lines |
| 2b | Create agent-error.py PostToolUse[Agent] hook | hooks/agent-error.py | +80 lines |
| 2c | Implement fallback logic (retrieval→tools, reasoning→MCP) | hooks/agent-error.py | +60 lines |
| 2d | Write comprehensive tests | tests/test_agent_error_hook.py | +200 lines |

**Total: ~370 lines, ~4–6 hours**

### Key Design Decisions

1. **Error tracking via temp file** (`~/.llm-router/agent_calls.json`) — survives hook restarts
2. **Fallback is suggesti­on, not automatic** — user chooses whether to accept recommendation
3. **Explore agents exempt** — pure retrieval rarely fails, low value to track
4. **Block reasoning-to-MCP fallback at depth limit** — respect circuit breaker

### Test Coverage

- Agent failure → correct suggestion (retrieval vs. reasoning vs. resource)
- Session reset → error log cleared
- Multiple failures → maintain history for pattern detection
- Explore agent failure → no suggestion (N/A)

---

## Gap 3: Agent Resource Budgeting

### Problem

Circuit breaker prevents infinite loops, but doesn't limit:
- **Cost per agent**: Expensive agents can consume entire session budget
- **Tokens per agent**: Single agent might use 500k tokens
- **Time per agent**: Long-running agents block user interaction

**Current state:** No per-agent limits. An agent costing $50 is approved if depth < max.

### Solution

**Extend agent-route.py with soft + hard resource limits**

```
Agent call requested:
  1. Check session cost so far
  2. Estimate agent cost (complexity × base cost)
  3. If estimated cost > remaining budget → block with reason
  4. If estimated cost > hard limit per agent → block
  5. Otherwise approve + decrement provisional budget
  6. On completion, reconcile actual vs. provisional
```

### Implementation

| Phase | Task | Files | Effort |
|-------|------|-------|--------|
| 3a | Add resource estimation to agent-route.py | hooks/agent-route.py, config.py | +50 lines |
| 3b | Add provisional spend tracking | hooks/agent-route.py, usage tracking | +40 lines |
| 3c | Implement reconciliation (actual vs provisional) | hooks/agent-error.py, cost.py | +80 lines |
| 3d | Write tests for all limit scenarios | tests/test_agent_resource_limits.py | +250 lines |

**Total: ~420 lines, ~5–7 hours**

### Configuration

Add to `config.py`:
```python
llm_router_agent_max_cost: float = 5.0          # per-agent USD limit
llm_router_agent_max_tokens: int = 100_000      # per-agent token limit
llm_router_agent_max_time: int = 300            # per-agent seconds
llm_router_agent_soft_budget_factor: float = 0.2  # warn at 80% of session remaining
```

### Test Coverage

- Soft limit (warn): agent cost 80% of remaining → approved with warning
- Hard limit (block): agent cost > remaining → blocked
- Reconciliation: provisional deducted, actual reconciled on completion
- Invalid config: negative limits default to unlimited
- Session tracking: limits persist across agent calls in same session

---

## Gap 4: Agent Output Validation & Sanitization

### Problem

Agents return unstructured text. No validation that output is:
- **Well-formed** (JSON, expected schema)
- **Safe** (no injected prompts, no sensitive data leakage)
- **Useful** (not hallucinations, not empty)

**Risk:** Agent returns `{"api_key": "sk-..."}` or `<image of sensitive doc>` — no sanitization.

### Solution

**Add output validator in PostToolUse[Agent] hook**

```
Agent returned output → validate:
  1. Content length reasonable (not empty, not >10MB)
  2. No leaked secrets (API keys, tokens, passwords)
  3. No obviously malicious content (SQL injection patterns, etc.)
  4. For structured tasks: schema validation (JSON, expected fields)
  5. Confidence check: request re-run if low confidence
```

### Implementation

| Phase | Task | Files | Effort |
|-------|------|-------|--------|
| 4a | Add secret detection to agent output | hooks/agent-output.py | +100 lines |
| 4b | Add schema validation framework | hooks/agent-output.py, config.py | +80 lines |
| 4c | Add confidence heuristics | hooks/agent-output.py | +60 lines |
| 4d | Write comprehensive tests | tests/test_agent_output_validation.py | +300 lines |

**Total: ~540 lines, ~6–8 hours**

### Key Design Decisions

1. **Validation is soft** — issues logged + reported to user, not blocking
2. **Secret detection uses regex + entropy checks** — catches accidental leaks
3. **Schema validation opt-in** — users specify expected output format via task prompt hints
4. **Confidence threshold configurable** — sensitivity vs. false positives

### Test Coverage

- Secret detection: finds API keys, tokens, passwords
- Content length: rejects empty, rejects >10MB
- Schema validation: enforces required JSON fields
- Hallucination detection: catches nonsense outputs
- Safe pass-through: benign outputs unmodified

---

## Gap 5: Smart Agent vs. MCP Tool Delegation

### Problem

Current `agent-route.py` uses heuristics to decide: retrieval → agent, reasoning → MCP tool.

**Limitations:**
- Heuristics miss edge cases (mixed tasks, implicit reasoning in retrieval)
- No learning from past decisions (good agents vs. poor MCP routes)
- No cost-based decision (cheap agent might be better than expensive MCP tool)

**Example:**
- "Find all security issues in code" → classified as retrieval, approves agent
- But agent might spend 1 hour analyzing; MCP tool returns same in 2 minutes for 1/100 cost

### Solution

**Add learned decision model to agent-route.py**

```
Decision flow:
  1. Heuristic classification (fast, free)
  2. If confidence < threshold, use model-based classifier
  3. Model trained on past decisions: (prompt, decision, outcome, cost, quality)
  4. Return recommendation + confidence
  5. Log decision + outcome for future training
```

### Implementation

| Phase | Task | Files | Effort |
|-------|------|-------|--------|
| 5a | Add decision logging to hooks | hooks/agent-route.py, cost.py | +40 lines |
| 5b | Build offline classifier trainer | tools/train_agent_classifier.py | +150 lines |
| 5c | Integrate classifier into agent-route.py | hooks/agent-route.py | +100 lines |
| 5d | Add feedback loop for learning | hooks/agent-error.py, cost.py | +80 lines |
| 5e | Write tests + training validation | tests/test_agent_classifier.py | +250 lines |

**Total: ~620 lines, ~7–9 hours**

### Key Design Decisions

1. **Heuristic first** — fast path, no model overhead for clear decisions
2. **Model is optional** — works without ML infrastructure (offline training)
3. **Features**: prompt length, keywords, historical cost/quality
4. **Training: weekly batch** — not real-time, no production latency
5. **Fallback: use heuristic** — if model unavailable

### Test Coverage

- Heuristic decisions: consistent with current behavior
- Model decisions: improve on heuristic for ambiguous cases
- Confidence calibration: high confidence = accurate
- Learning: past decisions inform future ones
- Cold start: works with no history

---

## Gap 6: Agent Performance Monitoring & Metrics

### Problem

No visibility into agent operations:
- How long do agents take?
- What % succeed vs. fail?
- Which agent types are most useful?
- How do agents compare to MCP tools on same task?

**Current:** Minimal logging, no dashboards, no alerts.

### Solution

**Add agent metrics to cost.py + dashboard**

```sql
CREATE TABLE agent_operations (
  id INTEGER PRIMARY KEY,
  session_id TEXT,
  timestamp REAL,
  agent_type TEXT,
  task_category TEXT,
  duration_seconds INTEGER,
  success BOOLEAN,
  cost_usd REAL,
  tokens_used INTEGER,
  quality_score REAL,  -- user feedback later
  depth INTEGER,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
```

Dashboard shows:
- Success rate by agent type
- Avg cost/tokens per agent type
- Execution time distribution
- Comparison: agent vs. MCP tool for same task category

### Implementation

| Phase | Task | Files | Effort |
|-------|------|-------|--------|
| 6a | Add agent_operations table + schema | cost.py | +40 lines |
| 6b | Log agent operations in hooks | hooks/agent-route.py, agent-error.py | +60 lines |
| 6c | Add dashboard endpoints | dashboard/server.py | +150 lines |
| 6d | Add metrics aggregation queries | cost.py | +100 lines |
| 6e | Write tests | tests/test_agent_metrics.py | +200 lines |

**Total: ~550 lines, ~6–8 hours**

### Key Design Decisions

1. **Schema extensible** — quality_score reserved for future user feedback
2. **Metrics real-time** — written to DB immediately, no buffering
3. **Dashboard read-only** — no modification of historical data
4. **Retention: 90 days** — old metrics archived then deleted

### Test Coverage

- Metrics captured for all agent operations
- Success/failure tracked correctly
- Cost/tokens accurate
- Dashboard queries correct
- Data retention policies enforced

---

## Gap 7: Agent Cost Attribution & Tracking

### Problem

Session spend is attributed to the user, not broken down by:
- Direct user tool calls vs. agent spawned calls
- Cost of agent failures (retries, recovery)
- Cost comparison: agent vs. MCP tool for same task

**Result:** User sees total cost, not where it went.

### Solution

**Add cost attribution hierarchy to cost.py**

```
Session (total)
  ├─ Direct user calls (user tools + direct MCP)
  └─ Agent operations
     ├─ Agent A (success, cost=$5)
     ├─ Agent B (failed→MCP fallback, agent cost=$2 + fallback $3 = $5)
     └─ Agent C (cost=$10)
```

Display in dashboard + session-end summary.

### Implementation

| Phase | Task | Files | Effort |
|-------|------|-------|--------|
| 7a | Add cost_source column to usage table | cost.py | +20 lines |
| 7b | Track agent call chain (caller → agent → fallback) | hooks/agent-route.py, cost.py | +80 lines |
| 7c | Add attribution queries | cost.py | +100 lines |
| 7d | Update dashboard to show attribution | dashboard/server.py | +120 lines |
| 7e | Update session-end summary | hooks/session-end.py | +60 lines |
| 7f | Write tests | tests/test_agent_cost_attribution.py | +200 lines |

**Total: ~580 lines, ~6–8 hours**

### Test Coverage

- Direct calls attributed correctly
- Agent operations attributed to agent
- Fallback chain tracked (agent → MCP)
- Cost rollup accurate
- Dashboard display correct

---

## Gap 8: Cross-Session Context Persistence

### Problem

Circuit breaker resets depth per session, but agent state is lost between sessions:
- "Agent A researched topic X in session 1"
- "In session 2, agent needs same research" → starts over

**Current:** Each session is isolated. No context carryover.

### Solution

**Add optional session context carryover**

```
Session start:
  1. Read agent_context from ~/.llm-router/agent_context.json
  2. If same user + same project, inject context into agent startup hook
  3. Agent can reference prior work ("As we discussed in the previous session...")
  4. Session end: save context for next session
```

**Gated by config:** `llm_router_agent_context_persistence: bool = false` (opt-in)

### Implementation

| Phase | Task | Files | Effort |
|-------|------|-------|--------|
| 8a | Add context file schema | config.py, state.py | +40 lines |
| 8b | Implement context save on session-end | hooks/session-end.py | +80 lines |
| 8c | Implement context load on session-start | hooks/session-start.py | +100 lines |
| 8d | Add context injection to subagent-start hook | hooks/subagent-start.py | +60 lines |
| 8e | Add cleanup/retention policies | state.py | +50 lines |
| 8f | Write tests | tests/test_agent_context_persistence.py | +200 lines |

**Total: ~530 lines, ~6–7 hours**

### Key Design Decisions

1. **Opt-in** — off by default to avoid context pollution
2. **Per-project** — context isolated by git remote + repo path
3. **Versioned** — old context dropped if schema changes
4. **Size-limited** — max 5KB per context, max 3 sessions retained
5. **User-deletable** — rm ~/.llm-router/agent_context.json to reset

### Test Coverage

- Context saved on session-end
- Context loaded on session-start
- Context injected into agent startup
- Cleanup policies enforced
- Project isolation working
- Size limits respected

---

## Implementation Roadmap

### Priority 1 (Critical for safety)
- **Gap 2: Error Recovery** — fallback prevents agent failures from blocking user
- **Gap 3: Resource Budgeting** — prevents runaway costs
- **Gap 4: Output Validation** — prevents secret leaks

### Priority 2 (Quality improvements)
- **Gap 5: Smart Delegation** — learns which decisions are best
- **Gap 6: Agent Metrics** — observability, debugging
- **Gap 7: Cost Attribution** — transparency

### Priority 3 (Nice-to-have)
- **Gap 8: Context Persistence** — improves multi-session UX

### Sequencing

```
Phase 1 (Week 1):
  Gap 2 (error recovery)
  Gap 3 (resource budgeting)
  Gap 4 (output validation)

Phase 2 (Week 2):
  Gap 5 (smart delegation)
  Gap 6 (agent metrics)
  Gap 7 (cost attribution)

Phase 3 (Week 3):
  Gap 8 (context persistence)
  Integration testing
  Documentation
```

---

## Scope Summary

| Gap | Title | Lines | Hours | Priority |
|-----|-------|-------|-------|----------|
| 2 | Error Recovery | 370 | 4–6 | P1 |
| 3 | Resource Budgeting | 420 | 5–7 | P1 |
| 4 | Output Validation | 540 | 6–8 | P1 |
| 5 | Smart Delegation | 620 | 7–9 | P2 |
| 6 | Agent Metrics | 550 | 6–8 | P2 |
| 7 | Cost Attribution | 580 | 6–8 | P2 |
| 8 | Context Persistence | 530 | 6–7 | P3 |
| **Total** | | **4,210** | **46–53 hours** | |

**Breakdown:**
- Implementation: ~3,000 lines
- Tests: ~1,200 lines
- Total: ~4,210 lines (equivalent to ~2–3 weeks at 40 hours/week)

---

## Success Criteria

All gaps complete when:

✅ **Gap 2**: Agent failures suggest fallback path (retrieval or MCP)
✅ **Gap 3**: Agent calls blocked if cost > remaining budget
✅ **Gap 4**: Secret detection catches leaked API keys
✅ **Gap 5**: Model-based decisions outperform heuristics
✅ **Gap 6**: Dashboard shows agent metrics by type
✅ **Gap 7**: Session-end summary breaks down agent cost
✅ **Gap 8**: Context persists across sessions when enabled

---

## Questions for Clarification

1. Should Gap 3 hard limits be configurable per-user, per-session, or global?
2. Should Gap 4 validation be synchronous (blocks agent output) or async (logs only)?
3. Should Gap 5 classifier use simple heuristics or full ML?
4. Should Gap 6 metrics include user quality ratings, or just cost/time?
5. Should Gap 7 show agent vs. MCP comparison in cost breakdown?
6. Should Gap 8 context include full conversation history or just summaries?

