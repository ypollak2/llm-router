# LLM Router Routing Lineage Tracking — Usage Guide

Comprehensive documentation for tracking and auditing every routing decision, ensuring LLM Router never wastes tokens on expensive model operations.

## Overview

LLM Router's lineage tracking provides **deep observability** into the routing system:

- **Decision Logging**: Every model selection is captured with context
- **Dual Storage**: JSONL (real-time) + SQLite (analytics)
- **Waste Detection**: Automatically identifies expensive models used for simple tasks
- **Request Tracing**: Link nested operations to understand decision chains
- **Reporting**: Human-readable insights and optimization opportunities

## Quick Start

```python
from llm_router.lineage import log_routing_decision, generate_routing_report
from llm_router.lineage.lineage_query import LineageQuery
from llm_router.lineage.lineage_store import LineageStore

# Log a routing decision
decision = log_routing_decision(
    operation="get_cap",              # What was performed
    classification="query/simple",    # Task complexity
    selected_model="gemini-2.5-flash", # Model used
    selection_reason="router_picked",  # Why chosen
    input_tokens=50,
    output_tokens=30,
    cost_usd=0.00008,
    latency_ms=145.3
)

# Query lineage data
store = LineageStore()
query = LineageQuery(store)

# Find wasteful operations (expensive models on simple tasks)
wasteful = query.find_wasteful_operations()

# Generate report
report = generate_routing_report(store)
print(report)
```

## Data Structures

### RoutingDecision

Immutable record of a single routing decision:

```python
@dataclass(frozen=True)
class RoutingDecision:
    decision_id: str              # Unique ID (UUID)
    operation: str                # e.g., "get_cap", "llm_code"
    classification: str           # e.g., "query/simple", "code/complex"
    selected_model: str           # e.g., "claude-haiku-4-5"
    selection_reason: str         # e.g., "router_picked", "fallback_after_ollama"
    
    # Token accounting
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    
    # Performance
    latency_ms: float
    routing_overhead_ms: float    # Time spent on routing decision
    
    # Fallback chain (if applicable)
    fallback_chain: list[str]     # ["ollama", "codex"]
    fallback_reason: str | None   # "ollama_timeout"
    
    # Tracing
    request_id: str               # Link to parent request
    parent_decision_id: str       # Link to parent decision
    metadata: dict[str, Any]      # Custom context
```

## Logging API

### `log_routing_decision()`

Log a routing decision to lineage store.

**Parameters:**
- `operation` (str): What was performed (e.g., "get_cap", "validate_config")
- `classification` (str): Task complexity (e.g., "query/simple", "code/complex")
- `selected_model` (str): Model used (e.g., "gemini-2.5-flash")
- `selection_reason` (str): Why selected (e.g., "router_picked", "fallback_after_timeout")
- `input_tokens` (int): Input token count
- `output_tokens` (int): Output token count
- `cost_usd` (float): Cost in USD
- `latency_ms` (float): Total latency in milliseconds
- `routing_overhead_ms` (float): Time spent on routing decision
- `fallback_chain` (list[str]): Models tried before success
- `fallback_reason` (str): Why fallback occurred
- `request_id` (str): Link to parent request (for tracing)
- `parent_decision_id` (str): Link to parent decision (for nested operations)
- `metadata` (dict): Custom context

**Returns:** `RoutingDecision` record that was logged

**Example:**
```python
# Simple operation, routed to cheap model
log_routing_decision(
    operation="get_cap",
    classification="query/simple",
    selected_model="gemini-2.5-flash",
    selection_reason="router_picked",
    input_tokens=50,
    output_tokens=30,
    cost_usd=0.00008,
    latency_ms=145.3,
)

# Fallback scenario
log_routing_decision(
    operation="llm_research",
    classification="research/moderate",
    selected_model="gemini-2.5-flash",
    selection_reason="fallback_after_ollama_timeout",
    fallback_chain=["ollama"],
    fallback_reason="ollama_timeout",
    input_tokens=1200,
    output_tokens=800,
    cost_usd=0.0008,
    latency_ms=4500.0,
)

# Nested operation (validation → check syntax → validate schema)
parent = log_routing_decision(
    operation="validate_config",
    classification="analyze/moderate",
    selected_model="claude-sonnet-4-6",
    selection_reason="router_picked",
    request_id="req-12345",
)

log_routing_decision(
    operation="check_syntax",
    classification="query/simple",
    selected_model="claude-haiku-4-5",
    selection_reason="nested_simple",
    request_id="req-12345",
    parent_decision_id=parent.decision_id,
)
```

