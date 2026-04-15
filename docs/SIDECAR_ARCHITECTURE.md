# llm-router Sidecar Service Architecture (v5.3.0+)

## Overview

llm-router now uses a **sidecar service architecture** to solve the deadlock problem where auto-routing classification could block infrastructure tools (Serena, Obsidian, etc.).

**Key principle:** Never block. Route asynchronously. Degrade gracefully.

```
User Prompt
    ↓
[thin HTTP hook] → sidecar service (localhost:7337)
                   (async classification)
    ↓
Routing directive OR allow unconditionally (no blocking)
    ↓
MCP Tool Call
    ↓
[observation-only enforce-route] → logs, never blocks
    ↓
Tool executes (Serena, Bash, etc. always work)
```

## Components

### 1. Service Core (`src/llm_router/service.py`)

**Async FastAPI application** running on `localhost:7337` (configurable via `LLM_ROUTER_SERVICE_PORT`)

#### Endpoints

**POST /classify**
- **Input:** Prompt, session ID, context
- **Output:** Classification decision with confidence level
- **Timeout:** 0.5 seconds (hooks never wait longer)
- **On timeout:** Hook allows unconditionally (graceful degradation)

```python
POST /classify
{
  "prompt": "what does os.path.join do?",
  "session_id": "abc123",
  "context": {}
}

Response:
{
  "task_type": "query",
  "complexity": "simple",
  "confidence": "high",  # high | medium | low
  "route_to": "llm_query",
  "reasoning": "Heuristic match: API documentation lookup",
  "skip_routing": false
}
```

**GET /health**
- **Output:** Service health status, PID
- **Used by:** Service manager to verify service is running

#### Confidence Scoring

Classification runs fast heuristic patterns:

- **High confidence (>7)** → route immediately
  - Clear intent: "what does X do?", "write function", "latest news"
  - Emit MANDATORY ROUTE directive
  
- **Medium confidence (4-7)** → queue for background (future v5.4)
  - Ambiguous: could be Q&A or infrastructure
  - Allow unconditionally now, classify in background
  
- **Low confidence (<4)** → allow unconditionally
  - Completely ambiguous
  - Log for analysis

#### Infrastructure Detection

Automatically **skip routing** for:
- MCP plugin tools: `mcp__plugin_*` (Serena, Obsidian, etc.)
- llm-router tools: `mcp__llm-router__*`
- System operations: `Bash`, `Read`, `Edit`, `Write`, `Agent`, `ToolSearch`

These tools are **never routed** — infrastructure operations, not user prompts.

### 2. Service Manager (`src/llm_router/service_manager.py`)

Manages service lifecycle: start, stop, health checks.

#### Startup (`start_service()`)

Called by `session-start.py` hook:

1. Check if service already running (PID file exists + alive)
2. If running → skip
3. If not → launch service in background:
   ```python
   subprocess.Popen(
       [sys.executable, "service.py"],
       start_new_session=True,  # Detach from parent
   )
   ```
4. Write PID to `~/.llm-router/service.pid`
5. Wait for health check (max 5 seconds)

#### Shutdown (`stop_service()`)

Called by `session-end.py` hook:

1. Read PID from file
2. Send SIGTERM (graceful shutdown)
3. Wait 5 seconds for clean exit
4. If not dead → SIGKILL (force kill)
5. Clean up PID file

#### Health Check (`is_service_ready()`)

Called internally before responding to hooks:
- HTTP GET to `/health` endpoint
- 1-second timeout
- Returns: service running and healthy

### 3. Hook Client (`src/llm_router/hook_client.py`)

Thin wrapper for hooks to call service:

```python
resp = classify_prompt(prompt, session_id, context)
# Returns: dict with classification or None on error
```

### 4. Hooks (Refactored)

#### auto-route.py (NEW: 60 lines, was 1256)

```python
def main():
    hook_input = json.loads(sys.stdin.read())
    prompt = hook_input["prompt"]
    
    # Call sidecar service (0.5s timeout)
    try:
        resp = urllib.request.urlopen(
            f"http://localhost:7337/classify",
            data=json.dumps({"prompt": prompt}).encode(),
            timeout=0.5
        )
        decision = json.loads(resp.read())
    except:
        sys.exit(0)  # Timeout → allow unconditionally
    
    # Only route high-confidence
    if decision["confidence"] != "high":
        sys.exit(0)
    
    # Emit directive
    print(json.dumps({
        "hookSpecificOutput": {
            "contextForAgent": f"⚡ MANDATORY ROUTE: {decision['task_type']} → call {decision['route_to']}()"
        }
    }))
```

**Key properties:**
- Returns immediately (never blocks)
- Calls service async
- Timeout = allow
- Error = allow

