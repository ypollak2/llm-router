# Plan: Gap 2 — Agent Error Recovery & Fallback

## Context

**Gap 1 (Circuit Breaker) Status**: ✅ Complete
- Depth guard prevents infinite agent loops
- Per-session tracking with state file
- All tests passing

**Gap 2 Objective**: Add agent error handler to suggest fallbacks when agents fail
- If retrieval agent fails → suggest `Read/Grep/Glob`
- If reasoning agent fails → suggest `llm_*` MCP tools directly
- If resource limit → suggest breaking task into chunks

---

## Problem Statement

Currently, if an agent fails (timeout, OOM, unparseable output), there's no recovery path:
1. User sees raw error
2. No automatic fallback offered
3. User must manually retry or switch strategies

**Scenarios:**
- Agent times out after 2 min → error, user repeats manually
- Agent hits memory limit → error, no recovery suggestion
- Agent returns corrupted output → error, no sanitization

**Goal**: Graceful degradation with intelligent fallback suggestions based on failure type.

---

## Architecture Overview

### Current Hook Flow
1. `PreToolUse[Agent]` (agent-route.py) → approve/block decision
2. Claude Code runs agent
3. Agent returns output (success/failure)
4. **[GAP]** No hook to intercept failures
5. User sees raw error

### With Gap 2 Solution
1. `PreToolUse[Agent]` → **logs approval to `agent_calls.json`** + approve/block
2. Claude Code runs agent
3. Agent fails/times out/returns bad output
4. **`PostToolUse[Agent]` (agent-error.py)** → reads log, classifies failure type, suggests fallback
5. User sees helpful suggestion: "Try using Read/Grep/Glob instead"

---

## Implementation Plan

### Phase 2a: Add Call Tracking to agent-route.py (+30 lines)

**Location**: `src/llm_router/hooks/agent-route.py`

**Changes**:

Add helper function (after existing helpers):
```python
def _log_agent_call(subagent_type: str, prompt: str, decision: str) -> None:
    """Log agent call for error recovery tracking."""
    calls_file = Path.home() / ".llm-router" / "agent_calls.json"
    
    # Read existing history
    history = []
    try:
        data = json.loads(calls_file.read_text())
        history = data.get("calls", [])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    
    # Append new call
    history.append({
        "timestamp": time.time(),
        "subagent_type": subagent_type,
        "prompt": prompt[:500],  # Truncate long prompts
        "decision": decision,
        "session_id": _get_session_id(),
    })
    
    # Keep last 50 calls only
    history = history[-50:]
    
    calls_file.write_text(json.dumps({
        "calls": history,
        "version": 1,
    }))
```

**In `main()`, after depth check approval**:
```python
    # Log call for error recovery tracking
    _log_agent_call(subagent_type, prompt, "approved")
    
    # Continue with existing logic...
```

**Test**: Verify agent_calls.json written correctly on agent approval.

---

### Phase 2b: Create agent-error.py PostToolUse[Agent] Hook (+80 lines)

**Location**: `src/llm_router/hooks/agent-error.py` (new file)

