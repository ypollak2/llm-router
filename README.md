<p align="center">
  <img src="docs/logo.svg" alt="LLM Router" width="160" />
</p>

<h1 align="center">LLM Router</h1>

<p align="center">
  <strong>One MCP server. Every AI model. Smart routing.</strong>
</p>

<p align="center">
  Route text, image, video, and audio tasks to 20+ AI providers — automatically picking the best model for the job based on your budget and active profile.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#how-it-works">How It Works</a> &bull;
  <a href="#providers">Providers</a> &bull;
  <a href="#mcp-tools">Tools</a> &bull;
  <a href="#configuration">Configuration</a> &bull;
  <a href="docs/PROVIDERS.md">Provider Setup</a>
</p>

<p align="center">
  <a href="https://github.com/ypollak2/llm-router/actions"><img src="https://img.shields.io/github/actions/workflow/status/ypollak2/llm-router/ci.yml?style=flat-square&label=tests" alt="Tests"></a>
  <a href="https://github.com/ypollak2/llm-router/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.10%E2%80%933.13-blue?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/MCP-1.0+-purple?style=flat-square" alt="MCP">
  <img src="https://img.shields.io/badge/providers-20+-orange?style=flat-square" alt="Providers">
  <a href="https://pypi.org/project/claude-code-llm-router/"><img src="https://img.shields.io/pypi/v/claude-code-llm-router?style=flat-square&label=PyPI" alt="PyPI"></a>
  <a href="https://pypi.org/project/claude-code-llm-router/"><img src="https://img.shields.io/pypi/dm/claude-code-llm-router?style=flat-square&label=downloads" alt="PyPI Downloads"></a>
  <a href="https://smithery.ai/server/llm-router"><img src="https://smithery.ai/badge/llm-router" alt="Smithery"></a>
  <a href="https://github.com/ypollak2/llm-router/stargazers"><img src="https://img.shields.io/github/stars/ypollak2/llm-router?style=flat-square&label=stars&color=yellow" alt="GitHub Stars"></a>
</p>

<p align="center">
  <b>If llm-router saves you money, please <a href="https://github.com/ypollak2/llm-router">⭐ star the repo</a> — it helps others find it.</b>
</p>

<p align="center">
  <img src="docs/images/demo.svg" alt="LLM Router demo — animated terminal" width="860" />
</p>

---

## The Problem

You use Claude Code. You also have GPT-4o, Gemini, Perplexity, DALL-E, Runway, ElevenLabs — but switching between them is manual, slow, and expensive.

**LLM Router** gives your AI assistant one unified interface to all of them — and automatically picks the right one based on what you're doing and what you can afford.

```
You:     "Research the latest AI funding rounds"
Router:  → Perplexity Sonar Pro (search-augmented, best for current facts)

You:     "Generate a hero image for the landing page"
Router:  → Flux Pro via fal.ai (best quality/cost for images)

You:     "Write unit tests for the auth module"
Router:  → Claude Sonnet (top coding model, within budget)

You:     "Create a 5-second product demo clip"
Router:  → Kling 2.0 via fal.ai (best value for short video)
```

### How It Saves You Money

Not every task needs the same model. Without a router, everything goes to the same expensive model — like hiring a surgeon to change a lightbulb.

```
"What does os.path.join do?"     → Gemini Flash    ($0.000001 — literally free)
"Refactor the auth module"       → Claude Sonnet   ($0.003)
"Design the full system arch"    → Claude Opus     ($0.015)
```

| Task type | Without Router | With Router | Savings |
|-----------|---------------|-------------|---------|
| Simple queries (60% of work) | Opus — $0.015 | Haiku/Gemini Flash — $0.0001 | **99%** |
| Moderate tasks (30% of work) | Opus — $0.015 | Sonnet — $0.003 | **80%** |
| Complex tasks (10% of work) | Opus — $0.015 | Opus — $0.015 | 0% |
| **Blended monthly estimate** | **~$50/mo** | **~$8–15/mo** | **70–85%** |

