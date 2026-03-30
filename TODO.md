# TODO — llm-router

> Priority order. #1 is the existential problem. Everything else builds on it.

---

## #1 — CRITICAL: Make the Router Actually Route (Zero Decisions in 7 Days)

**The problem**: The quality report shows 0 routing decisions. Claude Code reads the
`⚡ MANDATORY ROUTE:` hint injected by the hook and then answers directly — from its own
knowledge, or worse, spawns expensive Agent subagents. The MCP tools (`llm_query`, `llm_code`,
`llm_research`, etc.) are never called. Every token is burned on Opus/Sonnet. Sessions hit
limits fast.

**Root cause analysis**:

The hook injects the directive into `contextForAgent` in the UserPromptSubmit response.
Claude Code receives it — but treating injected context as a hard behavioral constraint
requires Claude to *prioritize* that context over its default "answer myself" behavior.
The rules file and CLAUDE.md prohibit self-answering, but prohibitions only work if Claude
reads those files. In long sessions (high context), they drift out of the active window.

**What needs to be built**:

### 1a. Session-start routing announcement (highest leverage)

Add a `SessionStart` hook that injects a compact routing summary at the TOP of every new
session — before any user message. This is always in the active context window.

```
╔══════════════════════════════════════════════════╗
║  llm-router active — 3 routing tiers available  ║
║  query/simple   → llm_query   (Haiku, free)     ║
║  code/moderate  → llm_code    (Sonnet, cheap)   ║
║  research/*     → llm_research (Perplexity)     ║
║  You MUST call these tools. Answering yourself   ║
║  = full Opus cost. Routing = 50–100x cheaper.   ║
╚══════════════════════════════════════════════════╝
```

### 1b. Per-prompt directive must be unmissable

The current `contextForAgent` approach buries the directive among other context.
Explore injecting it as a `system` message prefix (if MCP/hook spec allows) or as the
FIRST line of the human turn so it cannot be skipped.

### 1c. Usage feedback loop in session

After each MCP tool call succeeds, inject a compact "✓ Routed — saved $X vs Opus" line
back into context. This trains in-session behavior and provides the user with visible proof.

### 1d. Fallback detection

If Claude answers a routed prompt directly (no MCP tool called), the
PostToolUse hook should detect it and inject: "⚠ You answered directly. That was $Y in
avoidable Opus tokens. Next time call `llm_query` for simple questions."

### 1e. Session usage display

Show the routing distribution at session end or on `llm check_usage`:
```
This session: 47 prompts
  llm_query    ████████████████░░░░  18 (38%)  — Haiku   — $0.03
  llm_code     ████████░░░░░░░░░░░░   9 (19%)  — Sonnet  — $0.12
  llm_research ████░░░░░░░░░░░░░░░░   5 (11%)  — Perplex — $0.08
  Claude direct ██████████████░░░░░░  15 (32%)  — Opus    — $1.84
  ─────────────────────────────────────────────────
  Saved: $2.07 vs all-Opus  |  Missed: $1.84 (direct answers)
```

---

## #2 — Repo Order

The repo root has accumulated files without clear organization. Clean up:

- [ ] Move `ROADMAP.md` → `docs/ROADMAP.md` (already have `docs/VISION.md`)
- [ ] Move `CONTRIBUTING.md` → `docs/CONTRIBUTING.md`
- [ ] Move `CHANGELOG.md` → `docs/CHANGELOG.md`
- [ ] Delete `.playwright-mcp/` (stray screenshot dir from browser session)
- [ ] Add `docs/` index to README navigation section
- [ ] Evaluate `agents/` and `skills/` dirs — merge into `docs/` or keep separate

---

## #3 — Vision Items (from docs/VISION.md)

### 3a. Adaptive Routing — Learn From Usage
**Goal**: Personalized routing table, not just global benchmarks.
**Plan**: `docs/plans/adaptive-routing.md`
- Log outcome signals per routed call (latency, response length, user re-prompts)
- Weekly Bayesian reweight of model scores per task type
- Prior = Arena Hard benchmarks. Posterior = your usage.
- All data already collected in `routing_decisions` SQLite table.

### 3b. Latency-Aware Model Selection
**Goal**: Factor p95 latency per model as tiebreaker between similar-scoring models.
**Plan**: `docs/plans/latency-routing.md`
- `latency_ms` already logged in `routing_decisions` — just unused
- `adjusted_score = base_score * (1 - failure_penalty) * latency_factor`
- Weight: 10% max — tiebreaker, not primary signal

### 3c. OpenTelemetry Tracing
**Goal**: Full trace ID tying together classify → attempt → fallback → success.
**Plan**: `docs/plans/otel-tracing.md`
- Single `trace_id` per user prompt
- Export to Jaeger/Datadog/OTLP
- Table stakes for anyone running in production

### 3d. Confidence Display
**Goal**: Surface low-confidence classifications to user.
**Plan**: `docs/plans/confidence-display.md`
- `ClassificationResult.confidence` is computed but thrown away
- >85% → route silently
- 60–85% → route + note
- <60% → route + suggest override

### 3e. Provider-Specific Optimization Hooks
**Goal**: Inject per-model params that improve quality/cost.
**Plan**: `docs/plans/provider-hints.md`
- `openai/o3` → `reasoning_effort: medium`
- `perplexity/sonar-pro` → `search_recency_filter: week`
- `gemini/gemini-2.5-pro` → `thinking_budget: 8000`

### 3f. Streaming for All Tools
**Goal**: `stream=True` param on every `llm_*` tool, not just `llm_stream`.
**Plan**: `docs/plans/universal-streaming.md`

### 3g. Post-Response Validation
**Goal**: Detect garbage/empty responses and retry on next model.
**Plan**: `docs/plans/response-validation.md`
- `len(content.strip()) < 10` → retry
- CODE task + no code block + len < 50 → retry
- Failed validation → next model in fallback chain

---

## #4 — Team Mode (First Revenue Feature)

- [ ] `llm-router-server` FastAPI mode — shared config + usage DB across team
- [ ] Migrate SQLite → PostgreSQL for shared backend
- [ ] Simple web dashboard (React, Vercel/Railway)
- [ ] Stripe billing for $49/mo Team tier
- [ ] Per-user budget caps
- [ ] Slack/email spend alerts

**Target**: 50 teams × $49/mo = $2,450 MRR

---

## #5 — Distribution & Growth

- [ ] `llm check_usage` savings display at session end (makes ROI visible)
- [ ] One-click "share savings" (tweet-sized summary generator)
- [ ] PyPI download tracking → GitHub Stars correlation
- [ ] Goal: 500 GitHub stars, 200 active daily users before Team launch
