# LLM Router — Engineering Audit Q2 2026

> Brutally honest diagnostic. Every claim backed by file path and line number.
> Audience: Yali (engineer), CTO (will ask hard questions).

---

## 1.1 Activation Path

### How routing is invoked

Three layers, all must work:

1. **MCP Server** — registered in `~/.claude.json` as `uv run --directory ... llm-router`. Starts on Claude Code launch. Exposes 60+ tools (`llm_query`, `llm_code`, etc.)
2. **UserPromptSubmit hook** — `~/.claude/hooks/llm-router-auto-route.py` classifies every prompt and emits a routing directive into context
3. **PreToolUse hook** — `~/.claude/hooks/llm-router-enforce-route.py` blocks tools that violate the routing directive

### What must be true for routing on a fresh session

| Requirement | How it gets there | Failure if missing |
|---|---|---|
| MCP server registered in `~/.claude.json` | `llm-router install` | Tools don't appear — no `llm_*` in tool list |
| Hook files in `~/.claude/hooks/` | `llm-router install` copies from `src/llm_router/hooks/` | Hook silently skipped by Claude Code |
| Hooks registered in `~/.claude/settings.json` | `llm-router install` adds entries | Files exist but never executed |
| Python interpreter path correct in hook commands | Hardcoded at install time (`sys.executable`) | Hook fails with "python not found" — **silent** |
| `.env` or `~/.llm-router/config.yaml` with API keys | Manual setup | Ollama-only fallback; if Ollama down, heuristics-only |
| `~/.llm-router/` directory exists and writable | Auto-created by cost.py | State writes fail silently |

**Hooks are global** (in `~/.claude/`), not per-repo. One broken hook affects all repos.

### Silent failure modes

| # | Failure | Detection | User sees |
|---|---------|-----------|-----------|
| A | MCP server fails to start | Tools missing from list | Nothing — just no `llm_*` tools |
| B | Hook Python path wrong (venv moved) | Hook command errors | **Nothing** — Claude Code suppresses hook errors |
| C | Hook timeout (Ollama slow, >5-30s) | Hook killed mid-execution | **Nothing** — no routing directive, Claude answers directly |
| D | All API keys expired | Caught in auto-route.py fallback chain | Heuristics-only routing; user unaware quality degraded |
| E | Ollama not running | auto-route.py catches, falls back to API | Works but costs ~$0.0001/classification instead of free |
| F | `.env` unreadable | Skipped silently | No API keys loaded; Ollama-only or heuristics-only |
| G | Duplicate hook entries accumulate | No detection | First broken entry fails, second runs — wasteful but functional |

**Worst case**: User moves their Python venv → all hooks silently break → every prompt goes to Opus/Sonnet unrouted → **50-100x higher cost with zero indication**.

### Is setup idempotent?

**Mostly yes, with a gap:**
- Hook file copy: idempotent (version-checked, `install_hooks.py` lines 146-162)
- Hook registration: conditional (`install_hooks.py` lines 333-349) — but uses script-path matching, not interpreter-path matching
- **Gap**: Running `llm-router install` from a different venv adds a second hook entry instead of replacing the first → duplicate entries accumulate in `settings.json`
- Rules update: idempotent (version-checked, lines 170-191)

### Recovery

```bash
llm-router install --force  # Re-copies hooks, re-registers with current Python
llm-router doctor            # Should diagnose (but doesn't detect all failures)
```

---

## 1.2 Routing Logic

### Classification taxonomy

**4 complexity levels** (`types.py` lines 126-138):
- `simple` → BUDGET profile (Haiku/Flash)
- `moderate` → BALANCED profile (Sonnet/GPT-4o)
- `complex` → PREMIUM profile (Opus/o3)
- `deep_reasoning` → PREMIUM + extended thinking

**5 text task types** (`types.py` lines 70-84): query, research, generate, analyze, code

### Classification chain (6 layers, first-match wins)

| Priority | Method | Cost | Latency | Accuracy |
|---|---|---|---|---|
| 1 | Skip patterns (`/help`, `/clear`, etc.) | $0 | 0ms | 100% |
| 2 | Build-task fast-path (verb + object regex) | $0 | 0ms | ~95% |
| 3 | Content generation detection | $0 | 0ms | ~90% |
| 4 | Heuristic scoring (intent/topic/format patterns, threshold=12) | $0 | 0ms | 65-75% |
| 5 | Ollama local LLM (qwen3.5) | $0 | ~3s | 85-90% |
| 6 | API fallback (Gemini Flash / GPT-4o-mini) | ~$0.0001 | ~1s | 92-95% |

If all fail → safe fallback to `query/moderate` (0% confidence).

Source: `hooks/auto-route.py` lines 991-1070, `classifier.py` lines 122-276

### Edge cases where classification is wrong

