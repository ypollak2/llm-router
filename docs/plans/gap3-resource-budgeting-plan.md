# Plan: Gap 3 — Agent Resource Budgeting

## Context

**Gap 1 (Circuit Breaker) Status**: ✅ Complete
**Gap 2 (Error Recovery) Status**: ✅ Complete

**Gap 3 Objective**: Prevent runaway costs by enforcing soft and hard resource limits per agent call
- **Soft limit**: Warn if estimated cost > 80% of remaining budget
- **Hard limit**: Block if estimated cost > remaining session budget or exceeds per-agent max
- **Reconciliation**: Track provisional vs. actual spend, adjust budget on completion

---

## Problem Statement

Currently, circuit breaker prevents infinite loops, but doesn't limit:
- **Cost per agent**: A single expensive agent can consume entire session budget ($50+ in one call)
- **Tokens per agent**: Agent might use 500k tokens in one attempt
- **Time per agent**: Long-running agents (2+ hours) block user interaction

**Scenarios:**
1. User's session has $5 remaining. Agent call estimated at $3 is approved, but actually costs $8 (overrun)
2. Agent classified as "simple" but prompt is actually complex, costs $20 instead of $1
3. User sets max $100/month budget but first 3 agents total $95 — no warning before 4th call

**Goal**: Budget-aware agent routing with visibility into projected costs and hard stops.

---

## Architecture Overview

### Current Flow
```
User wants agent
  ↓
agent-route.py approves/blocks (depth check only)
  ↓
Agent executes
  ↓
Cost charged to session
```

### With Gap 3 Solution
```
User wants agent
  ↓
agent-route.py:
  1. Read session cost so far
  2. Estimate agent cost (complexity × base cost)
  3. Check soft limit (warn if 80% of remaining)
  4. Check hard limit (block if exceeds remaining or agent max)
  5. Approve/block + store provisional budget reservation
  ↓
Agent executes
  ↓
session-end hook (or new PostToolUse):
  1. Read actual cost from usage DB
  2. Compare actual vs. provisional
  3. Adjust session budget accordingly
  ↓
Cost reconciled
```

---

## Implementation Plan

### Phase 3a: Add Resource Estimation (+50 lines)

**Location**: `src/llm_router/hooks/agent-route.py`

**Changes**:

Add estimation helpers after existing helpers:
```python
def _estimate_agent_cost(complexity: str, task_type: str) -> float:
    """Estimate agent call cost in USD based on complexity and task.
    
    Base rates (from historical data):
    - simple/query: $0.10–0.30
    - simple/retrieval: $0.05–0.15
    - moderate/code: $0.50–1.00
    - moderate/analyze: $0.40–0.80
    - complex/reasoning: $2.00–5.00
    
    Returns conservative upper estimate to avoid surprises.
    """
    rates = {
        ("simple", "retrieval"): 0.15,
        ("simple", "query"): 0.30,
        ("simple", "code"): 0.20,
        ("moderate", "retrieval"): 0.30,
        ("moderate", "query"): 0.50,
        ("moderate", "code"): 1.00,
        ("moderate", "analyze"): 0.80,
        ("complex", "code"): 3.00,
        ("complex", "analyze"): 4.00,
        ("complex", "research"): 2.50,
    }
    # Default conservative estimate
    return rates.get((complexity, task_type), 1.50)


def _get_remaining_budget() -> float:
    """Get remaining session budget in USD.
    
    Priority:
    1. ~/.llm-router/session_budget.json (provisional tracking)
    2. Infer from usage.json (session % remaining)
    3. Conservative default $10 (assume 1/3 of typical session)
    """
    # Layer 1: Session budget file
    budget_file = Path.home() / ".llm-router" / "session_budget.json"
    try:
        data = json.loads(budget_file.read_text())
        if "remaining" in data:
            return float(data["remaining"])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    
    # Layer 2: Infer from usage pressure
    session_pct = _get_claude_pressure()  # 0.0–1.0
    # Assume $30 session budget, calculate remaining
    session_budget = 30.0
    spent = session_budget * session_pct
    return max(0.0, session_budget - spent)
```

**Integration in main()**:
```python
    # After reading prompt/complexity/task_type, estimate cost
    estimated_cost = _estimate_agent_cost(complexity, task_type)
    remaining = _get_remaining_budget()
    
    # Log for debugging
    # print(f"[debug] Cost estimate: ${estimated_cost:.2f}, remaining: ${remaining:.2f}", file=sys.stderr)
```

---

### Phase 3b: Add Soft + Hard Limit Checks (+40 lines)

**Location**: `src/llm_router/hooks/agent-route.py`

**Changes**:

