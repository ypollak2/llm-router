# LLM Router — Host Support Matrix

This page documents **exactly which features work where**, without sugar-coating limitations. Pick your editor, know what you get.

## Feature Availability by Host

| Feature | Claude Code | Codex CLI | Gemini CLI | Pi (pi.dev) | VS Code/Cursor | Browser | Local CLI |
|---------|:-----------:|:---------:|:----------:|:-----------:|:--------------:|:-------:|:---------:|
| **Auto-Routing Hooks** | ✅ Full | ⚠️ Prompt hook only | ⚠️ Prompt hook only | ❌ Not installable | 🔜 Not yet | ❌ No | ✅ Limited |
| **Session-End Tracking** | ✅ Yes | ❌ No | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ✅ Manual |
| **Quota Pressure Display** | ✅ Yes | ❌ No | ✅ Yes | ❌ No | ❌ No | ❌ No | ❌ No |
| **60 MCP Tools (Direct)** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes |
| **Cost Optimization** | ✅ — | ⚠️ Opt-in* | ✅ — | ✅ — | ⚠️ Partial* | ❌ No | ✅ Manual |
| **Free-First Routing** | ✅ Yes | ⚠️ Opt-in | ✅ Yes | ✅ Yes | ⚠️ Opt-in | ❌ No | ⚠️ Opt-in |
| **Saved Usage Analytics** | ✅ Yes | ⚠️ Routed calls only | ✅ Yes | ✅ Yes | ⚠️ Manual** | ❌ No | ✅ Yes |
| **Decision Replay** | ✅ Yes | ⚠️ Routed calls only | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ✅ Yes |

**Legend:**
- ✅ **Yes** — Fully supported, automatic
- ⚠️ **Partial** — Limited or requires configuration
- 🔜 **Not yet** — *the host supports this; llm-router has not shipped it*
- ❌ **No** — Not possible on this host
- *Codex: the prompt-routing hook (`UserPromptSubmit`) is installed and on by default; tool-call enforcement (`PreToolUse`) is not — `hosts/events.py:routing_ready("codex")` is False until its payload keys are verified*
- *Gemini CLI: the prompt-routing hook (`UserPromptSubmit`) is installed and on by default; tool-call enforcement (`PreToolUse`) is not implemented — `hosts/events.py:routing_ready("gemini-cli")` is False*
- *VS Code/Cursor: Manual routing via MCP tools, no automatic native-turn hooks
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

**Cost savings:** not measured — see
[audit/CRITICAL_2026-09-24_local_models_do_no_work.md](../audit/CRITICAL_2026-09-24_local_models_do_no_work.md).

**Why it's best:**
- Hooks have full access to Claude Code's runtime
- Session-level tracking captures every decision
- Real-time quota data from Claude subscription
- No manual routing needed

**Limitations:**
- Hooks only run in Claude Code (not VS Code or Cursor)

---

### 🟡 Codex CLI (prompt-routing hook; no tool-call enforcement)

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

### 🟡 Gemini CLI (prompt-routing hook; no tool-call enforcement)

**Tier: Automatic Routing (push) + MCP**

Google's agent runner. `llm-router install --host gemini-cli` wires the same
push-routing hook Claude Code gets; the tool-call enforcement hook does not
exist for this host (`hosts/events.py:routing_ready("gemini-cli")` is False).

**Activation:**
```bash
llm-router install --host gemini-cli
```

**Features:**
- ✅ Push routing: the prompt hook (`gemini-cli-auto-route.py`) is installed and on by default
- ✅ Session tracking (cost breakdown logged)
- ✅ Gemini models in primary chains
- ✅ Free-first routing (Ollama → Gemini Flash → GPT-4o)
- ✅ Budget tracking

**Cost savings:** not measured (see the Claude Code section above).

**Why it's good:**
- Gemini CLI runtime is stable and fast
- Google's free tier (1M tokens/day) available
- Works alongside Claude subscription
- Good for cost-conscious teams

**Limitations:**
- Tool-call enforcement (`PreToolUse`) is not implemented for Gemini CLI, so `enforce-route` is Claude Code only
- Gemini Free tier has daily limits
- No real-time quota display
- Requires Gemini account setup

---

### ⚪ Pi Coding Agent (not installable today)

> **Status 2026-09-24:** `llm-router install --host pi` fails with `Unknown host(s): pi`.
> Pi wiring exists only in a legacy code path the documented command never reaches, so
> everything below describes the intended integration, not something you can install.

**Tier: Full Cost Optimization**

Inflection AI's coding agent. MCP support via `~/.pi/agent/mcp.json`, lazy lifecycle by default.

**Activation:**
```bash
llm-router install --host pi
```