| Prompt | Classified as | Should be | Root cause |
|---|---|---|---|
| "prove P=NP is undecidable" | query/simple (fallback) | deep_reasoning/complex | Heuristic has no deep_reasoning patterns in scoring |
| "first implement auth, then add logging, then optimize" | code/moderate | complex (multi-step orchestration) | Heuristic is linear — one category wins, doesn't detect sequential structure |
| "why is this so slow?" (after code context) | query/simple | analyze/moderate | Context inheritance only works for 1-3 word continuations ("ok", "yes") |
| "how to write async in Python?" | code/simple | query/simple | "write" matches code intent, but user is asking, not doing |
| 600-char tutorial question with code | complex (length-based) | query/moderate | Length is a weak proxy for complexity (`auto-route.py` lines 973-985) |

### Does the classifier use an LLM call?

**Yes, conditionally.** Only when heuristic confidence < 12. Models tried: Haiku → Gemini Flash Lite → Groq → GPT-4o-mini → DeepSeek → Mistral (`profiles.py` lines 265-276). Cost tracked in `ClassificationResult.classifier_cost_usd` and `classifier_latency_ms`.

---

## 1.3 Metrics & Savings Math

### Every savings formula found

| # | Location | Formula | Baseline | Honesty |
|---|---|---|---|---|
| 1 | `text.py` `_savings_info()` lines 108-119 | `ratio = sonnet_cost / actual_cost` | Sonnet 4.6 ($15/M output) | **SHAKY** — Sonnet baseline reasonable for API users; meaningless for subscription users |
| 2 | `router.py` `_estimate_opus_cost()` lines 39-50 | `(input/1M)*15 + (output/1M)*75` | Opus ($15/$75 per M) | **MISLEADING** — nobody uses Opus for simple queries; inflates savings 10-50x |
| 3 | `gain.py` `estimate_opus_cost()` lines 122-142 | `actual_cost * multiplier{haiku:10, sonnet:3, flash:15}` | Opus via hand-rolled multipliers | **MISLEADING** — multipliers are heuristic guesses, not real pricing math; 2-5x inflation |
| 4 | `cost.py` `calc_savings()` lines 1113-1142 | `opus_cost - actual_cost` | Opus | **SHAKY** — same Opus strawman |
| 5 | `session-end.py` `_sonnet_baseline()` line 407 | `(in*3.0 + out*15.0) / 1M` | Sonnet 4.6 (hardcoded) | **SOLID** — Sonnet is defensible; excludes subscription provider |
| 6 | `cost.py` `get_routing_savings_vs_sonnet()` | `sonnet_cost(tokens) - actual_cost` | Sonnet 4.6 (explicit) | **SOLID** — transparent, explicit baseline |

### Critical issues

**Issue 1: Classifier costs NOT subtracted from savings** 🔴

Classification costs $0.0001-0.0005 per call. These are tracked (`ClassificationResult.classifier_cost_usd`) but never subtracted from gross savings. If routing saves $0.0003 and classification cost $0.0002, net savings = $0.0001 but reported as $0.0003.

**Issue 2: Subscription users see fake dollar savings** 🔴

For Claude Max ($200/mo flat), marginal cost per query = $0. When a query is routed to Ollama, the system reports "$0.015 saved" (Sonnet baseline). The user saved **zero dollars** — they already paid. What they actually saved is **quota headroom**.

`session-end.py` line 364 correctly excludes subscription provider from dollar savings, but free-model routing (Ollama) still gets full Sonnet baseline as "savings."

**Issue 3: Opus baseline is a strawman** 🟡

Multiple places compare against Opus ($75/M output). Nobody uses Opus for "what does REST API mean?" — they'd use Sonnet or Haiku. The Opus baseline inflates savings 10-50x for simple queries. Sonnet baseline (used in session-end.py) is more honest.

**Issue 4: Failed routes/retries not counted** 🟡

`session-end.py` line 236: `WHERE success = 1` filters out failed attempts. If model A fails and model B succeeds, only B's cost is counted. The latency cost of trying A is invisible.

### Honesty summary for CTO

| Claim | Defensible? | Conditions |
|---|---|---|
| "30% cost savings" | YES | API-key users, Sonnet baseline, classifier overhead ignored |
| "60-80% savings" | ONLY | With Opus baseline (inflated) or heavy Ollama usage |
| "$ saved" for subscription users | NO | Marginal cost = $0; should say "quota freed" instead |
| "87% savings" in docs | NO | Single-user peak, not representative; stated in README but with caveat |

**Most credible statement**: "Reduces API costs ~30% vs Sonnet baseline for pay-per-token users. Subscription users free quota for other tasks."

---

## 1.4 Telemetry & Persistence

### What's logged

**9 SQLite tables** in `~/.llm-router/usage.db` (global, cross-repo):

| Table | Records | Key data |
|---|---|---|
| `usage` | Every external LLM call | model, tokens, cost, latency, success, correlation_id |
| `routing_decisions` | Full decision lifecycle (27 cols) | prompt_hash, classifier_type/model/confidence, recommended vs final model, downshift info |
| `claude_usage` | Claude Code model consumption | model, tokens, complexity, cost_saved |
| `savings_stats` | Per-call savings (from hook JSONL) | session_id, task_type, estimated_claude_cost_saved, model_used |
| `semantic_cache` | Cached embeddings + responses | task_type, embedding (768-dim), response_content, model |
| `corrections` | User reroute feedback | original → corrected model/tool |
| `compression_stats` | Token compression metrics | original vs compressed tokens, strategy |
| `model_quality_trends` | Rolling quality scores | model, task_type, avg_score, trend |
| `quota_snapshots` | Per-prompt quota state | claude session/weekly %, final model, was_downgraded |