## Querying API

### `LineageQuery` Class

Interface for querying and analyzing lineage data.

**Methods:**

#### `get_recent(limit: int = 100) → list[dict]`
Get most recent routing decisions.

```python
query = LineageQuery()
recent = query.get_recent(limit=20)
```

#### `find_wasteful_operations(expensive_models: list[str]) → list[dict]`
Find operations that used expensive models unnecessarily (expensive models on simple tasks).

```python
wasteful = query.find_wasteful_operations(
    expensive_models=["claude-opus-4-7", "claude-sonnet-4-6"]
)
for op in wasteful:
    print(f"{op['operation']} used {op['selected_model']} (${op['cost_usd']:.4f})")
```

#### `get_model_usage_by_operation() → dict[str, dict[str, int]]`
Get model distribution for each operation.

```python
usage = query.get_model_usage_by_operation()
# {"get_cap": {"gemini-2.5-flash": 50, "claude-haiku-4-5": 10}, ...}
```

#### `get_token_usage_by_model() → dict[str, dict]`
Get token and cost breakdown by model.

```python
usage = query.get_token_usage_by_model()
# {
#   "gemini-2.5-flash": {"tokens": 5000, "cost_usd": 0.0025, "operations": 120},
#   "claude-sonnet-4-6": {"tokens": 25000, "cost_usd": 0.25, "operations": 8}
# }
```

#### `trace_decision_chain(request_id: str) → list[dict]`
Trace all decisions in a request (including nested operations).

```python
chain = query.trace_decision_chain("req-12345")
for decision in chain:
    print(f"{decision['operation']} → {decision['selected_model']}")
```

#### `get_fallback_statistics() → dict[str, int]`
Get frequency of fallback scenarios.

```python
fallbacks = query.get_fallback_statistics()
# {"ollama_timeout": 15, "codex_service_error": 3}
```

#### `get_classification_distribution() → dict[str, int]`
Get distribution of task classifications.

```python
dist = query.get_classification_distribution()
# {"query/simple": 500, "code/complex": 25, "analyze/moderate": 100}
```

#### `get_average_latency_by_model() → dict[str, float]`
Get average latency per model.

```python
latencies = query.get_average_latency_by_model()
# {"gemini-2.5-flash": 150.5, "claude-sonnet-4-6": 2340.0}
```

## Reporting

### `generate_routing_report()` → str

Generate comprehensive routing efficiency report.

```python
from llm_router.lineage import generate_routing_report

report = generate_routing_report()
print(report)
```

**Output includes:**
- Token usage by model (with costs)
- Model distribution per operation
- Wasteful operations (expensive models on simple tasks)
- Task classification distribution
- Fallback chain analysis
- Average latency by model
- Recent routing decisions

## Real-World Usage Patterns

### Pattern 1: Detect Token Waste in Production

```python
from llm_router.lineage.lineage_query import LineageQuery

query = LineageQuery()

# Find all simple operations using expensive models
wasteful = query.find_wasteful_operations()

if wasteful:
    print(f"⚠️  Found {len(wasteful)} wasteful operations:")
    
    total_wasted = sum(op['cost_usd'] for op in wasteful)
    efficient_cost = sum(0.00006 for op in wasteful)  # What Haiku would cost
    savings = total_wasted - efficient_cost
    
    print(f"💰 Potential savings: ${savings:.2f}")
    
    # Group by operation type
    by_op = {}
    for op in wasteful:
        if op['operation'] not in by_op:
            by_op[op['operation']] = []
        by_op[op['operation']].append(op)
    
    for op_name, instances in by_op.items():
        print(f"  {op_name}: {len(instances)} instances (${sum(i['cost_usd'] for i in instances):.4f})")
```

### Pattern 2: Monitor Fallback Patterns

```python
query = LineageQuery()

fallbacks = query.get_fallback_statistics()
print("Fallback analysis:")

for reason, count in sorted(fallbacks.items(), key=lambda x: x[1], reverse=True):
    print(f"  {reason}: {count} times")
    
# If ollama timeouts are frequent, may need to increase timeout or reduce fallback preference
if fallbacks.get("ollama_timeout", 0) > 50:
    print("⚠️  High ollama timeout rate — consider increasing timeout or deprioritizing Ollama")
```

### Pattern 3: Trace Complex Requests