Add configuration constants:
```python
# ── Resource limits ──────────────────────────────────────────────────────────
AGENT_MAX_COST_USD = 5.0          # Hard per-agent limit
SESSION_MAX_COST_USD = 50.0       # Hard per-session limit (fallback)
SOFT_BUDGET_FACTOR = 0.8          # Warn if cost > 80% of remaining
```

Add limit checking logic (after cost estimation):
```python
    # ── Soft limit: warn if consuming >80% of remaining budget ────────────────
    soft_limit = remaining * SOFT_BUDGET_FACTOR
    if estimated_cost > soft_limit:
        warning = (
            f"\n  ⚠️  Budget warning: Agent would use ${estimated_cost:.2f} "
            f"(80% of your remaining ${remaining:.2f}). "
            f"Approve risky agent? (proceeding anyway)\n"
        )
        # Log warning but continue (soft limit is advisory)
        # sys.stderr.write(warning)
    
    # ── Hard limit: block if exceeds limits ──────────────────────────────────
    if estimated_cost > remaining:
        result = {
            "decision": "block",
            "reason": (
                f"[llm-router] Agent would exceed session budget.\n"
                f"  Estimated cost: ${estimated_cost:.2f}\n"
                f"  Remaining budget: ${remaining:.2f}\n\n"
                f"Use llm_* MCP tools instead (typically cheaper and bounded)."
            ),
        }
        json.dump(result, sys.stdout)
        return
    
    if estimated_cost > AGENT_MAX_COST_USD:
        result = {
            "decision": "block",
            "reason": (
                f"[llm-router] Agent estimated cost exceeds per-agent limit.\n"
                f"  Estimated: ${estimated_cost:.2f}\n"
                f"  Hard limit: ${AGENT_MAX_COST_USD:.2f}\n\n"
                f"Task is too complex for a single agent. Break it into smaller steps."
            ),
        }
        json.dump(result, sys.stdout)
        return
```

---

### Phase 3c: Implement Budget Reconciliation (+80 lines)

**Location**: `src/llm_router/hooks/agent-error.py` (enhanced) and new `hooks/session-end.py` (or integrate into existing)

**Concept**:

1. **Provisional Budget Tracking** (agent-route.py):
   - When agent approved, reserve `estimated_cost` from session budget
   - Write to `session_budget.json`: `{"remaining": $X, "reserved_agents": [...]}`

2. **Reconciliation** (PostToolUse or session-end):
   - Read actual cost from usage.db (or llm_check_usage API)
   - Compare actual vs. provisional
   - Adjust remaining budget: `remaining -= (actual - provisional)`
   - Release reservation

**Implementation**:

Add to agent-route.py after approval:
```python
def _reserve_provisional_budget(estimated_cost: float, session_id: str) -> None:
    """Reserve budget for this agent call.
    
    Writes to session_budget.json to track provisional spend.
    Actual reconciliation happens in PostToolUse hook.
    """
    budget_file = Path.home() / ".llm-router" / "session_budget.json"
    
    # Read current state
    try:
        state = json.loads(budget_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"remaining": 30.0, "reserved_agents": []}
    
    # Deduct provisional estimate
    state["remaining"] = max(0.0, state["remaining"] - estimated_cost)
    state["reserved_agents"].append({
        "timestamp": time.time(),
        "estimated_cost": estimated_cost,
        "session_id": session_id,
    })
    
    # Keep only last 20 agent reservations
    state["reserved_agents"] = state["reserved_agents"][-20:]
    
    budget_file.write_text(json.dumps(state))
```

Call after approval but before agent execution:
```python
    # Reserve budget provisionally
    _reserve_provisional_budget(estimated_cost, session_id)
    
    sys.exit(0)  # Approve
```

**Reconciliation** (in new function or session-end hook):
```python
def _reconcile_budget() -> None:
    """Reconcile actual vs. provisional agent costs.
    
    Reads actual cost from usage DB, compares to reserved estimate,
    and adjusts remaining budget accordingly.
    
    Called at session end or after agent completion.
    """
    budget_file = Path.home() / ".llm-router" / "session_budget.json"
    
    try:
        state = json.loads(budget_file.read_text())
    except FileNotFoundError:
        return  # No budget tracking yet
    
    # For now, simple approach: on session end, reset budget
    # TODO: integrate with usage.db to get actual costs
    
    # Write reset state for next session
    state["remaining"] = 30.0  # Reset for next session
    state["reserved_agents"] = []
    budget_file.write_text(json.dumps(state))
```

---

### Phase 3d: Write Configuration + Tests (+250 lines)

**Location**: `src/llm_router/config.py` and `tests/test_agent_resource_limits.py`