### Data survival

| Data | Session end | MCP restart | Disk |
|---|---|---|---|
| SQLite tables | Survives | Survives | Survives |
| JSONL logs | Survives | Survives | Survives |
| Classification cache (LRU) | **Lost** | **Lost** | N/A |
| Session buffer (10 messages) | **Lost** | **Lost** | N/A |
| Profile override | **Lost** | **Lost** | N/A |

### Audit trail gaps

**MCP routes**: Full trail (prompt hash → classification → model → cost → counterfactual). ✅

**Hook routes**: Partial — decision logged but **execution outcome delayed ~30s** (async JSONL import at session end). Gap between routing and recording.

**Not logged at all**:
- Raw prompt text (only SHA-256 hash of first 500 chars — privacy by design)
- Intermediate model chain (which models were tried and skipped before success)
- Circuit breaker state changes (provider went unhealthy — invisible)
- Classification cache hit/miss history (in-memory only, lost on restart)

### Cross-repo: data aggregates globally

All data in `~/.llm-router/usage.db` — single database for all repos. `project_id` column exists but **no queries filter by it**. Lifetime analytics span all projects.

---

## 1.5 Visibility to the User

### What users see today

| Event | User sees | User doesn't see |
|---|---|---|
| **Prompt routed (auto-route hook)** | Routing directive box: task type, tool to call, estimated savings | Which model was actually selected, real cost, classification confidence |
| **MCP tool response** | Footer: `→ gemini-2.5-flash · simple · $0.0002 (43x cheaper)` | Why that model was chosen (unless verbose mode) |
| **Routing violation blocked** | Block message with remediation steps | Full enforcement log |
| **Session end** | Aggregate savings summary | Per-prompt routing decisions |
| **Nothing routed** | **Nothing** — user has no idea routing was skipped | Everything — complete invisibility |

### The critical gap

**Hook-level routing decisions are invisible after execution.** The auto-route hook tells Claude what tool to call (pre-response), and the MCP tool footer shows what model answered (in-response), but these two pieces are **never connected**. The user can't confirm:
- That the routing directive was actually followed
- That the estimated savings matched actual savings
- That the model selection was appropriate

### The v8.2.0 explainability footer

**Works for direct MCP calls** — every `llm_query`/`llm_code`/etc. response shows:
```
─────
→ gemini-2.5-flash · simple · $0.0002 (43x cheaper)
```

**Does NOT bridge the hook→tool gap** — the footer doesn't say "this was routed from your previous prompt via auto-route hook with heuristic confidence 94%."

### Where we could surface routing

1. Hook output (system message in context) — **already used** for directives
2. MCP tool response footer — **already implemented** (v8.2.0)
3. Session-end summary — **already shows aggregates**, could add per-prompt log
4. Status bar hook — **exists** but shows only aggregate metrics, not decisions
5. Persistent log file (`.claude/router-log.jsonl`) — **not implemented**

---

## Prioritized Issue List

### Blockers (must fix before showing CTO)

| # | Issue | Impact | Section |
|---|---|---|---|
| **B1** | Silent hook failures — venv path changes break all routing with zero indication | User pays 50-100x more without knowing | 1.1 |
| **B2** | Subscription users see fake dollar savings | CTO will immediately question "$X saved" on flat-rate plan | 1.3 |
| **B3** | Opus baseline inflates savings 10-50x in gain.py | Any scrutiny of the numbers will find this | 1.3 |

### Major (fix before rollout)

| # | Issue | Impact | Section |
|---|---|---|---|
| **M1** | Classifier costs not subtracted from savings | Net savings overstated by 1-10% | 1.3 |
| **M2** | No `llm_doctor` that detects all failure modes | Broken installations persist indefinitely | 1.1 |
| **M3** | Deep reasoning prompts misclassified as simple | "prove P=NP" gets Haiku instead of Opus | 1.2 |
| **M4** | No per-prompt audit trail visible to user | Can't verify routing worked correctly | 1.5 |
| **M5** | Hook-level routing has 30s telemetry delay | Decisions aren't in DB until session end | 1.4 |
| **M6** | Length-based complexity fallback (>500 chars = complex) | Long simple questions get expensive models | 1.2 |

### Minor (improve over time)

| # | Issue | Impact | Section |
|---|---|---|---|
| **m1** | Duplicate hook entries accumulate | Wasteful but functional | 1.1 |
| **m2** | Classification cache is in-memory only | Cold starts re-classify everything | 1.4 |
| **m3** | Context inheritance fragile for short follow-ups | "why is this slow?" loses code context | 1.2 |
| **m4** | No per-project filtering in analytics | Cross-repo aggregation can't be scoped | 1.4 |
| **m5** | Session files in `~/.llm-router/` may be world-readable | Security on shared systems | 1.4 |

---

*Generated 2026-05-14. All findings verified against source code at commit `004c193` (v8.4.0).*