**Structure**:
```python
#!/usr/bin/env python3
"""PostToolUse[Agent] hook — intercept agent failures and suggest fallbacks."""

import json
import sys
import time
from pathlib import Path


def _read_last_agent_call() -> dict | None:
    """Get the most recent agent call from history."""
    calls_file = Path.home() / ".llm-router" / "agent_calls.json"
    try:
        data = json.loads(calls_file.read_text())
        calls = data.get("calls", [])
        return calls[-1] if calls else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _classify_failure(error_output: str) -> str:
    """Classify failure type from error message."""
    lower_err = error_output.lower()
    
    # Resource limits
    if any(x in lower_err for x in ["timeout", "timed out", "exceeded", "limit"]):
        return "resource_limit"
    
    # Unparseable output
    if any(x in lower_err for x in ["json", "parse", "invalid", "syntax"]):
        return "unparseable_output"
    
    # Memory/crash
    if any(x in lower_err for x in ["memory", "oom", "segfault", "killed"]):
        return "resource_crash"
    
    # Generic failure
    return "unknown_failure"


def _get_fallback_suggestion(last_call: dict, failure_type: str) -> str | None:
    """Generate fallback suggestion based on agent type and failure."""
    if last_call is None:
        return None
    
    subagent_type = last_call.get("subagent_type", "").lower()
    prompt = last_call.get("prompt", "")
    
    # Explore agents rarely fail; no suggestion
    if subagent_type == "explore":
        return None
    
    # Retrieval task failed → suggest Read/Grep/Glob
    if _is_retrieval_intent(prompt):
        return (
            "[llm-router] Agent failed to retrieve data. "
            "Try using Glob (find files), Grep (search content), or Read (get file contents) directly."
        )
    
    # Reasoning task failed → suggest MCP tools
    if failure_type == "resource_limit":
        return (
            "[llm-router] Agent hit resource limit. "
            "Try breaking the task into smaller chunks or use llm_query/llm_analyze MCP tools directly."
        )
    
    if failure_type == "unparseable_output":
        return (
            "[llm-router] Agent output could not be parsed. "
            "This is a bug in the agent. Try llm_query or llm_code MCP tools instead."
        )
    
    # Generic reasoning failure
    return (
        "[llm-router] Agent failed. "
        "Try llm_analyze, llm_code, or llm_query MCP tools directly."
    )


def _is_retrieval_intent(prompt: str) -> bool:
    """Quick heuristic: is this a retrieval task?"""
    retrieval_keywords = [
        "search", "find", "list", "show", "get", "read", "grep",
        "locate", "look for", "where", "which files", "all", "all files"
    ]
    prompt_lower = prompt.lower()
    return any(kw in prompt_lower for kw in retrieval_keywords)


def main() -> None:
    """Handle agent failure via PostToolUse hook."""
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return  # Ignore if can't parse
    
    # Only handle Agent tool failures
    if hook_input.get("tool_name") != "Agent":
        return
    
    # Check if this was a failure
    tool_result = hook_input.get("tool_result")
    if tool_result is None:
        return  # Success, no output
    
    error_message = str(tool_result)
    if not error_message or "error" not in error_message.lower():
        return  # Not an error
    
    # Classify failure and get suggestion
    last_call = _read_last_agent_call()
    failure_type = _classify_failure(error_message)
    suggestion = _get_fallback_suggestion(last_call, failure_type)
    
    if suggestion:
        # Output suggestion to stdout (Claude Code will display it)
        print(json.dumps({
            "suggestion": suggestion,
            "failure_type": failure_type,
            "original_error": error_message[:200],  # Truncate
        }))


if __name__ == "__main__":
    main()
```

**Key Design**:
- Standalone script (no imports from `llm_router`)
- Reads call history from `agent_calls.json`
- Classifies failure type from error string
- Suggests appropriate fallback based on task type + failure
- Output goes to stdout for Claude Code to display

---

### Phase 2c: Implement Fallback Logic (+60 lines)

**Enhancements to agent-error.py**:

1. **Smart retrieval detection** (expand `_is_retrieval_intent`):
   - Keywords: search, find, list, locate, grep, read, get, where, show
   - Patterns: "find X", "list all Y", "search for Z"

2. **Failure type specificity**:
   - Timeout → "break into smaller chunks"
   - Memory/OOM → "simplify query"
   - Parse error → "agent output bug"
   - Generic → "try MCP tool"

3. **Context from last call**:
   - Preserve agent type (retrieval vs. reasoning)
   - Preserve prompt to infer task category
   - Track failure patterns (same prompt failing repeatedly?)

4. **Prevent circular suggestions**:
   - If user already tried fallback and it failed → different suggestion
   - Don't suggest Read if user said "Read timed out"

---

### Phase 2d: Write Comprehensive Tests (+200 lines)

**Location**: `tests/test_agent_error_hook.py` (new file)