> 💡 **With Ollama**: simple tasks route to a free local model — those 60% of queries cost **$0**.

---

## Quick Start

> **Zero API keys required** — if you have a Claude Code subscription, the router works out of the box. Simple tasks route to Claude Haiku (included), complex ones escalate to Sonnet/Opus. External providers (GPT-4o, Gemini, Perplexity) are optional add-ons.

### One-line install

```bash
pipx install claude-code-llm-router && llm-router install
```

Or with pip:

```bash
pip install claude-code-llm-router && llm-router install
```

### Option B: Claude Code Plugin

```bash
claude plugin add ypollak2/llm-router
```

### Option C: Manual Install

```bash
git clone https://github.com/ypollak2/llm-router.git
cd llm-router
uv sync
```

### Works with Claude Code, claw-code, OpenClaw, Cursor, Windsurf, Zed, and Agno

LLM Router is an MCP server — it works in any harness or framework that supports the Model Context Protocol.

#### Agent harnesses

**Claude Code** — auto-installed by `llm-router install`. Hooks intercept every prompt.

**claw-code** (open-source Rust rewrite of Claude Code) — add to `~/.claw-code/mcp.json`:
```json
{
  "mcpServers": {
    "llm-router": {
      "command": "llm-router",
      "args": []
    }
  }
}
```
> No subscription pressure in claw-code — every call is a paid API call, so the free-first chain (Ollama → Codex → Gemini Flash) saves even more here than in Claude Code.

**OpenClaw** (multi-provider Claude Code alternative) — add to OpenClaw's MCP config or install as a Skill:
```bash
# Via MCP config (immediate)
openclaw mcp add llm-router

# Via Skills (coming in v1.9)
openclaw skill add llm-router
```

#### IDEs

**Cursor** — add to `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "llm-router": {
      "command": "llm-router",
      "args": []
    }
  }
}
```

**Windsurf** — add to `~/.codeium/windsurf/mcp_config.json`:
```json
{
  "mcpServers": {
    "llm-router": {
      "command": "llm-router",
      "args": []
    }
  }
}
```

**Zed** — add to Zed's `settings.json`:
```json
{
  "context_servers": {
    "llm-router": {
      "command": {
        "path": "llm-router",
        "args": []
      }
    }
  }
}
```

#### Agno (multi-agent framework)

