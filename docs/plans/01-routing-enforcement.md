# Plan: Routing Enforcement (TODO #1)

> **Status**: In progress — hook v3 deployed, but 0 recorded decisions. The directive
> is injected but not acted on.

---

## Problem Statement

The quality report shows 0 routing decisions in 7 days. Every prompt is answered by
Claude Opus directly. Sessions hit token limits fast. The router exists but doesn't route.

**Measured impact**: A session with 50 prompts all-Opus ≈ $3–5 in API costs + hits the
Claude subscription session limit. With routing: same 50 prompts ≈ $0.40, no limit pressure.

---

## Why the Current Approach Fails

The hook injects `⚡ MANDATORY ROUTE:` into `contextForAgent`. Claude receives it but:

1. **In-context drift**: Rules in `CLAUDE.md` and `llm-router.md` are large files. In long
   sessions they drift toward the bottom of the context window and carry less weight.
2. **Default instinct wins**: Claude's training makes it answer helpfully from its own
   knowledge. Overriding that requires the instruction to be salient at decision time.
3. **No feedback signal**: Claude gets no signal that self-answering was wrong. No cost
   consequence. No in-session reinforcement.

---

## Implementation Plan

### Step 1 — SessionStart hook (highest leverage, ~1 day)

Create `~/.claude/hooks/llm-router-session-start.py` that fires on `SessionStart` and
injects a compact routing table into the session context before any user message.

```python
#!/usr/bin/env python3
# llm-router-hook-version: 1
import json, sys

BANNER = """
╔══════════════════════════════════════════════════════════╗
║  llm-router ACTIVE                                      ║
║  MANDATORY: call MCP tools for ALL non-file-edit tasks  ║
║  query/simple   → llm_query    (Haiku — 50x cheaper)   ║
║  code/*         → llm_code     (Sonnet — 10x cheaper)  ║
║  research/*     → llm_research (Perplexity — web)      ║
║  generate/*     → llm_generate (Flash — fast + cheap)  ║
║  FORBIDDEN: answering yourself · Agent subagents        ║
╚══════════════════════════════════════════════════════════╝
"""

payload = json.loads(sys.stdin.read())
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "contextForAgent": BANNER.strip()
    }
}))
```

Register in `~/.claude/settings.json` under `hooks.SessionStart`.

**Why this works**: SessionStart fires once per session, injecting the table at position 0
of the context. It's always in the active window, regardless of session length.

---

### Step 2 — Compact directive format (1 day)

Current directive is 4 lines. Reduce to 1 line, max signal density:

```
⚡ ROUTE→llm_query (query/simple, Haiku) | FORBIDDEN: self-answer · Agent · WebSearch
```

Shorter = less likely to be skimmed. All critical info on one scannable line.

---

### Step 3 — In-session usage counter (2 days)

After every `llm_*` tool call succeeds, the server increments a session counter and
the next `llm_check_usage` or `llm_classify` call returns the distribution:

```
Session so far: llm_query×12  llm_code×4  direct×8  |  saved $1.23
```

This gives Claude a visible reward signal for routing correctly, and gives the user
transparency on where tokens are going.

---

### Step 4 — Direct-answer detection (3 days, harder)

Use a `PostToolUse` hook on all non-routing tools. If Claude calls `Read`, `Grep`, `Bash`,
or `WebSearch` on a prompt that had a routing hint injected, emit a warning:

```python
# PostToolUse hook pseudocode
if tool_name in {"WebSearch", "WebFetch"} and session_has_routing_hint:
    warn("⚠ You used WebSearch directly. The routing hint said llm_research. "
         "This burned ~$0.08 in avoidable Opus tokens.")
```

**Caveat**: requires the hook to know what hint was injected in the current turn.
Store hint in a temp file keyed by session ID.

---

### Step 5 — Session-end summary (1 day)

Add `Stop` hook that emits routing distribution when session ends:

```
━━ Session Summary ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  llm_query    ██████████░░░  12 calls  $0.02
  llm_code     ████░░░░░░░░░   4 calls  $0.09
  llm_research ██░░░░░░░░░░░   2 calls  $0.04
  Claude direct████████░░░░░   8 calls  $1.84  ← avoidable
  ─────────────────────────────────────────────
  Net saved: $1.23  |  Missed savings: $1.84
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `src/llm_router/hooks/session-start.py` | New SessionStart hook with routing banner |
| `src/llm_router/hooks/session-end.py` | New Stop hook with session distribution |
| `src/llm_router/hooks/auto-route.py` | Compact directive to single line |
| `src/llm_router/server.py` | In-session counter per session_id |
| `src/llm_router/install_hooks.py` | Register new hooks in settings.json |

---

## Success Metric

`llm_quality_report` shows ≥ 60% of prompts routed through MCP tools within 7 days of
deploying these changes.