#### enforce-route.py (NEW: observation-only)

```python
def main():
    hook_input = json.loads(sys.stdin.read())
    tool_name = hook_input["tool_name"]
    
    # Log tool usage for analytics
    logger.info(f"tool={tool_name}")
    
    # ALWAYS allow (observation mode)
    sys.exit(0)
```

**Key properties:**
- NEVER blocks any tool
- Logs all tool calls for stats
- Serena, Bash, Read always work
- Zero deadlock risk

#### session-start.py (UPDATED)

Added at beginning of `main()`:
```python
try:
    from llm_router.service_manager import start_service
    if not start_service():
        print("⚠️  Could not start routing service")
except Exception as e:
    print(f"⚠️  Service error: {e}")
```

#### session-end.py (UPDATED)

Added at beginning of `main()`:
```python
try:
    from llm_router.service_manager import stop_service
    stop_service()
except Exception:
    pass
```

## Flow Diagram

### Typical Routing Flow (High Confidence)

```
1. User: "what does os.path.join do?"
   ↓
2. session-start hook runs → start_service() → service running on port 7337
   ↓
3. auto-route hook fires (UserPromptSubmit)
   - HTTP POST to localhost:7337/classify (0.5s timeout)
   - Service returns: {confidence: "high", route_to: "llm_query", ...}
   ↓
4. auto-route hook emits MANDATORY ROUTE directive
   - "⚡ MANDATORY ROUTE: query/simple → call llm_query()"
   ↓
5. User calls llm_query(prompt="what does os.path.join do?")
   ↓
6. enforce-route hook fires (PreToolUse)
   - Logs: "tool=llm_query"
   - Returns: allow (never blocks)
   ↓
7. llm_query MCP tool executes and routes to cheap model
```

### Infrastructure Tool Flow (Always Works)

```
1. User: calls Serena.read_file() to read README.md
   ↓
2. enforce-route hook fires (PreToolUse)
   - Logs: "tool=mcp__plugin_serena_serena__read_file"
   - Returns: allow (observation mode, never blocks)
   ↓
3. Serena tool executes successfully
   ↓
4. No routing attempted (infrastructure operation)
```

### Service Unavailable Flow (Graceful Degradation)

```
1. Service crashed or port unavailable
   ↓
2. auto-route hook calls localhost:7337/classify
   - Timeout after 0.5s (service not responding)
   ↓
3. Hook catches timeout exception
   ↓
4. Hook returns: allow (graceful degradation)
   ↓
5. Everything works without routing
   - No performance impact
   - No blocking
   - Just no optimization (pay full model cost)
```

## Benefits

| Problem | Solution |
|---------|----------|
| auto-route hung system | Service async, hook is instant |
| enforce-route blocked Serena | Observation mode, never blocks |
| Deadlock risk | No blocking anywhere |
| Complex hook logic | Service handles classification |
| Difficult to debug | Clear separation of concerns |
| Hard to extend | Service is modular, easy to add endpoints |
| Silent failures | Timeouts are explicit, allow gracefully |

## Configuration

**Environment variables:**

```bash
LLM_ROUTER_SERVICE_PORT=7337     # Service port (default)
LLM_ROUTER_ENFORCE=off           # Optional: disable enforcement (for testing)
```

**Files:**

```
~/.llm-router/
  service.pid          # Running service PID
  service.log          # Service activity logs
  auto-route.log       # Hook activity logs
  enforce.log          # Tool usage logs
```

## Testing

Run sidecar service tests:

```bash
uv run pytest tests/test_sidecar_service.py -xvs
```

Tests cover:
- Module imports
- Heuristic classification
- Infrastructure detection
- Confidence scoring

## Future Enhancements (v5.4+)

1. **Async background classification**
   - Medium-confidence prompts queued for Ollama
   - Results stored for next session
   
2. **Service metrics**
   - GET `/stats` endpoint
   - Routing decisions per task type
   - Confidence distribution
   
3. **Service watchdog**
   - Auto-restart on failure
   - Heartbeat monitoring
   
4. **Performance optimization**
   - Classification result caching
   - Batch classification for multiple prompts

## Troubleshooting

**Service not starting:**
```bash
# Check logs
tail -f ~/.llm-router/service.log

# Check port is available
lsof -i :7337

# Start manually for debugging
python3 -m llm_router.service
```

**Service killed unexpectedly:**
```bash
# Check PID file
cat ~/.llm-router/service.pid

# Verify process
ps aux | grep service.py

# Restart Claude Code (session-start will restart)
```

**Routing not happening:**
- Check auto-route.log for classification attempts
- Verify service is running: `curl http://localhost:7337/health`
- Try manual route: `llm_query(prompt="...")`

