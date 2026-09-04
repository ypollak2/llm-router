# LLM Router — Host Support Matrix

This page documents **exactly which features work where**, without sugar-coating limitations. Pick your editor, know what you get.

## Feature Availability by Host

| Feature | Claude Code | Codex CLI | Gemini CLI | Pi (pi.dev) | VS Code/Cursor | Browser | Local CLI |
|---------|:-----------:|:---------:|:----------:|:-----------:|:--------------:|:-------:|:---------:|
| **Auto-Routing Hooks** | ✅ Full | 🔜 Not yet | ✅ Full | ✅ Full | 🔜 Not yet | ❌ No | ✅ Limited |
| **Session-End Tracking** | ✅ Yes | ❌ No | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ✅ Manual |
| **Quota Pressure Display** | ✅ Yes | ❌ No | ✅ Yes | ❌ No | ❌ No | ❌ No | ❌ No |
| **60 MCP Tools (Direct)** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes |
| **Cost Optimization** | ✅ 60–80% | ⚠️ Opt-in* | ✅ 50–70% | ✅ 50–70% | ⚠️ Partial* | ❌ No | ✅ Manual |
| **Free-First Routing** | ✅ Yes | ⚠️ Opt-in | ✅ Yes | ✅ Yes | ⚠️ Opt-in | ❌ No | ⚠️ Opt-in |
| **Saved Usage Analytics** | ✅ Yes | ⚠️ Routed calls only | ✅ Yes | ✅ Yes | ⚠️ Manual** | ❌ No | ✅ Yes |
| **Decision Replay** | ✅ Yes | ⚠️ Routed calls only | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ✅ Yes |

**Legend:**
- ✅ **Yes** — Fully supported, automatic
- ⚠️ **Partial** — Limited or requires configuration
- 🔜 **Not yet** — *the host supports this; llm-router has not shipped it*
- ❌ **No** — Not possible on this host
- *Codex/VS Code/Cursor: Manual routing via MCP tools, no automatic native-turn hooks
- **Manual analytics requires running `llm-router snapshot` periodically

### Why "not yet" is a separate row from "no"

Until recently, Claude Code was the only host that let anything intercept a
prompt before the model saw it, and this page said "❌ No" for the others. That
is no longer true, and the distinction matters: **❌** means the host cannot do
it, **🔜** means we haven't built it.

| Host | Prompt-interception hook | Can it block? | Status here |
|---|---|:---:|---|
| Claude Code | `UserPromptSubmit` | yes | shipped |
| Codex CLI | `UserPromptSubmit` | yes — `{"decision":"block"}` | shipped (`llm-router install`; hook trust record written to config.toml) |
| Cursor | `beforeSubmitPrompt` | yes | not yet |
| Gemini CLI | `UserPromptSubmit` | yes | shipped |

Codex hooks are enabled by default and its `PreToolUse` can additionally
*rewrite* tool arguments via `updatedInput`, which the Claude Code path cannot.
Cursor's hooks are project-scoped (`.cursor/hooks.json`) with no plugin-root
variable, and its **cloud agents run project hooks but not prompt hooks** — so
auto-routing will not apply to cloud agents even after the port lands.

The machine-readable version of this table is
`llm_router.hosts.events`, which also records which payload fields have been
verified against a real run of each host versus only read off a docs page.
`routing_ready(host)` is the function this page's first row should agree with.

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

### 🟢 Codex CLI (Full)

**Tier: Automatic Routing (push) + MCP**

OpenAI's agent runner. `llm-router install` detects Codex and wires it the
same way it wires Claude Code, so routing works in both directions: cheap
work from Claude Code lands on the ChatGPT seat, hard work from Codex lands
on the Claude seat.

**Activation:**
```bash
llm-router install            # auto-detects Codex; or: --host codex
llm-router doctor             # Codex CLI section + Seats table
```

**What is written (verified against Codex 0.153):**
- `~/.codex/config.toml` — `[mcp_servers.llm_router]` (via `codex mcp add`, TOML
  fallback) and a `[hooks.state."…"] trusted_hash` record per hook. Codex
  silently skips a hook without that record; earlier installers never wrote it.
- `~/.codex/hooks.json` — `UserPromptSubmit` → auto-route (the ⚡ ROUTE hint),
  `PostToolUse` → telemetry.
- `~/.codex/AGENTS.md` — a marked block of routing rules, replaced on re-run.

Earlier versions wrote `config.yaml`, `config.json`, `rules/llm_router.md`
and `instructions.md`. Codex reads none of them; install removes ours.

**Features:**
- ✅ Push routing: the same ⚡ ROUTE hint Claude Code gets, on every prompt
- ✅ MCP routing via `llm_auto` and routed tools
- ✅ Codex injected as free tier 1 in all chains
- ✅ Cost tracking via SQLite; decision analytics

**Limitations:**
- Requires Codex CLI installed locally
- PreToolUse payload keys are not yet captured, so `enforce-route` is Claude Code only
- No native Codex turn/token metering in `llm_session_spend`
- Gateway mode (`--mode gateway`) is opt-in: the gateway does not yet speak
  Codex's "responses" wire format

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

### 🟡 Pi Coding Agent (Strong)

**Tier: Full Cost Optimization**