**Config additions**:
```python
# Agent resource limits
llm_router_agent_max_cost_usd: float = 5.0
llm_router_agent_max_tokens: int = 100_000
llm_router_agent_max_time_seconds: int = 300
llm_router_session_max_cost_usd: float = 50.0
llm_router_soft_budget_warning_factor: float = 0.8  # Warn at 80% of remaining
```

**Test classes** (13 tests):

1. **TestSoftLimitWarning** (3 tests)
   - Cost at 80% of remaining → warning
   - Cost at 50% of remaining → no warning
   - Cost at 90% of remaining → warning

2. **TestHardLimitBlock** (4 tests)
   - Cost > remaining → blocked
   - Cost > agent max → blocked
   - Cost within limits → approved
   - Edge case: remaining exactly equals cost → approved

3. **TestBudgetReconciliation** (3 tests)
   - Provisional deducted on approval
   - Actual reconciled on completion
   - Overage tracked correctly

4. **TestBudgetTracking** (3 tests)
   - Session budget file created
   - Multiple agents tracked
   - Session reset clears budget

---

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `src/llm_router/hooks/agent-route.py` | Add cost estimation + limit checks + provisional reservation | +90 |
| `src/llm_router/hooks/agent-error.py` | Enhance with reconciliation logic (optional, can be session-end hook) | +40 |
| `src/llm_router/config.py` | Add resource limit config fields | +5 |
| `tests/test_agent_resource_limits.py` | New test file with 13 comprehensive tests | +250 |

**Total: ~385 lines**

---

## Hook Registration

No new hooks needed — all logic integrates into existing `PreToolUse[Agent]` hook (agent-route.py).

Optional: Create `session-end.py` hook if reconciliation needs to be deferred:
```bash
~/.claude/hooks/SessionEnd.sh → src/llm_router/hooks/session-end.py
```

---

## Verification

### Unit Tests
```bash
# Run new resource limit tests
uv run pytest tests/test_agent_resource_limits.py -v

# Run existing tests (regression check)
uv run pytest tests/test_agent_route_hook.py tests/test_agent_error_hook.py -v

# Full suite
uv run pytest tests/ -q --tb=short
```

### Manual Smoke Tests

1. **Session budget tracking**:
   ```bash
   # Check that session_budget.json is created and updated
   cat ~/.llm-router/session_budget.json | jq .
   ```

2. **Cost estimation**:
   ```bash
   # Simulate an agent call that exceeds budget
   echo '{
     "hook_event_name": "PreToolUse",
     "tool_name": "Agent",
     "tool_input": {
       "prompt": "analyze entire codebase comprehensively",
       "subagent_type": "general-purpose"
     }
   }' | HOME=$(mktemp -d) python3 src/llm_router/hooks/agent-route.py
   # Should block with cost estimate if budget is low
   ```

3. **Soft limit warning**:
   - Create low remaining budget (e.g., $1)
   - Approve agent with 80% cost
   - Verify logs show warning (if enabled)

---

## Success Criteria

✅ Soft limit warns when agent cost > 80% of remaining budget
✅ Hard limit blocks if cost > remaining budget or agent max
✅ Budget file tracks provisional spend
✅ All 13 tests pass with high coverage
✅ No regression in existing tests (Gap 1, Gap 2)
✅ Configuration fields override defaults correctly

---

## Open Design Questions

1. **What should the base cost rates be?**
   - Current estimate: simple $0.10–0.30, moderate $0.40–1.00, complex $2.00–5.00
   - Should these be configurable or learned from actual usage?

2. **Should soft limit be a warning or a hard stop with confirmation?**
   - Current plan: soft limit is advisory only (log warning, continue)
   - Alternative: ask user before proceeding

3. **How often to reconcile actual vs. provisional?**
   - Current plan: on session end
   - Alternative: immediately after each agent call (requires live cost API)

4. **Should per-agent limit scale with session budget?**
   - Current plan: fixed $5/agent regardless of session budget
   - Alternative: $5 max or 10% of session budget, whichever is smaller

5. **How to handle cost estimation errors?**
   - If estimated $1 but actual $5, should next agent be auto-limited?
   - Should we track estimation accuracy and adjust future estimates?

---

## Timeline

- **Phase 3a**: Cost estimation (~30 min)
- **Phase 3b**: Limit checks (~20 min)
- **Phase 3c**: Budget reconciliation (~45 min)
- **Phase 3d**: Tests + config (~90 min)

**Total: ~3 hours (estimates 5–7 hours in plan)**

---

## Integration with Other Gaps

- **Gap 2 (Error Recovery)**: Resource limit failures already suggest breaking into chunks
- **Gap 4 (Output Validation)**: Validates that output matches expected scope (no secret leaks)
- **Gap 5 (Smart Delegation)**: Uses historical cost/quality to improve routing decisions
- **Gap 6 (Metrics)**: Tracks actual costs per agent for dashboard display

