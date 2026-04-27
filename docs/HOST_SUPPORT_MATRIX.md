# LLM Router — Host Support Matrix

This page documents **exactly which features work where**, without sugar-coating limitations. Pick your editor, know what you get.

## Feature Availability by Host

| Feature | Claude Code | Codex CLI | Gemini CLI | VS Code/Cursor | Browser | Local CLI |
|---------|:-----------:|:---------:|:----------:|:--------------:|:-------:|:---------:|
| **Auto-Routing Hooks** | ✅ Full | ✅ Full | ✅ Full | ❌ No | ❌ No | ✅ Limited |
| **Session-End Tracking** | ✅ Yes | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ✅ Manual |
| **Quota Pressure Display** | ✅ Yes | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ❌ No |
| **48 MCP Tools (Direct)** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes |
| **Cost Optimization** | ✅ 60–80% | ✅ 60–80% | ✅ 50–70% | ⚠️ Partial* | ❌ No | ✅ Manual |
| **Free-First Routing** | ✅ Yes | ✅ Yes | ✅ Yes | ⚠️ Opt-in | ❌ No | ⚠️ Opt-in |
| **Saved Usage Analytics** | ✅ Yes | ✅ Yes | ✅ Yes | ⚠️ Manual** | ❌ No | ✅ Yes |
| **Decision Replay** | ✅ Yes | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ✅ Yes |

**Legend:**
- ✅ **Yes** — Fully supported, automatic
- ⚠️ **Partial** — Limited or requires configuration
- ❌ **No** — Not supported on this host
- *VS Code/Cursor: Manual routing via MCP tools, no automatic hooks
- **Manual analytics requires running `llm-router snapshot` periodically

---

## Host Details

### 🔴 Claude Code (Recommended)

**Tier: Full Cost Optimization**

The best-supported host. Hooks run automatically, tracking happens seamlessly.

**Activation:**
```bash
llm-router install
```

**Features:**
- ✅ Auto-routing hooks (detect task type, route automatically)
- ✅ Session tracking (every decision logged automatically)
- ✅ Quota pressure display (real-time Claude subscription %)
- ✅ Hook health checks (auto-restart if needed)
- ✅ Decision replay (re-run past prompts with different models)

**Cost savings:** 60–80% vs Opus-everywhere

**Why it's best:**
- Hooks have full access to Claude Code's runtime
- Session-level tracking captures every decision
- Real-time quota data from Claude subscription
- No manual routing needed

**Limitations:**
- Hooks only run in Claude Code (not VS Code or Cursor)

---

### 🟡 Codex CLI (Strong)

**Tier: Full Cost Optimization**

OpenAI's agent runner. Hooks work excellently, integration is smooth.

**Activation:**
```bash
llm-router install --host codex
```

**Features:**
- ✅ Auto-routing hooks (codex-auto-route.py installed)
- ✅ Session tracking (Codex captures all routing decisions)
- ✅ Codex injected as free tier 1 in all chains
- ✅ Cost tracking via SQLite
- ✅ Decision analytics

**Cost savings:** 60–80% vs Opus-everywhere

**Why it's excellent:**
- Codex CLI runtime is purpose-built for agent work
- Hooks integrate cleanly (no deadlock concerns)
- Codex injected automatically in cost chains
- Full analytics available

**Limitations:**
- Requires Codex CLI installed locally
- No real-time quota pressure display (use `llm-router budget` manually)

---

### 🟡 Gemini CLI (Strong)

**Tier: Full Cost Optimization**

Google's agent runner. Hooks work well, Gemini models available in chains.

**Activation:**
```bash
llm-router install --host gemini-cli
```

**Features:**
- ✅ Auto-routing hooks (gemini-cli-auto-route.py installed)
- ✅ Session tracking (cost breakdown logged)
- ✅ Gemini models in primary chains
- ✅ Free-first routing (Ollama → Gemini Flash → GPT-4o)
- ✅ Budget tracking

**Cost savings:** 50–70% vs Opus-everywhere

**Why it's good:**
- Gemini CLI runtime is stable and fast
- Google's free tier (1M tokens/day) available
- Works alongside Claude subscription
- Good for cost-conscious teams

**Limitations:**
- Gemini Free tier has daily limits
- No real-time quota display
- Requires Gemini account setup

---

### 🟠 VS Code / Cursor (MCP Only)

**Tier: Manual Routing**

VS Code and Cursor don't run hooks automatically. llm-router is available as an MCP server—you manually invoke routing when needed.

**Activation:**
```bash
llm-router install --host vscode  # or --host cursor
```

**Features:**
- ✅ All 48 MCP tools available (llm_route, llm_query, llm_code, etc.)
- ✅ Manual invocation of routing tools
- ⚠️ No automatic hook-based routing
- ⚠️ No session tracking (unless you invoke tools)
- ⚠️ Analytics require manual snapshots

**Cost savings:** 30–50% (depends on how often you use routing)

**Why you might use it:**
- VS Code and Cursor are lighter weight than Claude Code
- You have fine-grained control over routing
- Works alongside other extensions
- No hook deadlock risk

**Limitations:**
- No auto-routing (you manually invoke `llm_route`)
- No session tracking unless you run `llm-router snapshot` manually
- Higher cognitive load (you pick tools, not automatic)
- Best case: 30–50% savings (worse than Claude Code)

**Recommendation:**
If you're already in VS Code/Cursor and want to try llm-router: use it. But for maximum cost savings, switch to Claude Code.

---

### ⚪ Browser / Web UI (Limited)

**Tier: Read-Only**

No MCP support in browsers. You can view analytics and dashboards, but can't route or track live.