Inflection AI's coding agent. MCP support via `~/.pi/agent/mcp.json`, lazy lifecycle by default.

**Activation:**
```bash
llm-router install --host pi
```

**Features:**
- ✅ All 60 MCP tools available (via MCP proxy or directTools)
- ✅ Session tracking (cost breakdown logged)
- ✅ Free-first routing (Ollama → Gemini Flash → GPT-4o)
- ✅ Budget tracking
- ✅ Lazy lifecycle (connects on first tool call, auto-disconnects)

**Cost savings:** 50–70% vs single-model usage

**Why it's good:**
- Pi's MCP adapter supports importing configs from other agents
- Lazy lifecycle means zero overhead when not routing
- `directTools` option makes key tools visible without proxy discovery
- Works alongside Pi's native model

**Limitations:**
- No real-time quota display
- Pi's native model isn't tracked by llm-router (only routed calls)

---

### 🟠 VS Code / Cursor (MCP Only)

**Tier: Manual Routing**

VS Code and Cursor don't run hooks automatically. llm-router is available as an MCP server—you manually invoke routing when needed.

**Activation:**
```bash
llm-router install --host vscode  # or --host cursor
```

**Features:**
- ✅ All 60 MCP tools available (llm_route, llm_query, llm_code, etc.)
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
Already installed with `pip install llm-routing`

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
→ **Codex CLI** (invoke MCP routing explicitly; native turns are not tracked)

### "I want to use Gemini for free tier"
→ **Gemini CLI** (free tier included, 50–70% savings)

### "I want to use Pi's coding agent"
→ **Pi** (MCP tools, 50–70% savings, lazy lifecycle)

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
- Codex CLI ❌ (explicit MCP calls only)
- Gemini CLI ✅
- Pi (pi.dev) ✅
- VS Code/Cursor ❌
- Browser ❌
- Local CLI ⚠️ (manual only)

Hooks run **before** Claude's tool calls, analyzing the prompt to decide if routing is needed. Works only on hosts with runtime hooks support.

### Session Tracking

**Supported on:**
- Claude Code ✅ (automatic)
- Codex CLI ⚠️ (routed MCP calls only)
- Gemini CLI ✅ (automatic)
- Pi (pi.dev) ✅ (automatic)
- VS Code/Cursor ❌ (would need manual invocation)
- Browser ❌
- Local CLI ✅ (manual `llm-router snapshot`)

Automatic session tracking logs every routing decision for analytics. Manual tracking requires periodic snapshots.

### Cost Optimization Quality

**Savings by host:**

| Host | Best Case | Typical | Worst Case | Notes |
|------|-----------|---------|-----------|-------|
| Claude Code | 80% | 70% | 50% | Optimal—hooks catch every decision |
| Codex CLI | 80% | Varies | 0% | Savings only for explicitly routed calls |
| Gemini CLI | 70% | 55% | 40% | Good—Gemini Free tier included |
| Pi (pi.dev) | 70% | 55% | 40% | Good—lazy lifecycle, MCP proxy |
| VS Code/Cursor | 50% | 35% | 15% | Lower—only when you invoke tools |
| Browser | 0% | 0% | 0% | Read-only—no active routing |
| Local CLI | 60% | 40% | 20% | Scripting only—not continuous |

---

## Frequently Asked Questions

### "Can I run llm-router on multiple hosts at once?"

**Yes.** Each host maintains its own `~/.llm-router/` directory. Metrics are shared automatically
across hosts on the same machine since they all read/write the same `~/.llm-router/routing.db`
SQLite file — there's no separate env var to configure this.

```bash
# Claude Code
llm-router install

# Also install for Codex
llm-router install --host codex

# Also install for Pi
llm-router install --host pi

# Metrics are shared across all hosts
llm-router snapshot  # Shows combined stats
```

### "Which host should I use if I care about cost savings?"

**Claude Code > Gemini CLI / Pi > Codex CLI / VS Code/Cursor**

Ranking by cost optimization:
1. **Claude Code** — Automatic hooks, 70–80% savings
2. **Gemini CLI** — Automatic hooks, 50–70% savings
3. **Pi (pi.dev)** — MCP tools, 50–70% savings
4. **Codex CLI** — Explicit MCP routing only
5. **VS Code/Cursor** — Manual routing, 30–50% savings

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

| Dimension | Claude Code | Codex CLI | Gemini CLI | Pi (pi.dev) | VS Code | Browser | CLI |
|-----------|:-----------:|:---------:|:----------:|:-----------:|:-------:|:-------:|:---:|
| Cost savings | 🟢 80% | ⚠️ Opt-in | 🟡 70% | 🟡 70% | 🟠 35% | ⚪ 0% | 🟡 40% |
| Setup friction | 🟢 Low | 🟡 Med | 🟡 Med | 🟢 Low | 🟢 Low | 🟢 Low | 🟡 Med |
| Auto-routing | ✅ Yes | ❌ No | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ⚠️ Partial |
| Recommend | **🥇 Gold** | ⚠️ Manual | **🥈 Silver** | **🥉 Bronze** | ⚠️ Manual | 📊 Analytics | 🔧 Advanced |

**TL;DR:** Want max savings? Use Claude Code. Want flexibility? Pick your editor, use MCP tools manually. Want analytics? Check the dashboard.