**Features:**
- ✅ All 70 MCP tools available (via MCP proxy or directTools)
- ✅ Session tracking (cost breakdown logged)
- ✅ Free-first routing (Ollama → Gemini Flash → GPT-4o)
- ✅ Budget tracking
- ✅ Lazy lifecycle (connects on first tool call, auto-disconnects)

**Cost savings:** not measured (see the Claude Code section above).

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
- ✅ All 70 MCP tools available (llm_route, llm_query, llm_code, etc.)
- ✅ Manual invocation of routing tools
- ⚠️ No automatic hook-based routing
- ⚠️ No session tracking (unless you invoke tools)
- ⚠️ Analytics require manual snapshots

**Cost savings:** not measured (see the Claude Code section above).

**Why you might use it:**
- VS Code and Cursor are lighter weight than Claude Code
- You have fine-grained control over routing
- Works alongside other extensions
- No hook deadlock risk

**Limitations:**
- No auto-routing (you manually invoke `llm_route`)
- No session tracking unless you run `llm-router snapshot` manually
- Higher cognitive load (you pick tools, not automatic)
- Lower savings than Claude Code (not measured)

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
→ **Claude Code** (auto-hooks; savings not measured)

### "I'm already in Codex"
→ **Codex CLI** (the prompt hook routes automatically; tool calls are not enforced; native turns are not tracked)

### "I want to use Gemini for free tier"
→ **Gemini CLI** (free tier included; prompt hook only, no tool-call enforcement)

### "I want to use Pi's coding agent"
→ **Pi** — not installable via `llm-router install` today (see the Pi section)

### "I prefer VS Code"
→ **VS Code MCP** (manual routing, low friction; savings not measured)

### "I want to check costs after work"
→ **Web Dashboard** (read-only analytics)

### "I'm scripting or batch processing"
→ **Local CLI** (explicit routing per call)

---

## Feature Deep-Dives

### Auto-Routing Hooks

**Supported on:**
- Claude Code ✅
- Codex CLI ⚠️ (prompt hook only; no tool-call enforcement)
- Gemini CLI ⚠️ (prompt hook only; no tool-call enforcement)
- Pi (pi.dev) ❌ (not installable today)
- VS Code/Cursor ❌
- Browser ❌
- Local CLI ⚠️ (manual only)

Hooks run **before** Claude's tool calls, analyzing the prompt to decide if routing is needed. Works only on hosts with runtime hooks support.

### Session Tracking

**Supported on:**
- Claude Code ✅ (automatic)
- Codex CLI ⚠️ (routed MCP calls only)
- Gemini CLI ✅ (automatic)
- Pi (pi.dev) ❌ (not installable today)
- VS Code/Cursor ❌ (would need manual invocation)
- Browser ❌
- Local CLI ✅ (manual `llm-router snapshot`)

Automatic session tracking logs every routing decision for analytics. Manual tracking requires periodic snapshots.

### Cost Optimization Quality

**Savings by host:** not measured (see the Claude Code section above).

| Host | Best Case | Typical | Worst Case | Notes |
|------|-----------|---------|-----------|-------|
| Claude Code | — | — | — | Hooks catch every decision |
| Codex CLI | — | — | — | Prompt hook routes; tool calls not enforced |
| Gemini CLI | — | — | — | Prompt hook installed; tool-call enforcement not implemented |
| Pi (pi.dev) | — | — | — | Not installable today |
| VS Code/Cursor | — | — | — | Only when you invoke tools |
| Browser | — | — | — | Read-only—no active routing |
| Local CLI | — | — | — | Scripting only—not continuous |

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

# Metrics are shared across all hosts
llm-router snapshot  # Shows combined stats
```

### "Which host should I use if I care about cost savings?"

**Claude Code > Gemini CLI > Codex CLI / VS Code/Cursor**

Ranking by cost optimization (savings not measured — see the Claude Code section above):
1. **Claude Code** — Automatic hooks
2. **Gemini CLI** — Prompt hook only, no tool-call enforcement
3. **Codex CLI** — Explicit MCP routing only
4. **VS Code/Cursor** — Manual routing

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
| Cost savings | 🟢 — | ⚠️ Opt-in | 🟡 — | 🟡 — | 🟠 — | ⚪ — | 🟡 — |
| Setup friction | 🟢 Low | 🟡 Med | 🟡 Med | 🟢 Low | 🟢 Low | 🟢 Low | 🟡 Med |
| Auto-routing | ✅ Yes | ❌ No | ⚠️ Prompt only | ✅ Yes | ❌ No | ❌ No | ⚠️ Partial |
| Recommend | **🥇 Gold** | ⚠️ Manual | **🥈 Silver** | ⛔ Not installable | ⚠️ Manual | 📊 Analytics | 🔧 Advanced |

**TL;DR:** Want max savings? Use Claude Code. Want flexibility? Pick your editor, use MCP tools manually. Want analytics? Check the dashboard.
