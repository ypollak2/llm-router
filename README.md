# llm-router

> Route every AI call to the cheapest model that can handle it.
> 46 tools · 20+ providers · Claude Code, VS Code, Cursor, Codex, and more.

[![PyPI](https://img.shields.io/pypi/v/claude-code-llm-router?style=flat-square)](https://pypi.org/project/claude-code-llm-router/)
[![Tests](https://img.shields.io/github/actions/workflow/status/ypollak2/llm-router/ci.yml?style=flat-square&label=tests)](https://github.com/ypollak2/llm-router/actions)
[![Downloads](https://img.shields.io/pypi/dm/claude-code-llm-router?style=flat-square)](https://pypi.org/project/claude-code-llm-router/)
[![Python](https://img.shields.io/badge/python-3.10–3.13-blue?style=flat-square)](https://pypi.org/project/claude-code-llm-router/)
[![MCP](https://img.shields.io/badge/MCP-1.0+-purple?style=flat-square)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Stars](https://img.shields.io/github/stars/ypollak2/llm-router?style=flat-square&color=yellow)](https://github.com/ypollak2/llm-router/stargazers)

**Average savings: 60–80% vs running everything on Claude Opus.**

```bash
# One command to start saving
uvx claude-code-llm-router install

# Or: guided 5-minute setup
uvx claude-code-llm-router quickstart
```

| Host | One-line install |
|------|-----------------|
| <img src="https://img.shields.io/badge/-Claude_Code-191919?logo=anthropic&logoColor=white" height="16"/> Claude Code | `llm-router install` |
| <img src="https://img.shields.io/badge/-VS_Code-0078d4?logo=visual-studio-code&logoColor=white" height="16"/> VS Code | `llm-router install --host vscode` |
| <img src="https://img.shields.io/badge/-Cursor-000000?logoColor=white" height="16"/> Cursor | `llm-router install --host cursor` |
| <img src="https://img.shields.io/badge/-Codex_CLI-412991?logo=openai&logoColor=white" height="16"/> Codex CLI | `llm-router install --host codex` |

---

LLM Router is an MCP server and hook set that intercepts prompts and routes them to the cheapest model that can handle the task.

It is built for a common failure mode in AI coding tools: using your best model for everything. In Claude Code, that burns quota on simple explanations, file lookups, small edits, and repetitive prompts. In other MCP clients, it means paying premium-model prices for work that never needed them.

The goal is simple: keep cheap work on cheap or free models, keep hard work on Claude or other premium models, and remove the need to micromanage model selection. Works in Claude Code, Cursor, VS Code, Codex, Windsurf, Zed, claw-code, and Agno.

---

## Why

Most sessions contain a lot of low-value turns: quick questions, repo lookups, boilerplate edits, and small follow-ups. Those are exactly the prompts that quietly burn through premium models.

LLM Router offloads that work first, then escalates when the task actually needs more capability.

- Cheap work stays cheap.
- Hard work still gets the best model.
- Your workflow stays the same.

It does not try to replace Claude or force weak models onto hard tasks. It removes the waste around them.

---

## Quick Start

```bash
pipx install claude-code-llm-router && llm-router install
```

`llm-router install` registers the MCP server and installs hooks so prompt routing starts automatically.

If you use Claude Code Pro/Max, you can start with zero API keys. Otherwise add `GEMINI_API_KEY` for a cheap free-tier fallback.

```bash
GEMINI_API_KEY=AIza...              # optional free-tier fallback
LLM_ROUTER_CLAUDE_SUBSCRIPTION=true
```

---

## How It Works

1. Intercept the prompt before your default premium model sees it.
2. Classify the task and its complexity.
3. Try the cheapest capable route first.
4. Escalate or fall back when the task needs more capability.

Under the hood, every prompt goes through a `UserPromptSubmit` hook before your top-tier model sees it:

```
0. Context inherit      instant, free    "yes/ok/go ahead" reuse prior turn's route
1. Heuristic scoring    instant, free    high-confidence patterns route immediately
2. Ollama local LLM     free, ~1s        catches what heuristics miss
3. Cheap API            ~$0.0001         Gemini Flash / GPT-4o-mini fallback
```

| Prompt | Classified as | Routed to |
|--------|--------------|-----------|
| "What does os.path.join do?" | query/simple | Gemini Flash ($0.000001) |
| "Fix the bug in auth.py" | code/moderate | Haiku / Sonnet |
| "Design the full auth system" | code/complex | Sonnet / Opus |
| "Research latest AI funding" | research | Perplexity Sonar Pro |
| "Generate a hero image" | image | Flux Pro via fal.ai |

**Free-first chain** (subscription mode): Ollama → Codex (free via OpenAI sub) → paid API

---

## MCP Tools

46 tools across 6 categories:

### Smart Routing
| Tool | What it does |
|------|-------------|
| `llm_route` | Auto-classify prompt → route to best model |
| `llm_auto` | Route + server-side savings tracking — designed for hook-less hosts (Codex CLI, Claude Desktop, Copilot) |
| `llm_classify` | Classify complexity + recommend model |
| `llm_select_agent` | Pick agent CLI (claude_code / codex) + model for a session |
| `llm_stream` | Stream LLM response for long-running tasks |
| `llm_reroute` | Correct a bad routing decision in-session and train the router |

### Text & Code
| Tool | What it does |
|------|-------------|
| `llm_query` | General questions — routed to cheapest capable model |
| `llm_research` | Web-grounded answers via Perplexity Sonar |
| `llm_generate` | Creative writing, summaries, brainstorming |
| `llm_analyze` | Deep reasoning — analysis, debugging, design review |
| `llm_code` | Code generation, refactoring, algorithms |
| `llm_edit` | Route edit reasoning to cheap model → returns `{file, old, new}` patch pairs |

### Filesystem
| Tool | What it does |
|------|-------------|
| `llm_fs_find` | Describe files to find → cheap model returns glob/grep commands |
| `llm_fs_rename` | Describe a rename → returns `mv`/`git mv` commands (dry_run by default) |
| `llm_fs_edit_many` | Bulk edits across files → returns all patch pairs |
| `llm_fs_analyze_context` | Summarise workspace context for smarter routing |

### Media
| Tool | What it does |
|------|-------------|
| `llm_image` | Image generation — Flux, DALL-E, Gemini Imagen |
| `llm_video` | Video generation — Runway, Kling, Veo 2 |
| `llm_audio` | TTS/voice — ElevenLabs, OpenAI |

### Orchestration
| Tool | What it does |
|------|-------------|
| `llm_orchestrate` | Multi-step pipeline across multiple models |
| `llm_pipeline_templates` | List available pipeline templates |

### Monitoring & Admin
| Tool | What it does |
|------|-------------|
| `llm_usage` | Unified dashboard — Claude sub, Codex, APIs, savings |
| `llm_savings` | Cross-session savings breakdown by period, host, and task type |
| `llm_check_usage` | Live Claude subscription usage (session %, weekly %) |
| `llm_health` | Provider availability + circuit breaker status |
| `llm_providers` | List all configured providers and models |
| `llm_set_profile` | Switch profile: `budget` / `balanced` / `premium` |
| `llm_setup` | Interactive provider wizard — add keys, validate, install hooks |
| `llm_quality_report` | Routing accuracy, savings metrics, classifier stats |
| `llm_rate` | Rate last response 👍/👎 — logged for quality tracking |
| `llm_codex` | Route task to local Codex desktop agent (free) |
| `llm_save_session` | Persist session summary for cross-session context |
| `llm_cache_stats` | Cache hit rate, entries, evictions |
| `llm_cache_clear` | Clear classification cache |
| `llm_refresh_claude_usage` | Force-refresh subscription data via OAuth |
| `llm_update_usage` | Feed usage data from claude.ai into the router |
| `llm_track_usage` | Report Claude Code token usage for budget tracking |
| `llm_dashboard` | Open web dashboard at localhost:7337 |
| `llm_team_report` | Team-wide routing savings report |
| `llm_team_push` | Push local savings data to shared team store |
| `llm_policy` | Show active org/repo routing policy + last 10 policy decisions |
| `llm_digest` | Savings digest with spend-spike detection; push to Slack/Discord webhook |
| `llm_benchmark` | Per-task-type routing accuracy from `llm_rate` feedback |
| `llm_session_spend` | Real-time API spend breakdown for the current session |
| `llm_approve_route` | Approve or reject a pending high-cost routing call |
| `llm_budget` | Budget Oracle — real-time spend vs. cap per provider with pressure bars |

---

## Routing Profiles

Three profiles — switch anytime with `llm_set_profile`:

| Profile | Use case | Chain |
|---------|----------|-------|
| `budget` | Dev, drafts, exploration | Ollama → Haiku → Gemini Flash |
| `balanced` | Production work *(default)* | Codex → Sonnet → GPT-4o |
| `premium` | Critical tasks, max quality | Codex → Opus → o3 |

Profile is overridden by complexity: simple prompts always use the budget chain, complex ones escalate to premium, regardless of the active profile setting.

---

## Providers

| Provider | Models | Free tier | Best for |
|----------|--------|-----------|----------|
| **Ollama** | Any local model | Yes (forever) | Privacy, zero cost, offline |
| **Google Gemini** | 2.5 Flash, 2.5 Pro | Yes (1M tokens/day) | Generation, long context |
| **Groq** | Llama 3.3, Mixtral | Yes | Ultra-fast inference |
| **OpenAI** | GPT-4o, o3, DALL-E | No | Code, reasoning, images |
| **Perplexity** | Sonar, Sonar Pro | No | Research, current events |
| **Anthropic** | Haiku, Sonnet, Opus | No | Writing, analysis, safety |
| **DeepSeek** | V3, Reasoner | Limited | Cost-effective reasoning |
| **Mistral** | Large, Small | Limited | Multilingual |
| **fal.ai** | Flux, Kling, Veo | No | Images, video, audio |
| **ElevenLabs** | Voice models | Limited | High-quality TTS |
| **Runway** | Gen-3 | No | Professional video |

Full setup guides: [docs/PROVIDERS.md](docs/PROVIDERS.md)

---

## Works With

### Claude Code

Auto-installed by `llm-router install`. Hooks intercept every prompt — you never need to call tools manually unless you want explicit control.

```bash
pipx install claude-code-llm-router && llm-router install
```

**Live status bar** shows routing stats before every prompt and in the persistent bottom statusline:
```
📊  CC 13%s · 24%w  │  sub:0 · free:305 · paid:27  │  $1.59 saved (35%)
```

### claw-code

Add to `~/.claw-code/mcp.json`:
```json
{
  "mcpServers": {
    "llm-router": { "command": "llm-router", "args": [] }
  }
}
```

Every API call in claw-code is paid — the free-first chain (Ollama → Codex → Gemini Flash) saves more here than in Claude Code.

### Cursor / Windsurf / Zed

Add to your IDE's MCP config:
```json
{
  "mcpServers": {
    "llm-router": { "command": "llm-router", "args": [] }
  }
}
```

### Agno (multi-agent)

Two integration modes:

**Option 1 — RouteredModel** (v2.0+): use llm-router as a first-class Agno model. Every agent call is automatically routed to the cheapest capable provider.

```bash
pip install "claude-code-llm-router[agno]"
```

```python
from agno.agent import Agent
from llm_router.integrations.agno import RouteredModel, RouteredTeam

# Single agent — routes each call intelligently
coder = Agent(
    model=RouteredModel(task_type="code", profile="balanced"),
    instructions="You are a coding assistant.",
)
coder.print_response("Write a Python quicksort.")

# Multi-agent team with shared $20/month budget cap
# Automatically downshifts to 'budget' profile at 80% spend
team = RouteredTeam(
    members=[coder, researcher],
    monthly_budget_usd=20.0,
    downshift_at=0.80,
)
```

**Option 2 — MCP tools**: use llm-router's 45 tools in any Agno agent:

```python
from agno.agent import Agent
from agno.models.anthropic import Claude
from agno.tools.mcp import MCPTools

agent = Agent(
    model=Claude(id="claude-sonnet-4-6"),
    tools=[MCPTools(command="llm-router")],
    instructions="Use llm_research for web searches, llm_code for coding tasks.",
)
```

### Supported Hosts

| Host | Install command | Writes files | Hook support |
|------|----------------|:------------:|:------------:|
| <img src="https://img.shields.io/badge/-Claude_Code-191919?logo=anthropic&logoColor=white" height="18"/> **Claude Code** | `llm-router install` | ✅ | ✅ Full auto-route |
| <img src="https://img.shields.io/badge/-Codex_CLI-412991?logo=openai&logoColor=white" height="18"/> **Codex CLI** | `llm-router install --host codex` | ✅ | ✅ PostToolUse |
| <img src="https://img.shields.io/badge/-OpenCode-0080FF?logo=codesandbox&logoColor=white" height="18"/> **OpenCode** | `llm-router install --host opencode` | ✅ | ✅ PostToolUse |
| <img src="https://img.shields.io/badge/-Gemini_CLI-4285F4?logo=google&logoColor=white" height="18"/> **Gemini CLI** | `llm-router install --host gemini-cli` | ✅ | ✅ Extension hook |
| <img src="https://img.shields.io/badge/-GitHub_Copilot-24292e?logo=github&logoColor=white" height="18"/> **GitHub Copilot CLI** | `llm-router install --host copilot-cli` | ✅ | — |
| <img src="https://img.shields.io/badge/-OpenClaw-6B38CC?logo=claw&logoColor=white" height="18"/> **OpenClaw** | `llm-router install --host openclaw` | ✅ | — |
| <img src="https://img.shields.io/badge/-Trae_IDE-FF6B35?logo=jetbrains&logoColor=white" height="18"/> **Trae IDE** | `llm-router install --host trae` | ✅ | — |
| <img src="https://img.shields.io/badge/-Factory_Droid-1A1A2E?logo=robot&logoColor=white" height="18"/> **Factory Droid** | `llm-router install --host factory` | ✅ manifest | — (Claude Code compat) |
| <img src="https://img.shields.io/badge/-VS_Code-0078d4?logo=visual-studio-code&logoColor=white" height="18"/> **VS Code (MCP native)** | `llm-router install --host vscode` | ✅ | — |
| <img src="https://img.shields.io/badge/-Cursor-000000?logo=cursor&logoColor=white" height="18"/> **Cursor IDE** | `llm-router install --host cursor` | ✅ | — |
| **Claude Desktop** | `llm-router install --host desktop` | snippet | — |
| **GitHub Copilot (VS Code)** | `llm-router install --host copilot` | snippet | — |

All installs are idempotent — run any command twice safely.

### Codex CLI

```bash
llm-router install --host codex
```

Writes `~/.codex/config.yaml`, `~/.codex/hooks.json` (PostToolUse), and `~/.codex/instructions.md`.

```bash
codex plugin install llm-router   # or via Codex marketplace
```

### OpenCode

```bash
llm-router install --host opencode
```

Writes `~/.config/opencode/config.json` (MCP block), PostToolUse hook, and routing rules.

### Gemini CLI

```bash
llm-router install --host gemini-cli
```

Writes `~/.gemini/settings.json`, creates the `llm-router` extension with `gemini-extension.json` + `hooks.json`, and appends routing rules.

### GitHub Copilot CLI

```bash
llm-router install --host copilot-cli
```

Writes `~/.config/gh/copilot/mcp.json` and routing rules.

### OpenClaw

```bash
llm-router install --host openclaw
```

Writes `~/.openclaw/mcp.json` and routing rules.

### Trae IDE

```bash
llm-router install --host trae
```

Writes the platform-appropriate Trae config (`~/Library/Application Support/Trae/mcp.json` on macOS) and a `.rules` file in the current directory.

### Factory Droid

Factory Droid natively supports Claude Code plugin format (`.claude-plugin/`) — no extra setup needed:

```bash
factory plugin install ypollak2/llm-router
# or via Factory marketplace search: llm-router
```

The dedicated `.factory-plugin/` manifest is included for Factory marketplace discovery.

### Claude Desktop

```bash
llm-router install --host desktop
```

Prints the snippet for `claude_desktop_config.json`. No hooks in Desktop — use `llm_auto` for savings tracking.

### GitHub Copilot (VS Code)

```bash
llm-router install --host copilot
```

Prints the snippet for `.vscode/mcp.json` and a `copilot-instructions.md` template.

### All at once

```bash
llm-router install --host all   # installs/prints all hosts
```

### Docker / CI

```bash
RUN pip install claude-code-llm-router && llm-router install --headless
# Pass keys at runtime: docker run -e GEMINI_API_KEY=... your-image
```

---

## Configuration

```bash
# API keys — at least one required
GEMINI_API_KEY=AIza...              # free tier at aistudio.google.com
OPENAI_API_KEY=sk-proj-...
PERPLEXITY_API_KEY=pplx-...
ANTHROPIC_API_KEY=sk-ant-...        # skip if using Claude Code subscription
DEEPSEEK_API_KEY=...
GROQ_API_KEY=gsk_...
FAL_KEY=...                         # images, video, audio via fal.ai
ELEVENLABS_API_KEY=...

# Router
LLM_ROUTER_PROFILE=balanced         # budget | balanced | premium
LLM_ROUTER_MONTHLY_BUDGET=0         # USD, 0 = unlimited
LLM_ROUTER_CLAUDE_SUBSCRIPTION=false  # true = Claude Code Pro/Max
LLM_ROUTER_ENFORCE=enforce          # shadow | suggest | enforce (default: enforce)

# Ollama (local models)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_BUDGET_MODELS=gemma4:latest,qwen3.5:latest

# Spend limits
LLM_ROUTER_DAILY_SPEND_LIMIT=5.00   # USD, 0 = disabled

# Per-provider monthly budget caps (overridden by llm-router budget set)
LLM_ROUTER_BUDGET_OPENAI=20.0
LLM_ROUTER_BUDGET_GEMINI=5.0
LLM_ROUTER_BUDGET_GROQ=3.0
LLM_ROUTER_BUDGET_DEEPSEEK=3.0

# Enterprise integrations
HELICONE_API_KEY=sk-helicone-...         # enables Helicone routing properties
LLM_ROUTER_HELICONE_PULL=false           # pull spend from Helicone API
LLM_ROUTER_LITELLM_BUDGET_DB=/path/to/litellm.db  # LiteLLM Proxy DB
LLM_ROUTER_LITELLM_USER=my-team          # optional LiteLLM user/team filter
```

### Enforcement Modes

Choose how strict routing should be. The easiest way is `llm-router onboard`, which lets you pick a mode interactively.

| Mode | Behaviour | Best for |
|------|-----------|----------|
| `shadow` | Observe routing decisions, never blocks | Safest first install |
| `suggest` | Show route hints, allow direct answers | Low-friction adoption |
| `enforce` | Block routed violations until the route is followed | Maximum savings |

`LLM_ROUTER_ENFORCE=hard` is the strict compatibility alias for `enforce`. Legacy `soft` and `off` values are still supported for direct CLI or env-based control.

### Repo-level config (`.llm-router.yml`)

Commit a routing policy alongside your code — no env vars required:

```yaml
profile: balanced
enforce: suggest          # shadow | suggest | enforce
block_providers:
  - openai                # never use OpenAI in this repo

routing:
  code:
    model: ollama/qwen3.5:latest   # always use local model for code tasks
  research:
    provider: perplexity           # always use Perplexity for research

daily_caps:
  _total: 2.00            # global $2/day cap
  code: 0.50              # code tasks capped at $0.50/day
```

User-level overrides live in `~/.llm-router/routing.yaml` (same schema). Repo config wins.

Full reference: [.env.example](.env.example)

---

## Budget Control

### Per-Provider Caps (CLI)

Set monthly spend caps per provider — persisted to `~/.llm-router/budgets.json`:

```bash
llm-router budget list                  # show all providers with cap, spend, pressure
llm-router budget set openai 20         # $20/month cap for OpenAI
llm-router budget set gemini 5          # $5/month cap for Gemini
llm-router budget remove openai         # remove cap (reverts to env-var or unlimited)
```

Caps set via CLI take priority over `LLM_ROUTER_BUDGET_*` env vars. The router automatically routes away from providers approaching their caps.

### Via Env Vars

```bash
LLM_ROUTER_BUDGET_OPENAI=20.0          # $20/month cap for OpenAI
LLM_ROUTER_BUDGET_GEMINI=5.0           # $5/month cap for Gemini
LLM_ROUTER_BUDGET_GROQ=3.0
LLM_ROUTER_BUDGET_DEEPSEEK=3.0
LLM_ROUTER_BUDGET_TOGETHER=5.0
LLM_ROUTER_BUDGET_PERPLEXITY=10.0
LLM_ROUTER_BUDGET_MISTRAL=5.0
LLM_ROUTER_MONTHLY_BUDGET=50           # global cap across all providers
```

```
llm_usage("month")
→ Calls: 142 | Tokens: 320k | Cost: $3.42 | Budget: 6.8% of $50
```

The router tracks spend in SQLite across all providers and routes away from providers approaching their monthly caps.

---

## Dashboard

```bash
llm-router dashboard   # opens localhost:7337
```

Live view of routing decisions, cost trends, model distribution, and subscription pressure. Auto-refreshes every 30s.

Tabs: **Overview** · **Performance** · **Config** · **Logs** · **💰 Budget**

The Budget tab shows per-provider spend vs. cap with editable cap inputs — changes are persisted to `~/.llm-router/budgets.json` and take effect immediately.

### Prometheus metrics

```
GET http://localhost:7337/metrics
```

Exposes standard text exposition format, no extra dependencies:

```
# HELP llm_router_spend_usd Monthly spend per provider in USD
llm_router_spend_usd{provider="openai"} 3.42
llm_router_budget_pressure{provider="openai"} 0.17
llm_router_budget_cap_usd{provider="openai"} 20.0
llm_router_savings_usd_total 12.87
```

---

## Enterprise Integrations

### Helicone

Add observability headers to every routed call and optionally pull spend data back from the Helicone API:

```bash
HELICONE_API_KEY=sk-helicone-...      # enables routing properties in every call
LLM_ROUTER_HELICONE_PULL=true         # pull spend per provider from Helicone API
```

When `LLM_ROUTER_HELICONE_PULL=true`, Helicone spend is merged with local SQLite spend and LiteLLM spend — budget pressure uses the **maximum** across all sources.

Headers injected on every call:
```
Helicone-Auth: Bearer sk-helicone-...
Helicone-Property-Router-Task-Type: code
Helicone-Property-Router-Model: openai/gpt-4o
Helicone-Property-Router-Complexity: moderate
Helicone-Property-Router-Profile: balanced
```

### LiteLLM BudgetManager

Teams running a LiteLLM Proxy with budget management can point llm-router at the proxy's SQLite DB:

```bash
LLM_ROUTER_LITELLM_BUDGET_DB=/path/to/litellm.db
LLM_ROUTER_LITELLM_USER=my-team        # optional — filter by user/team key
```

The router reads `spend_logs` aggregated by provider (e.g. `openai/gpt-4o` → `openai`) and incorporates LiteLLM spend into pressure calculations. This ensures routing pressure reflects traffic flowing through both direct llm-router calls and the LiteLLM proxy simultaneously.

Budget cap lookup from `budget_limits` table (LiteLLM Proxy ≥ 1.30) is also supported.

---

## Session Summary

At session end the router prints a breakdown:
```
  Free models  305 calls  ·  $0.52 saved  (Ollama / Codex)
  External       27 calls  ·  $0.006       (Gemini Flash, GPT-4o)
  💡 Saved ~$0.53 this session
```

Share your savings:
```bash
llm-router share   # copies savings card to clipboard + opens tweet
```

---

## Roadmap

**Positioning**: *Route every AI coding task to the cheapest capable model. Works across Claude Code, Cursor, VS Code, Codex, Gemini CLI, and more.*

### Phase 1 — Foundation ✅ Complete

| Version | Headline |
|---------|----------|
| v1.3–v2.0 | Foundation, dashboard, enforcement, Agno adapter |
| **v2.1** | **Route Simulator** — `llm-router test "<prompt>"` dry-run + `llm_savings` dashboard |
| **v2.2** | **Explainable Routing** — `LLM_ROUTER_EXPLAIN=1`, "why not Opus?", per-decision reasoning |
| **v2.3** | **Zero-Friction Activation** — onboarding wizard, shadow/suggest/enforce modes, yearly savings projection |

### Phase 2 — Smarter Routing ✅ Complete

| Version | Headline |
|---------|----------|
| **v2.4** | **Repo-Aware YAML Config** — `.llm-router.yml` committed with the codebase, block_providers, model pins |
| **v2.5** | **Context-Aware Routing** — "yes/ok/go ahead" inherits prior turn's route, zero classifier latency |
| **v2.6** | **Latency + Personalized Routing** — p95 latency scoring, per-user acceptance signals |

### Phase 3 — Team Infrastructure ✅ Complete

| Version | Headline |
|---------|----------|
| **v3.0** | **Team Dashboard** — shared savings across the whole team |
| **v3.1** | **Multi-Host + Cross-Session Savings** — `llm_auto`, Codex/Desktop/Copilot adapters, persistent savings across sessions |
| **v3.2** | **Policy Engine** — org/project/user routing policy, spend caps, audit log |
| **v3.3** | **Slack Digests + Codex Plugin** — weekly savings digest, spend-spike alerts, Codex marketplace plugin |

### Phase 4 — Multi-Host Expansion ✅ Complete

| Version | Headline |
|---------|----------|
| **v3.4** | **Agent-Context Routing** — subscription-first chain reordering when Codex or Claude Code is active |
| **v3.5** | **Multi-Agent CLI Compatibility** — OpenCode, Gemini CLI, Copilot CLI, OpenClaw, Factory Droid, Trae |
| **v3.6** | **VS Code + Cursor IDE Support** — native MCP config, routing rules, idempotent install |
| **v4.0** | **Token Efficiency + Real-Time Spend** — tool slim mode, session spend meter, reroute learning, quickstart wizard |
| **v4.1** | **Playwright DOM Compression + Enforcement Fixes** — DOM compression hook, PostToolUse MCP matcher fix, smart enforcement default |
| **v4.2** | **Quota-Aware Routing + Context-Aware Classification** — Ollama-first CC-mode for simple tasks, qwen3.5:32b in BALANCED chains, short code follow-up context inheritance |

### What's Next

| Version | Headline | Status |
|---------|----------|--------|
| **v4.3** | **OTEL / Prometheus Export** — metrics endpoint for routing decisions, cost, and fallback rates | 📅 Planned |
| **v5.0** | **Learned Routing** — self-training classifier from `llm_rate` feedback; personal routing patterns | 📅 Planned |

---

## Development

```bash
uv sync --extra dev
uv run pytest tests/ -q --ignore=tests/test_integration.py
uv run ruff check src/ tests/
```

See [CLAUDE.md](CLAUDE.md) for architecture and module layout.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Key areas: new provider integrations, routing intelligence, MCP client testing.

---

## License

[MIT](LICENSE)

---

<sub>Built with <a href="https://litellm.ai">LiteLLM</a> and <a href="https://modelcontextprotocol.io">MCP</a></sub>