```python
query = LineageQuery()

# Trace a specific request that used unexpected tokens
chain = query.trace_decision_chain("req-xyz-123")

print("Request trace:")
for decision in chain:
    indent = "  " if decision['parent_decision_id'] else ""
    print(f"{indent}{decision['operation']} → {decision['selected_model']} ({decision['total_tokens']} tokens)")
```

### Pattern 4: Generate Daily Efficiency Report

```python
from llm_router.lineage import generate_routing_report
import datetime

report = generate_routing_report()

# Save to file
with open(f"lineage_report_{datetime.date.today()}.txt", "w") as f:
    f.write(report)

print("Report saved!")
```

## Data Storage

### JSONL File (`~/.llm-router/routing_lineage.jsonl`)

Append-only log of routing decisions (one JSON object per line).

**Advantages:**
- Real-time readable
- Stream processing friendly
- Easy to tail for recent decisions
- Human-readable

**Example:**
```
{"decision_id": "uuid1", "operation": "get_cap", "classification": "query/simple", ...}
{"decision_id": "uuid2", "operation": "llm_code", "classification": "code/complex", ...}
```

### SQLite Database (`~/.llm-router/routing_lineage.db`)

Relational database with indexes for efficient querying and analytics.

**Tables:**
- `routing_decisions`: All routing decisions with indexed columns

**Advantages:**
- Fast queries
- Aggregations (SUM, AVG, COUNT)
- Time-range queries
- Scales to millions of records

## Best Practices

1. **Always log with context**
   ```python
   log_routing_decision(
       ...,
       request_id="req-123",  # Link to parent request
       metadata={"user_id": "user-456", "feature": "budget_mgmt"}
   )
   ```

2. **Log fallbacks properly**
   ```python
   log_routing_decision(
       ...,
       fallback_chain=["ollama", "codex"],  # Models tried
       fallback_reason="ollama_timeout",     # Why fallback occurred
   )
   ```

3. **Include routing overhead**
   ```python
   import time
   start = time.time()
   decision = router.classify(...)  # Routing decision
   overhead = time.time() - start
   
   log_routing_decision(
       ...,
       routing_overhead_ms=overhead * 1000,
   )
   ```

4. **Review reports regularly**
   - Daily: Check for wasteful patterns
   - Weekly: Analyze cost trends
   - Monthly: Strategic optimization review

## Example: Complete Integration

```python
from llm_router.lineage import log_routing_decision, generate_routing_report
from llm_router.lineage.lineage_query import LineageQuery

# During request processing
try:
    result = route_and_execute(request)
    
    log_routing_decision(
        operation=request.operation,
        classification=request.task_type,
        selected_model=result.model_used,
        selection_reason=result.router_reason,
        input_tokens=result.tokens_in,
        output_tokens=result.tokens_out,
        cost_usd=result.cost,
        latency_ms=result.latency,
        request_id=request.id,
    )
except Exception as e:
    log_routing_decision(
        operation=request.operation,
        classification=request.task_type,
        selected_model="error",
        selection_reason=str(e),
        request_id=request.id,
    )

# Generate report for monitoring
query = LineageQuery()
wasteful = query.find_wasteful_operations()

if wasteful:
    report = generate_routing_report()
    alert(f"Wasteful operations detected!\n{report}")
```

## Troubleshooting

**Q: No data in report?**
- Verify `log_routing_decision()` is being called
- Check file permissions on `~/.llm-router/`
- Ensure store singleton is initialized: `from llm_router.lineage import get_lineage_store`

**Q: SQLite locked error?**
- Queries are read-only, shouldn't lock
- Check for write operations outside append()
- Clear any dangling database connections

**Q: Want to reset lineage?**
```python
import os
from pathlib import Path

router_dir = Path.home() / ".llm-router"
os.remove(router_dir / "routing_lineage.jsonl")
os.remove(router_dir / "routing_lineage.db")
```

## Tests

Run the test suite to verify routing optimality:

```bash
# Test routing decision logging
pytest tests/lineage/test_routing_decisions.py -v

# Test end-to-end scenarios
pytest tests/lineage/test_lineage_integration.py -v

# All lineage tests
pytest tests/lineage/ -v
```

**Tests verify:**
- ✅ Simple operations use cheap models (Haiku/Gemini Flash)
- ✅ Complex operations use appropriate models (Sonnet/Opus)
- ✅ Token accounting is accurate
- ✅ Fallback chains are tracked correctly
- ✅ Request tracing works end-to-end
- ✅ Dual storage (JSONL + SQLite) stays consistent