**Test Classes**:

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestErrorClassification` | 5 | Timeout, OOM, parse error, memory, unknown |
| `TestRetrievalFallback` | 4 | Find files, search content, list items, locate |
| `TestReasoningFallback` | 3 | Analyze, implement, design failures |
| `TestResourceLimitFallback` | 3 | Timeout, OOM, limit |
| `TestExploreExemption` | 2 | Explore agents don't get suggestions |
| `TestCallTracking` | 4 | Log written, history maintained, session reset |
| `TestEdgeCases` | 3 | Missing file, malformed JSON, success (no suggestion) |

**Total: 24 tests, comprehensive coverage**

**Pattern** (subprocess invocation like Gap 1 tests):
```python
def _run(hook_input_dict: dict, tmp_path: Path) -> dict | None:
    """Run agent-error.py hook with given input."""
    payload = json.dumps(hook_input_dict)
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, HOOK_PATH],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.stdout.strip():
        return json.loads(result.stdout)
    return None
```

---

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `src/llm_router/hooks/agent-route.py` | Add `_log_agent_call()` helper + call in main() | +30 |
| `src/llm_router/hooks/agent-error.py` | New hook file (complete implementation) | +80 |
| `tests/test_agent_error_hook.py` | New test file (24 tests) | +200 |
| `src/llm_router/config.py` | (no changes, reuse existing config) | — |

**Total: ~310 lines**

---

## Hook Registration

For the new `agent-error.py` hook to run, it must be registered in Claude Code. The hook runs on `PostToolUse[Agent]` events.

**Hook location**: `~/.claude/hooks/PostToolUse[Agent].sh`

**Content**:
```bash
#!/bin/bash
python3 /path/to/src/llm_router/hooks/agent-error.py
```

*This will be auto-configured by the install/setup process.*

---

## Verification

### Unit Tests
```bash
# Run new error hook tests
uv run pytest tests/test_agent_error_hook.py -v

# Run existing tests (regression check)
uv run pytest tests/test_agent_route_hook.py -v
uv run pytest tests/test_subagent_start_hook.py -v

# Full suite
uv run pytest tests/ -q --tb=short
```

### Manual Smoke Tests

1. **Simulated retrieval failure**:
   ```bash
   echo '{
     "hook_event_name": "PostToolUse",
     "tool_name": "Agent",
     "tool_result": "Error: agent timed out after 120s"
   }' | \
   HOME=$(mktemp -d) \
   python3 src/llm_router/hooks/agent-error.py
   ```
   Expected: Suggests breaking into smaller chunks

2. **Simulated parse failure**:
   ```bash
   echo '{
     "hook_event_name": "PostToolUse",
     "tool_name": "Agent",
     "tool_result": "Error: invalid JSON output"
   }' | \
   HOME=$(mktemp -d) \
   python3 src/llm_router/hooks/agent-error.py
   ```
   Expected: Suggests using MCP tools

---

## Success Criteria

✅ Agent failures suggest appropriate fallback:
- Retrieval failures → Read/Grep/Glob
- Reasoning failures → llm_* MCP tools
- Resource limits → break into chunks
- Explore agents → no suggestion

✅ All 24 tests pass with high coverage

✅ No regression in existing tests (Gap 1, subagent tests)

✅ Call history persists and survives hook restarts

✅ Suggestions are helpful and actionable (not generic)

---

## Open Design Questions

1. **Should fallback suggestions be automatic or user-accepted?**
   - Current plan: suggestion only (user chooses)
   - Alternative: auto-fallback to MCP tool (risky)

2. **How far back to track call history?**
   - Current plan: last 50 calls
   - Alternative: per-session only

3. **Should we track user acceptance of suggestions?**
   - For future learning/feedback on fallback quality

4. **Multi-step failures**: If fallback also fails, what happens?
   - Current plan: no special handling (user sees both errors)
   - Alternative: suggest different fallback

---

## Timeline

- **Phase 2a**: Call tracking (~30 min)
- **Phase 2b**: Hook implementation (~45 min)
- **Phase 2c**: Fallback logic refinement (~30 min)
- **Phase 2d**: Tests (~90 min)

**Total: ~3 hours (estimates 4–6 hours in plan)**