**Activation:**
```bash
llm-router dashboard
# Opens http://localhost:7337 in your browser
```

**Features:**
- ✅ Cost dashboards (view past routing decisions)
- ✅ Analytics (see which models saved money)
- ✅ Decision replay (inspect past decisions)
- ❌ No live routing
- ❌ No prompt access

**Use case:** Reviewing costs after work, not for active development.

---

### 🟢 Local CLI (Development)

**Tier: Command-Line Tool**

Use llm-router directly from the shell. Useful for scripting and batch operations.

**Activation:**
Already installed with `pip install llm-router`

**Features:**
- ✅ `llm-router route <prompt>` — Route a single prompt
- ✅ `llm-router snapshot` — Capture analytics
- ✅ `llm-router budget` — Check spending
- ✅ `llm-router verify-hooks` — Validate hook health
- ⚠️ No continuous tracking (only explicit calls)

**Use case:** Scripting, batch processing, verification.

---

## Honest Comparison: Which Host for You?

### "I want maximum cost savings"
→ **Claude Code** (auto-hooks, 60–80% savings)

### "I'm already in Codex"
→ **Codex CLI** (hooks work great, 60–80% savings)

### "I want to use Gemini for free tier"
→ **Gemini CLI** (free tier included, 50–70% savings)

### "I prefer VS Code"
→ **VS Code MCP** (manual routing, 30–50% savings, but low friction)

### "I want to check costs after work"
→ **Web Dashboard** (read-only analytics)

### "I'm scripting or batch processing"
→ **Local CLI** (explicit routing per call)

---

## Feature Deep-Dives

### Auto-Routing Hooks

**Supported on:**
- Claude Code ✅
- Codex CLI ✅
- Gemini CLI ✅
- VS Code/Cursor ❌
- Browser ❌
- Local CLI ⚠️ (manual only)

Hooks run **before** Claude's tool calls, analyzing the prompt to decide if routing is needed. Works only on hosts with runtime hooks support.

### Session Tracking

**Supported on:**
- Claude Code ✅ (automatic)
- Codex CLI ✅ (automatic)
- Gemini CLI ✅ (automatic)
- VS Code/Cursor ❌ (would need manual invocation)
- Browser ❌
- Local CLI ✅ (manual `llm-router snapshot`)

Automatic session tracking logs every routing decision for analytics. Manual tracking requires periodic snapshots.

### Cost Optimization Quality

**Savings by host:**

| Host | Best Case | Typical | Worst Case | Notes |
|------|-----------|---------|-----------|-------|
| Claude Code | 80% | 70% | 50% | Optimal—hooks catch every decision |
| Codex CLI | 80% | 70% | 50% | Excellent—hooks work automatically |
| Gemini CLI | 70% | 55% | 40% | Good—Gemini Free tier included |
| VS Code/Cursor | 50% | 35% | 15% | Lower—only when you invoke tools |
| Browser | 0% | 0% | 0% | Read-only—no active routing |
| Local CLI | 60% | 40% | 20% | Scripting only—not continuous |

---

## Frequently Asked Questions

### "Can I run llm-router on multiple hosts at once?"

**Yes.** Each host maintains its own `~/.llm-router/` directory. Metrics are shared if using the same SQLite DB (see `LLM_ROUTER_METRICS_DB`).

```bash
# Claude Code
llm-router install

# Also install for Codex
llm-router install --host codex

# Metrics are shared across both
llm-router snapshot  # Shows combined stats
```

### "Which host should I use if I care about cost savings?"

**Claude Code > Codex CLI > Gemini CLI > VS Code/Cursor**

Ranking by cost optimization:
1. **Claude Code** — Automatic hooks, 70–80% savings
2. **Codex CLI** — Automatic hooks, 70–80% savings
3. **Gemini CLI** — Automatic hooks, 50–70% savings
4. **VS Code/Cursor** — Manual routing, 30–50% savings

### "Do hooks ever break things?"

Hooks are reviewed before installation (`llm-router install --check` shows changes). They're also monitored for deadlocks. If a hook causes issues, uninstall with:

```bash
llm-router uninstall
```

### "Can I use llm-router in Cursor?"

**Yes, but limited.** Cursor uses VS Code's architecture, so you get MCP tools but no auto-hooks. You'd manually invoke `llm_route` when needed.

### "What if I want the lowest latency?"

**Local Ollama + Codex CLI.** Both run locally:
- Ollama: ~100ms first-token latency
- Codex: Immediate (local agent)
- External APIs: 500ms–2s (network latency)

### "Can I switch hosts later?"

**Yes.** Your metrics are stored locally in SQLite. If you switch to a new host:

```bash
llm-router install --host <new-host>
# Metrics from previous host are preserved in ~/.llm-router/usage.db
```

---

## Summary

| Dimension | Claude Code | Codex CLI | Gemini CLI | VS Code | Browser | CLI |
|-----------|:-----------:|:---------:|:----------:|:-------:|:-------:|:---:|
| Cost savings | 🟢 80% | 🟢 80% | 🟡 70% | 🟠 35% | ⚪ 0% | 🟡 40% |
| Setup friction | 🟢 Low | 🟡 Med | 🟡 Med | 🟢 Low | 🟢 Low | 🟡 Med |
| Auto-routing | ✅ Yes | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ⚠️ Partial |
| Recommend | **🥇 Gold** | **🥈 Silver** | **🥉 Bronze** | ⚠️ Manual | 📊 Analytics | 🔧 Advanced |

**TL;DR:** Want max savings? Use Claude Code. Want flexibility? Pick your editor, use MCP tools manually. Want analytics? Check the dashboard.