[Agno](https://github.com/agno-agi/agno) agents can consume llm-router's 33 tools as an MCP tool provider — no extra setup beyond the standard MCP config:

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

> **v2.0 roadmap**: `RouteredModel` — a drop-in Agno model that routes each prompt to the cheapest capable model automatically, and `RouteredTeam` with a `monthly_budget_usd` cap enforced across all agents in the team.

> The MCP tools (`llm_query`, `llm_code`, `llm_research`, etc.) work identically across all harnesses and IDEs. The auto-route hook is Claude Code / claw-code specific; other environments call the tools directly.

### Enable Global Auto-Routing

Make the router evaluate **every prompt** across all projects:

```bash
# From the MCP tool:
llm_setup(action='install_hooks')

# Or from the CLI:
llm-router install
```

This installs hooks + rules to `~/.claude/` and registers the MCP server in both `~/.claude/settings.json` (interactive) and `~/.claude.json` (CLI / `claude -p`), so routing works in every mode — interactive, non-interactive, and agent.

#### Docker / CI / Agent mode

```bash
# In your Dockerfile or CI setup:
RUN pip install claude-code-llm-router && llm-router install --headless
```

`--headless` skips interactive prompts, prints a ready-to-merge `settings.json` snippet, and works without the `claude` CLI present. Pass API keys at runtime:

```bash
# docker run -e GEMINI_API_KEY=... -e OPENAI_API_KEY=... your-image
# No LLM_ROUTER_CLAUDE_SUBSCRIPTION needed — API-key mode routes free-first automatically
```

> **Start for free**: Google's Gemini API has a [free tier](https://aistudio.google.com/apikey) with 1M tokens/day. [Groq](https://console.groq.com/keys) also offers a generous free tier with ultra-fast inference.

### What You Get

- **34 MCP tools** — smart routing, text/code/filesystem, image/video/audio, streaming, orchestration, usage monitoring, web dashboard
- **Auto-route hook** — intercepts every prompt before your top-tier model sees it; heuristic → Ollama → cheap API classifier chain, hooks self-update on `pip upgrade`; works in interactive, `claude -p`, and Docker/agent mode
- **Routing enforcement** — `enforce-route.py` PreToolUse hook blocks Write/Edit/Bash before `llm_*` is called; set `LLM_ROUTER_ENFORCE=hard` to hard-block violations; defaults to `soft` (logs only)
- **Live status bar + persistent statusline** — `📊 CC 13%s · 24%w │ sub:0 · free:15 · paid:27 │ $0.52 saved (35%)` fires before every prompt and stays visible in the bottom status bar throughout the session
- **Claude subscription mode** — routes entirely within your CC subscription; Codex (free) before paid externals; external only when quota exhausted
- **Anthropic prompt caching** — auto-injects `cache_control` breakpoints on long system prompts; up to 90% savings on repeated context
- **Semantic dedup cache** — Ollama embeddings + cosine similarity skip identical-intent calls at zero cost
- **Web dashboard** — `llm-router dashboard` → `localhost:7337`; cost trends, model distribution, recent decisions
- **Hard spend caps** — `LLM_ROUTER_DAILY_SPEND_LIMIT` and `LLM_ROUTER_MONTHLY_BUDGET` raise before any call
- **Filesystem tools** — `llm_fs_find`, `llm_fs_rename`, `llm_fs_edit_many` route file-operation reasoning to cheap models so Opus never burns tokens on grep patterns
- **`llm-router share`** — one command generates a shareable savings card, copies it to clipboard, and opens a pre-filled tweet with your real numbers
- **Prompt classification cache** — SHA-256 LRU cache for instant repeat classifications
- **Circuit breaker + health** — catches 429s, marks unhealthy providers, auto-recovers
- **Quality logging** — records every routing decision; `llm_quality_report` shows accuracy, savings, downshift rate
- **Cross-platform** — macOS, Linux, Windows (desktop notifications, background processes, path handling)

---

## Dashboard

The built-in web dashboard (`llm_dashboard` or `llm-router dashboard`) gives you a live view of routing decisions, cost trends, and subscription pressure.

| Overview | Performance |
|---|---|
| ![Overview](docs/images/dashboard-overview.png) | ![Performance](docs/images/dashboard-performance.png) |

| Logs & Analysis |
|---|
| ![Logs](docs/images/dashboard-logs.png) |

> **Design:** Liquid Glass dark theme — Inter + JetBrains Mono, Material Symbols, Tailwind CSS. Auto-refreshes every 30 s.

### Share Your Savings

```bash
llm-router share
```

Generates a savings card, copies it to clipboard, and opens a one-click tweet:

```
  ┌──────────────────────────────────────────────────────┐
  │                                                      │
  │   🤖 llm-router saved me $18.40 (lifetime)           │
  │      35% cheaper than always-Sonnet                  │
  │                                                      │
  │   2,110 total calls tracked                          │
  │   320 free  (Ollama / Codex)  ·  1,790 paid API      │
  │   Top model: gemini-2.5-flash                        │
  │                                                      │
  │   ⭐ github.com/ypollak2/llm-router                   │
  │                                                      │
  └──────────────────────────────────────────────────────┘

  ✓  Card copied to clipboard
  →  Tweet it: https://twitter.com/intent/tweet?text=...
```

### Status Bar

Two places show the same stats — before every prompt, and in the persistent bottom statusline:

**Before every prompt** (UserPromptSubmit hook):
```
📊  CC 13%s · 24%w · 43%♪   │   sub:0 · free:305 · paid:1813   │   $1.59 saved (35%)
```

**Persistent statusline** (Claude Code bottom bar, always visible):
```
…/Projects/my-app  main | claude-sonnet-4-6 | 📊  CC 13%s · 24%w · 43%♪   │   sub:0 · free:305 · paid:1813   │   $1.59 saved (35%)
```

`%s` = session usage · `%w` = weekly usage · `%♪` = Sonnet monthly · `sub` = CC subscription calls · `free` = Ollama/Codex ($0) · `paid` = external API calls

### Session Summary

At session end the Stop hook prints a full breakdown:

```
────────────────────────────────────────────────────────────────
  Claude Code subscription  (live)

  session (5h)     ████░░░░░░░░░░░░░░░░  13.0% → 21.0%  (+8.0pp)
  weekly (all)     █████░░░░░░░░░░░░░░░  24.0% → 24.0%  (+0.0pp)
  weekly sonnet    █████████░░░░░░░░░░░  43.0% → 44.0%  (+1.0pp)

  Free models  305 calls  ·  $0.52 saved vs Sonnet  (Ollama/Codex)

  codex        298×  29k↑ 28k↓ ~est   $0.51 saved
  ollama         7×  360↑ 719↓        $0.01 saved

  External routing  14 calls  ·  $0.006  ·  29% saved vs Sonnet

  query           7×  gemini-2.5-flash   $0.004
  code            3×  gpt-5.4            $0.000
  research        2×  gpt-4o             $0.002

  💡 Saved ~$0.53 with llm-router · github.com/ypollak2/llm-router
────────────────────────────────────────────────────────────────
```

---

## How It Works

### Auto-Route Hook — Every Prompt, Cheaper Model First

The `UserPromptSubmit` hook intercepts **all prompts** before your top-tier model sees them.

| Prompt | Classified as | Model used |
|--------|---------------|------------|
| `why doesn't the router work?` | `analyze/moderate` | Haiku |
| `how does benchmarks.py work?` | `query/simple` | Ollama / Haiku |
| `fix the bug in profiles.py` | `code/moderate` | Haiku / Sonnet |
| `implement a distributed cache` | `code/complex` | Sonnet / Opus |
| `write a blog post about LLMs` | `generate/moderate` | Haiku / Gemini Flash |
| `git status` (raw shell command) | *(skipped — terminal op)* | — |

Classification chain (stops at first success):

```
1. Heuristic scoring    instant, free   → high-confidence patterns route immediately
2. Ollama local LLM     free, ~1s       → catches what heuristics miss
3. Cheap API            ~$0.0001        → Gemini Flash / GPT-4o-mini fallback
4. Query catch-all      instant, free   → any remaining question → Haiku
```

Hook scripts are versioned and self-update — existing users get improvements automatically after `pip install --upgrade`.

### Claude Code Subscription Mode

If you use Claude Code Pro/Max, you already pay for Haiku, Sonnet, and Opus. Enable subscription mode and the router routes **within your subscription first** — Codex (free via OpenAI subscription) before any paid API call, external only when quota is exhausted.

```bash
# In .env
LLM_ROUTER_CLAUDE_SUBSCRIPTION=true
```

#### Routing

All tasks route via MCP tools regardless of subscription mode. The free-first chain keeps costs low:

| Complexity | MCP tool | Chain |
|-----------|----------|-------|
| simple | `llm_query` | Ollama → Codex → Gemini Flash → Groq |
| moderate | `llm_analyze` / `llm_code` / `llm_generate` | Ollama → Codex → GPT-4o → Gemini Pro |
| complex | `llm_code` / `llm_analyze` | Ollama → Codex → o3 → Gemini Pro |
| research | `llm_research` | Perplexity (web-grounded) |

Setting `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` enables inline OAuth refresh to keep subscription usage data fresh for accurate session-end reporting — it does not change routing behaviour. Run `llm_check_usage` at session start or rely on auto-refresh (triggers when data is >30 min stale).

#### External Fallback Chains (free-first)

| Tier | Chain |
|---|---|
| BUDGET (simple) | Ollama → Codex/gpt-5.4 → Codex/o3 → Gemini Flash → Groq → GPT-4o-mini |
| BALANCED (moderate) | Ollama → Codex/gpt-5.4 → Codex/o3 → GPT-4o → Gemini Pro → DeepSeek |
| PREMIUM (complex) | Ollama → Codex/gpt-5.4 → Codex/o3 → o3 → Gemini Pro |

Live subscription status:

```
+----------------------------------------------------------+
|                Claude Subscription (Live)                |
+----------------------------------------------------------+
|   Session      [====........]  35%  resets in 3h 7m      |
|   Weekly (all) [===.........]  23%  resets Fri 01:00 PM  |
|   Sonnet only  [===.........]  26%  resets Wed 10:00 AM  |
+----------------------------------------------------------+
|   OK 35% pressure -- full model selection                |
+----------------------------------------------------------+
```

---

## Providers

| Provider | Models | Free Tier | Best For |
|----------|--------|-----------|----------|
| **🦙 Ollama** | Any local model | **Yes (free forever)** | Privacy, zero cost, offline |
| **Google Gemini** | 2.5 Pro, 2.5 Flash | **Yes** (1M tokens/day) | Generation, long context |
| **Groq** | Llama 3.3, Mixtral | **Yes** | Ultra-fast inference |
| **OpenAI** | GPT-4o, GPT-4o-mini, o3 | No | Code, analysis, reasoning |
| **Perplexity** | Sonar, Sonar Pro | No | Research, current events |
| **Anthropic** | Claude Sonnet, Haiku | No | Nuanced writing, safety |
| **Deepseek** | V3, Reasoner | Yes (limited) | Cost-effective reasoning |
| **Mistral** | Large, Small | Yes (limited) | Multilingual |
| **Together** | Llama 3, CodeLlama | Yes (limited) | Open-source models |
| **xAI** | Grok 3 | No | Real-time information |
| **Cohere** | Command R+ | Yes (trial) | RAG, enterprise search |

Image, video, and audio providers (fal.ai, Runway, Stability AI, ElevenLabs, etc.) — see [docs/PROVIDERS.md](docs/PROVIDERS.md) for full setup guides.

> 🦙 **Ollama** runs models locally — no API key, no cost, no data sent externally. [Setup guide →](docs/PROVIDERS.md#ollama--local-models-free-private)

---

## MCP Tools

Once installed, Claude Code gets these 33 tools:

| Tool | What It Does |
|------|-------------|
| **Smart Routing** | |
| `llm_classify` | Classify complexity + recommend model with time-aware budget pressure |
| `llm_route` | Auto-classify, then route to the best external LLM |
| `llm_select_agent` | Session-level routing — pick which agent CLI (claude_code / codex) + model to invoke for an entire session |
| `llm_track_usage` | Report Claude Code token usage for budget tracking |
| `llm_stream` | Stream LLM responses for long-running tasks |
| **Text & Code** | |
| `llm_query` | General questions — auto-routed to the best text LLM |
| `llm_research` | Search-augmented answers via Perplexity |
| `llm_generate` | Creative content — writing, summaries, brainstorming |
| `llm_analyze` | Deep reasoning — analysis, debugging, problem decomposition |
| `llm_code` | Coding tasks — generation, refactoring, algorithms |
| `llm_edit` | Route code-edit reasoning to a cheap model → returns exact `{file, old_string, new_string}` pairs |
| **Filesystem** | |
| `llm_fs_find` | Describe files to find; cheap model generates glob patterns and grep commands |
| `llm_fs_rename` | Describe a rename/reorganise; cheap model returns `mv`/`git mv` commands (`dry_run=True` by default) |
| `llm_fs_edit_many` | Bulk edit across multiple files via glob pattern — cheap model returns all `{file, old, new}` pairs |
| **Media** | |
| `llm_image` | Image generation — Gemini Imagen, DALL-E, Flux, or SD |
| `llm_video` | Video generation — Gemini Veo, Runway, Kling, etc. |
| `llm_audio` | Voice/audio — TTS via ElevenLabs or OpenAI |
| **Orchestration** | |
| `llm_orchestrate` | Multi-step pipelines across multiple models |
| `llm_pipeline_templates` | List available orchestration templates |
| **Monitoring & Setup** | |
| `llm_check_usage` | Check live Claude subscription usage (session %, weekly %) |
| `llm_update_usage` | Feed live usage data from claude.ai into the router |
| `llm_refresh_claude_usage` | Force-refresh Claude subscription data via OAuth |
| `llm_codex` | Route tasks to local Codex desktop agent (free, uses OpenAI sub) |
| `llm_setup` | Discover API keys, add providers, validate keys, install global hooks |
| `llm_rate` | Rate last response (👍/👎) — stored in `routing_decisions` for quality tracking |
| `llm_quality_report` | Routing accuracy, classifier stats, savings metrics, downshift rate |
| `llm_set_profile` | Switch routing profile (budget / balanced / premium) |
| `llm_usage` | Unified dashboard — Claude sub, Codex, APIs, savings in one view |
| `llm_health` | Check provider availability and circuit breaker status |
| `llm_providers` | List all supported and configured providers |
| `llm_cache_stats` | View cache hit rate, entries, memory estimate, evictions |
| `llm_cache_clear` | Clear the classification cache |
| **Session Memory** | |
| `llm_save_session` | Summarize + persist current session for cross-session context injection |

> **Context injection**: text tools (`llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`) automatically prepend recent conversation history to every external call — GPT-4o, Gemini, and Perplexity receive the same context you have. Controlled by `LLM_ROUTER_CONTEXT_ENABLED` (default: on).

---

## Routing Profiles

Three built-in profiles map to task complexity. Switch anytime:

```
llm_set_profile("budget")    # Development, drafts, exploration
llm_set_profile("balanced")  # Production work, client deliverables
llm_set_profile("premium")   # Critical tasks, maximum quality
```

| | Budget (simple) | Balanced (moderate) | Premium (complex) |
|--|--------|----------|---------|
| **Text** | Ollama → Haiku → Gemini Flash | Sonnet → GPT-4o → DeepSeek | Opus → Sonnet → o3 |
| **Code** | Ollama → Codex → DeepSeek → Haiku | Codex → Sonnet → GPT-4o | Codex → Opus → o3 |
| **Research** | Perplexity Sonar | Perplexity Sonar Pro | Perplexity Sonar Pro |
| **Image** | Flux Dev, Imagen Fast | Flux Pro, Imagen 3, DALL-E 3 | Imagen 3, DALL-E 3 |
| **Video** | minimax, Veo 2 | Kling, Veo 2, Runway Turbo | Veo 2, Runway Gen-3 |
| **Audio** | OpenAI TTS | ElevenLabs | ElevenLabs |

Model order is pressure-aware — as Claude quota is consumed, chains reorder to preserve remaining budget. See [BENCHMARKS.md](docs/BENCHMARKS.md) for how model quality scores drive rankings.

---

## Budget Control

```bash
# In .env
LLM_ROUTER_MONTHLY_BUDGET=50   # USD, 0 = unlimited
```

The router tracks real-time spend across all providers in SQLite and blocks requests when the monthly budget is reached.

```
llm_usage("month")
→ Calls: 142 | Tokens: 320,000 | Cost: $3.42 | Budget: 6.8% of $50
```

Per-provider budgets: `LLM_ROUTER_BUDGET_OPENAI=10.00`, `LLM_ROUTER_BUDGET_GEMINI=5.00`.

---

## Multi-Step Orchestration

Chain tasks across models in a pipeline:

```
llm_orchestrate("Research AI trends and write a report", template="research_report")
```

| Template | Pipeline |
|----------|----------|
| `research_report` | Research → Analyze → Write |
| `competitive_analysis` | Multi-source research → SWOT → Report |
| `content_pipeline` | Research → Draft → Review → Polish |
| `code_review_fix` | Review → Fix → Test |

---

## Configuration

```bash
# Required: at least one provider
GEMINI_API_KEY=AIza...         # Free tier! https://aistudio.google.com/apikey
OPENAI_API_KEY=sk-proj-...
PERPLEXITY_API_KEY=pplx-...

# Optional: more providers
ANTHROPIC_API_KEY=sk-ant-...
DEEPSEEK_API_KEY=...
GROQ_API_KEY=gsk_...
FAL_KEY=...
ELEVENLABS_API_KEY=...

# Router config
LLM_ROUTER_PROFILE=balanced               # budget | balanced | premium
LLM_ROUTER_MONTHLY_BUDGET=0              # USD, 0 = unlimited
LLM_ROUTER_CLAUDE_SUBSCRIPTION=false     # true = Claude Code Pro/Max user

# Ollama (two independent roles — classifier and task answerer)
LLM_ROUTER_OLLAMA_URL=http://localhost:11434    # hook classifier
LLM_ROUTER_OLLAMA_MODEL=qwen3.5:latest
OLLAMA_BASE_URL=http://localhost:11434          # router answerer
OLLAMA_BUDGET_MODELS=llama3.2,qwen2.5-coder:7b

# Smart routing (Claude Code model selection)
QUALITY_MODE=balanced          # best | balanced | conserve
MIN_MODEL=haiku                # floor: haiku | sonnet | opus
```

See [.env.example](.env.example) for the full list.

> **Ollama note**: `LLM_ROUTER_OLLAMA_URL` is for the hook classifier (classifying complexity); `OLLAMA_BASE_URL` is for the router answerer (actually answering tasks). Configuring one does not enable the other. See [docs/PROVIDERS.md](docs/PROVIDERS.md) for the full local-first setup.

---

## Development

```bash
uv sync --extra dev
uv run pytest tests/ -q --ignore=tests/test_integration.py
uv run ruff check src/ tests/
llm-router install   # deploy hooks to ~/.claude/
```

See [CLAUDE.md](CLAUDE.md) for architecture, module layout, and contribution guidelines.

---

## Roadmap

See [CHANGELOG.md](CHANGELOG.md) for what's been shipped. Coming next:

| Version | Theme | Headline features |
|---|---|---|
| ~~v1.3~~ | ~~Observability~~ | ✅ Web dashboard, prompt caching, semantic dedup cache, hard daily cap |
| ~~v1.4~~ | ~~Developer Ergonomics~~ | ✅ Real savings in `status`, `update` command, `uninstall --purge`, animated SVG demo |
| ~~v1.5~~ | ~~Filesystem + Transparency~~ | ✅ `llm_fs_*` tools, free-model savings in status + session summary, sub/free/paid call counts in status bar |
| ~~v1.6~~ | ~~Growth & Sharing~~ | ✅ `llm-router share` savings card + tweet, one-time star CTA in session summary |
| ~~v1.7~~ | ~~Ecosystem~~ | ✅ Multi-harness docs (claw-code, OpenClaw, Agno, Cursor, Windsurf, Zed) |
| ~~v1.8~~ | ~~Reliability~~ | ✅ Inline OAuth refresh (prevents session exhaustion), claw-code hooks, Docker/headless install, fix routing format drift + MCP CLI registration |
| ~~v1.9~~ | ~~Enforcement + Agent Selection~~ | ✅ `enforce-route.py` hook (`LLM_ROUTER_ENFORCE=hard` to block violations), `llm_select_agent` session-level routing for agent orchestrators |
| v2.0 | Learning Router | Self-improving classifier trained on your own routing history; routing dry-run (`llm-router test <prompt>`); Agno `RouteredModel` + `RouteredTeam` |

See [ROADMAP.md](ROADMAP.md) for design notes and competitive context.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Key areas: new provider integrations, routing intelligence, MCP client testing, documentation.

---

## License

[MIT](LICENSE) — use it however you want.

---

<p align="center">
  <sub>Built with <a href="https://litellm.ai">LiteLLM</a> and <a href="https://modelcontextprotocol.io">MCP</a></sub>
</p>
