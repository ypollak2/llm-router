<p align="center">
  <img src="docs/logo.svg" alt="LLM Router" width="120" />
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
  <a href="#routing-profiles">Profiles</a> &bull;
  <a href="#budget-control">Budget Control</a> &bull;
  <a href="docs/PROVIDERS.md">Provider Setup</a>
</p>

<p align="center">
  <a href="https://github.com/ypollak2/llm-router/actions"><img src="https://img.shields.io/github/actions/workflow/status/ypollak2/llm-router/ci.yml?style=flat-square&label=tests" alt="Tests"></a>
  <a href="https://github.com/ypollak2/llm-router/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.10%E2%80%933.13-blue?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/MCP-1.0+-purple?style=flat-square" alt="MCP">
  <img src="https://img.shields.io/badge/providers-20+-orange?style=flat-square" alt="Providers">
  <a href="https://pypi.org/project/claude-code-llm-router/"><img src="https://img.shields.io/pypi/v/claude-code-llm-router?style=flat-square&label=PyPI" alt="PyPI"></a>
</p>

---

## The Problem

You use Claude Code (or any MCP client). You also have access to GPT-4o, Gemini, Perplexity, DALL-E, Runway, ElevenLabs — but switching between them is manual, slow, and expensive.

**LLM Router** gives your AI assistant one unified interface to all of them — and it automatically picks the right one based on what you're doing and what you can afford.

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

### How It Saves You Real Money

Here's the key insight: **not every task needs the same model**.

When you use Claude Code without a router, every single request — whether it's "what does this function do?" or "redesign this entire architecture" — goes to the same expensive model. That's like hiring a surgeon to change a lightbulb.

LLM Router classifies each task automatically and sends it to the cheapest model that can handle it well:

```
"What does os.path.join do?"     → Gemini Flash    ($0.000001 — literally free)
"Refactor the auth module"       → Claude Sonnet   ($0.003)
"Design the full system arch"    → Claude Opus     ($0.015)
```

<p align="center">
  <img src="docs/images/savings.svg" alt="Task Distribution" width="400" />
</p>

| Task type | Without Router | With Router | Savings |
|-----------|---------------|-------------|---------|
| Simple queries (60% of work) | Opus — $0.015 | Haiku/Gemini Flash — $0.0001 | **99%** |
| Moderate tasks (30% of work) | Opus — $0.015 | Sonnet — $0.003 | **80%** |
| Complex tasks (10% of work) | Opus — $0.015 | Opus — $0.015 | 0% |
| **Blended monthly estimate** | **~$50/mo** | **~$8–15/mo** | **70–85%** |

> 💡 **With Ollama**: Route simple tasks to a free local model (`llama3.2`, `qwen2.5-coder`) and the savings become even more dramatic — those 60% of simple tasks cost **$0**.

The router pays for itself in the first hour of use.

---

## Quick Start

### Option A: PyPI (Recommended)

```bash
pip install claude-code-llm-router
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
./scripts/install.sh    # registers as MCP server in Claude Code
```

### Get Running in 3 Steps

<p align="center">
  <img src="docs/images/quickstart.svg" alt="Quick Start" width="700" />
</p>

### Enable Global Auto-Routing

Make the router evaluate **every prompt** across all projects:

```bash
# From the MCP tool:
llm_setup(action='install_hooks')

# Or from the CLI:
llm-router-install-hooks
```

This installs hooks + rules to `~/.claude/` so every Claude Code session auto-routes tasks to the optimal model.

> **Start for free**: Google's Gemini API has a [free tier](https://aistudio.google.com/apikey) with 1M tokens/day — no credit card needed. [Groq](https://console.groq.com/keys) also offers a generous free tier with ultra-fast inference.

### What You Get

- **24 MCP tools** — Smart routing, text, image, video, audio, streaming, setup, quality analytics, usage monitoring, cache management
- **`/route` skill** — Smart task classification and routing in one command
- **Smart classifier** — Auto-picks Claude Haiku/Sonnet/Opus based on complexity
- **Prompt classification cache** — SHA-256 exact-match LRU cache (1000 entries, 1h TTL) for instant repeat classifications
- **Auto-route hook** — Multi-layer `UserPromptSubmit` classifier: routes **every prompt** (including codebase questions) through Haiku/Ollama first; heuristic scoring (instant) → Ollama local LLM (free, ~1s) → cheap API (Gemini Flash/GPT-4o-mini, ~$0.0001) → auto fallback. Hooks self-update after `pip upgrade` — no reinstall needed.
- **Streaming responses** — `llm_stream` tool for long-running tasks, shows output as it arrives
- **Usage auto-refresh** — `PostToolUse` hook detects stale Claude subscription data (>15 min) and nudges for refresh
- **Savings awareness** — Every 5th routed task, shows estimated Claude API costs and rate limit capacity saved
- **Rate limit detection** — Catches 429/rate_limit errors with smart cooldowns (15s for rate limits vs 60s for hard failures)
- **Key validation** — `llm_setup(action='test')` validates API keys with minimal LLM calls (~$0.0001 each)
- **Claude subscription monitoring** — Live session/weekly usage from claude.ai
- **Codex desktop integration** — Route tasks to local OpenAI Codex (free)
- **LLM Orchestrator agent** — Autonomous multi-step task decomposition across models

---

## How It Works

### Architecture

<p align="center">
  <img src="docs/images/architecture.svg" alt="Architecture" width="700" />
</p>

### Routing Decision Flow

<p align="center">
  <img src="docs/images/routing-flow.svg" alt="Routing Flow" width="600" />
</p>

---

## Benchmark-Driven Routing

Model chains are ranked using weekly-refreshed data from four authoritative sources, so the router always sends your task to the current best model for that task type.

### Current Top Models by Task

| Task | 🥇 Premium | 🥈 Balanced | 🥉 Budget |
|------|-----------|------------|----------|
| 💻 Code | **DeepSeek-R1**, o3, Opus | **DeepSeek Chat**, GPT-4o, Sonnet | Flash, DeepSeek, Haiku |
| 🔍 Analyze | **DeepSeek-R1**, GPT-4o, Sonnet | **DeepSeek-R1**, GPT-4o, Gemini Pro | Flash, DeepSeek, Haiku |
| ❓ Query | **DeepSeek Chat**, GPT-4o, Gemini Pro | **DeepSeek Chat**, GPT-4o, Gemini Pro | Flash, DeepSeek, Haiku |
| ✍️ Generate | **DeepSeek Chat**, GPT-4o, Gemini Pro | **DeepSeek Chat**, GPT-4o, Gemini Pro | Flash, DeepSeek, Haiku |
| 🔎 Research | Perplexity Pro, Perplexity, GPT-4o | Perplexity Pro, Perplexity, GPT-4o | Perplexity, Flash, Haiku |

> **Bold** = first model tried when Claude quota is high (> 85%) or in subscription mode.
> **Full benchmark data, scoring weights, raw scores, and sources:** [docs/BENCHMARKS.md](docs/BENCHMARKS.md)
> 🔄 Updated every Monday via GitHub Actions — distributed to all users on next `pip upgrade`

### How rankings are computed

```
Arena Hard win-rate  ──┐
Aider code pass rate ──┼── weighted by task type ──► quality score ──► quality-cost tier sort
HuggingFace MMLU/MATH──┤                                                ↓
LiteLLM pricing     ──┘                             within 5% quality band → cheapest model first
```

**Quality-cost sorting**: models within 5% quality of each other are grouped into a tier. Within that tier, the cheapest model sorts first. This means GPT-4o ($0.006/1K) leads over Sonnet ($0.009/1K) when their quality difference is under 5%, and DeepSeek Chat ($0.0007/1K) leads over everyone when it's within the top quality band.

---

## Auto-Route Hook — Every Prompt, Cheaper Model First

The `UserPromptSubmit` hook intercepts **all prompts** — not just explicit routing requests — and classifies them before your top-tier model sees them. Simple tasks go straight to Haiku or a local Ollama model; only genuinely complex work escalates.

### What gets routed

| Prompt | Classified as | Model used |
|--------|---------------|------------|
| `why doesn't the router work?` | `analyze/moderate` | Haiku |
| `how does benchmarks.py work?` | `query/simple` | Ollama / Haiku |
| `fix the bug in profiles.py` | `code/moderate` | Haiku / Sonnet |
| `implement a distributed cache` | `code/complex` | Sonnet / Opus |
| `write a blog post about LLMs` | `generate/moderate` | Haiku / Gemini Flash |
| `git status` (raw shell command) | *(skipped — terminal op)* | — |

### Classification chain (stops at first success)

```
1. Heuristic scoring    instant, free   → high-confidence patterns route immediately
2. Ollama local LLM     free, ~1s       → catches what heuristics miss
3. Cheap API            ~$0.0001        → Gemini Flash / GPT-4o-mini fallback
4. Query catch-all      instant, free   → any remaining question → Haiku
```

### Self-updating hooks

Hook scripts are versioned (`# llm-router-hook-version: N`). On every MCP server startup, if the bundled version in the installed package is newer than what's in `~/.claude/hooks/`, it's automatically overwritten. **Existing users get classification improvements automatically after `pip install --upgrade claude-code-llm-router`** — no need to re-run `llm-router-install-hooks`.

---

## Claude Code Subscription Mode (Recommended)

If you use Claude Code, you already pay for Haiku, Sonnet, and Opus. Enable subscription mode and the router routes **entirely within your subscription** — zero API calls to Anthropic:

```bash
# In .env
LLM_ROUTER_CLAUDE_SUBSCRIPTION=true
```

### Default Routing Strategy (No Pressure)

| Complexity | Model | Cost |
|-----------|-------|------|
| simple | Claude Haiku 4.5 | free (subscription) |
| moderate | Sonnet (passthrough) | free (you're already using it) |
| complex | Claude Opus 4.6 | free (subscription) |
| research | Perplexity Sonar Pro | ~$0.005/query (web-grounded) |

### Pressure-Based External Fallback (Three Independent Buckets)

When Claude quota gets tight, external models activate tier by tier. Each threshold controls its own complexity tier — higher pressure cascades down:

| Condition | simple | moderate | complex |
|-----------|--------|----------|---------|
| `session < 85%` | Haiku (sub) | Sonnet (passthrough) | Opus (sub) |
| `session ≥ 85%` | Gemini Flash / Groq | Sonnet (passthrough) | Opus (sub) |
| `sonnet ≥ 95%` | Gemini Flash / Groq | GPT-4o / DeepSeek | Opus (sub) |
| `weekly ≥ 95%` OR `session ≥ 95%` | Gemini Flash / Groq | GPT-4o / DeepSeek | GPT-4o / DeepSeek |

> **Cascade rule**: once a higher tier goes external, all lower tiers go external too. When weekly ≥ 95%, everything routes external — it makes no sense to put simple tasks on external while complex stays on subscription.

Run `llm_check_usage` at session start to populate accurate pressure data. Routing hooks warn when usage data is >30 minutes old (stale quota → routing hints are flagged `⚠️ STALE`).

---

## Smart Routing (Claude Code Models)

Use Claude Code's own models (Haiku/Sonnet/Opus) **without extra API keys** via the smart classifier:

```
llm_classify("What is the capital of France?")
→ [S] simple (99%) → haiku

llm_classify("Write a REST API with auth and pagination")
→ [M] moderate (98%) → sonnet

llm_classify("Design a distributed CQRS architecture")
→ [C] complex (85%) → opus
```

### Complexity-First Routing

Complexity drives model selection — this is the real savings mechanism. You don't need opus for "what time is it?" and you don't want haiku for architecture design. Budget pressure is a late safety net, not the primary router.

```bash
# In .env
QUALITY_MODE=balanced        # best | balanced | conserve
MIN_MODEL=haiku              # floor: never route below this
```

| Claude Usage | Effect |
|-------------|--------|
| 0-85% | No downshift — complexity routing handles efficiency |
| 85-95% | Downshift by 1 tier + suggest external fallback |
| 95%+ | Downshift by 2 tiers + recommend external (Codex, OpenAI, Gemini) |

Budget pressure comes from **real Claude subscription data** (session %, weekly %) fetched live from claude.ai. The router also factors in **time until session reset** — if you're at 90% but the session resets in 5 minutes, no downshift needed.

### External Fallback

When Claude quota is tight (85%+), the router ranks available external models:

```
llm_classify("Design auth architecture")
# -> complex -> sonnet (downshifted from opus)
#    pressure: [========..] 90%
#    >> fallback: codex/gpt-5.4 (free, preserves Claude quota)
```

- **Codex (local)**: Free — uses your OpenAI desktop subscription
- **OpenAI API**: GPT-4o, o3 (ranked by quality, filtered by budget)
- **Gemini API**: gemini-2.5-pro, gemini-2.5-flash

Per-provider budgets via `LLM_ROUTER_BUDGET_OPENAI=10.00`, `LLM_ROUTER_BUDGET_GEMINI=5.00`.

### Claude Subscription Monitoring

Live usage data from your claude.ai account — no guessing:

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

Fetched via Playwright from claude.ai's internal JSON API (same data the settings page uses). One `browser_evaluate` call, cached in memory for routing decisions.

---

## Providers

### Text & Code LLMs

| Provider | Models | Free Tier | Best For |
|----------|--------|-----------|----------|
| **🦙 Ollama** | Any local model | **Yes (free forever)** | Privacy, zero cost, offline use |
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

> 🦙 **Ollama** runs models locally — no API key, no cost, no data sent externally. [Full Ollama setup guide →](docs/PROVIDERS.md#ollama--local-models-free-private)

### Image Generation

| Provider | Models | Best For |
|----------|--------|----------|
| **Google Gemini** | Imagen 3 | High quality, integrated with text models |
| **fal.ai** | Flux Pro, Flux Dev | Quality/cost ratio, fast generation |
| **OpenAI** | DALL-E 3, DALL-E 2 | Prompt adherence, text in images |
| **Stability AI** | Stable Diffusion 3 | Fine control, open weights |

### Video Generation

| Provider | Models | Best For |
|----------|--------|----------|
| **Google Gemini** | Veo 2 | Integrated with Gemini ecosystem |
| **Runway** | Gen-3 Alpha | Professional quality, motion control |
| **fal.ai** | Kling, minimax | Value, fast generation |
| **Replicate** | Various | Open-source video models |

### Audio & Voice

| Provider | Models | Best For |
|----------|--------|----------|
| **ElevenLabs** | Multilingual v2 | Voice cloning, highest quality |
| **OpenAI** | TTS-1, TTS-1-HD | Cost-effective text-to-speech |

> **20+ providers and growing.** See [docs/PROVIDERS.md](docs/PROVIDERS.md) for full setup guides with API key links.

---

## MCP Tools

Once installed, Claude Code gets these 25 tools:

| Tool | What It Does |
|------|-------------|
| **Smart Routing** | |
| `llm_classify` | Classify complexity + recommend model with time-aware budget pressure |
| `llm_route` | Auto-classify, then route to the best external LLM |
| `llm_track_usage` | Report Claude Code token usage for budget tracking |
| **Text & Code** | |
| `llm_query` | General questions — auto-routed to the best text LLM |
| `llm_research` | Search-augmented answers via Perplexity |
| `llm_generate` | Creative content — writing, summaries, brainstorming |
| `llm_analyze` | Deep reasoning — analysis, debugging, problem decomposition |
| `llm_code` | Coding tasks — generation, refactoring, algorithms |
| `llm_edit` | Route code-edit *reasoning* to a cheap model → returns exact `{file, old_string, new_string}` pairs for Claude to apply |
| **Media** | |
| `llm_image` | Image generation — Gemini Imagen, DALL-E, Flux, or SD |
| `llm_video` | Video generation — Gemini Veo, Runway, Kling, etc. |
| `llm_audio` | Voice/audio — TTS via ElevenLabs or OpenAI |
| **Orchestration** | |
| `llm_orchestrate` | Multi-step pipelines across multiple models |
| `llm_pipeline_templates` | List available orchestration templates |
| **Cache** | |
| `llm_cache_stats` | View cache hit rate, entries, memory estimate, evictions |
| `llm_cache_clear` | Clear the classification cache |
| **Streaming** | |
| `llm_stream` | Stream LLM responses for long-running tasks — output as it arrives |
| **Monitoring & Setup** | |
| `llm_check_usage` | Check live Claude subscription usage (session %, weekly %) |
| `llm_update_usage` | Feed live usage data from claude.ai into the router |
| `llm_codex` | Route tasks to local Codex desktop agent (free, uses OpenAI sub) |
| `llm_setup` | Discover API keys, add providers, get setup guides, validate keys, install global hooks |
| `llm_quality_report` | Routing accuracy, classifier stats, savings metrics, downshift rate |
| `llm_set_profile` | Switch routing profile (budget / balanced / premium) |
| `llm_usage` | Unified dashboard — Claude sub, Codex, APIs, savings in one view |
| `llm_health` | Check provider availability and circuit breaker status |
| `llm_providers` | List all supported and configured providers |
| **Session Memory** | |
| `llm_save_session` | Summarize + persist current session for cross-session context injection |

> **Context injection**: text tools (`llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`) automatically prepend recent conversation history and previous session summaries to every external LLM call — so GPT-4o, Gemini, and Perplexity receive the same context you have. Pass `context="..."` to add caller-supplied context on top. Controlled by `LLM_ROUTER_CONTEXT_ENABLED` (default: on).

---

## Routing Profiles

<p align="center">
  <img src="docs/images/profiles.svg" alt="Routing Profiles" width="700" />
</p>

Three built-in profiles map to task complexity. Model order is pressure-aware — the router
dynamically reorders chains based on live Claude subscription usage.

| | Budget (simple) | Balanced (medium) | Premium (complex) |
|--|--------|----------|---------|
| **Text** | Ollama → **Haiku** → cheap | **Sonnet** → DeepSeek → GPT-4o | **Opus** → Sonnet → o3 |
| **Research** | Perplexity Sonar | Perplexity Sonar Pro | Perplexity Sonar Pro |
| **Code** | Ollama → **Haiku** → DeepSeek | **Sonnet** → DeepSeek → GPT-4o | **Opus** → Sonnet → DeepSeek-R1 → o3 |
| **Image** | Flux Dev, Imagen Fast | Flux Pro, Imagen 3, DALL-E 3 | Imagen 3, DALL-E 3 |
| **Video** | minimax, Veo 2 | Kling, Veo 2, Runway Turbo | Veo 2, Runway Gen-3 |
| **Audio** | OpenAI TTS | ElevenLabs | ElevenLabs |

### Quota-aware chain reordering

Claude Pro/Max tokens are treated as free — the router uses them first. As quota is consumed,
chains automatically reorder to preserve remaining Claude budget:

| Claude usage | Chain order |
|---|---|
| **0–84%** | Claude first (free under subscription) |
| **85–98%** | DeepSeek/Codex → cheap externals → Claude last |
| **≥ 99% (hard cap)** | DeepSeek → Codex → cheap → paid — **zero Claude** |
| **Research (any)** | Perplexity always first (web-grounded) |

### Claude Code subscription mode

If you use Claude Code (Pro/Max), set `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` in `.env`. The router will **never route to Anthropic via API** — you're already on Claude, so API routing would require a separate key and add duplicate billing. Instead, every task routes to the best non-Claude alternative:

```bash
# In .env
LLM_ROUTER_CLAUDE_SUBSCRIPTION=true   # no ANTHROPIC_API_KEY needed
```

At normal quota (< 85%), chains lead with the highest-quality available model. At high quota (> 85%), DeepSeek takes over — quality 1.0 benchmark score at ~1/8th the cost of GPT-4o:

| | Low quota (< 85%) | High quota (> 85%) |
|--|---|---|
| **BUDGET/CODE** | DeepSeek Chat | DeepSeek Chat |
| **BALANCED/CODE** | DeepSeek Chat | DeepSeek Chat |
| **BALANCED/ANALYZE** | DeepSeek Reasoner | DeepSeek Reasoner |
| **PREMIUM/CODE** | o3 | DeepSeek Reasoner |
| **PREMIUM/ANALYZE** | DeepSeek Reasoner | DeepSeek Reasoner |

Switch profile anytime:
```
llm_set_profile("budget")    # Development, drafts, exploration
llm_set_profile("balanced")  # Production work, client deliverables
llm_set_profile("premium")   # Critical tasks, maximum quality
```

---

## Budget Control

Set a monthly budget to prevent overspending:

```bash
# In .env
LLM_ROUTER_MONTHLY_BUDGET=50   # USD, 0 = unlimited
```

The router:
- **Tracks real-time spend** across all providers in SQLite
- **Blocks requests** when the monthly budget is reached
- **Shows budget status** in `llm_usage`

```
llm_usage("month")

## Usage Summary (month)
Calls: 142
Tokens: 240,000 in + 80,000 out = 320,000 total
Cost: $3.4200
Avg latency: 1200ms

### Budget Status
Monthly budget: $50.00
Spent this month: $3.4200 (6.8%)
Remaining: $46.5800
```

---

## Multi-Step Orchestration

Chain tasks across different models in a pipeline:

<p align="center">
  <img src="docs/images/orchestration.svg" alt="Orchestration Pipeline" width="600" />
</p>

```
llm_orchestrate("Research AI trends and write a report", template="research_report")
```

Built-in templates:

| Template | Steps | Pipeline |
|----------|-------|----------|
| `research_report` | 3 | Research → Analyze → Write |
| `competitive_analysis` | 4 | Multi-source research → SWOT → Report |
| `content_pipeline` | 4 | Research → Draft → Review → Polish |
| `code_review_fix` | 3 | Review → Fix → Test |

---

## Configuration

### Environment Variables

```bash
# Required: at least one provider
GEMINI_API_KEY=AIza...         # Free tier! https://aistudio.google.com/apikey
OPENAI_API_KEY=sk-proj-...
PERPLEXITY_API_KEY=pplx-...

# Optional: more providers (add as many as you want)
ANTHROPIC_API_KEY=sk-ant-...
DEEPSEEK_API_KEY=...
GROQ_API_KEY=gsk_...
FAL_KEY=...
ELEVENLABS_API_KEY=...

# Router config
LLM_ROUTER_PROFILE=balanced        # budget | balanced | premium
LLM_ROUTER_MONTHLY_BUDGET=0        # USD, 0 = unlimited
LLM_ROUTER_CLAUDE_SUBSCRIPTION=false  # true = you're a Claude Code Pro/Max user;
                                       # anthropic/* excluded, router uses non-Claude models

# Smart routing (Claude Code model selection)
DAILY_TOKEN_BUDGET=0               # tokens/day, 0 = unlimited
QUALITY_MODE=balanced              # best | balanced | conserve
MIN_MODEL=haiku                    # floor: haiku | sonnet | opus
```

See [.env.example](.env.example) for the full list of supported providers.

### Claude Code Integration

After running `./scripts/install.sh`, your `~/.claude.json` will include:

```json
{
  "mcpServers": {
    "llm-router": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/llm-router", "llm-router"]
    }
  }
}
```

---

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
uv run pytest -v

# Run integration tests (requires real API keys)
uv run pytest tests/test_integration.py -v

# Lint
uv run ruff check src/
```

---

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the detailed roadmap with phases and priorities.

### Completed (v0.1–v0.5)

- [x] Core text LLM routing (10+ providers)
- [x] Configurable profiles (budget / balanced / premium)
- [x] Cost tracking with SQLite
- [x] Health checks with circuit breaker
- [x] Image generation (Gemini Imagen 3, DALL-E, Flux, SD)
- [x] Video generation (Gemini Veo 2, Runway, Kling, minimax)
- [x] Audio/voice routing (ElevenLabs, OpenAI TTS)
- [x] Monthly budget enforcement
- [x] Multi-step orchestration with pipeline templates
- [x] Claude Code plugin with orchestrator agent and /route skill
- [x] Freemium tier gating
- [x] CI with GitHub Actions
- [x] Smart complexity-first routing (simple->haiku, moderate->sonnet, complex->opus)
- [x] Live Claude subscription monitoring (session %, weekly %, Sonnet %)
- [x] Time-aware budget pressure (factors in session reset proximity)
- [x] External fallback ranking when Claude is tight (Codex, OpenAI, Gemini)
- [x] Codex desktop integration (local agent, free via OpenAI subscription)
- [x] Unified usage dashboard (Claude sub + Codex + APIs + savings)
- [x] `llm_setup` tool for API discovery and secure key management
- [x] Per-provider budget limits
- [x] ASCII box-drawing dashboard (terminal-friendly, no Unicode issues)
- [x] Prompt classification cache (SHA-256 exact-match, in-memory LRU, 1h TTL)
- [x] `llm_cache_stats` + `llm_cache_clear` MCP tools
- [x] Auto-route hook (UserPromptSubmit heuristic classifier, zero-latency)
- [x] Rate limit detection with smart cooldowns (15s rate limit vs 60s hard failure)
- [x] `llm_setup(action='test')` — API key validation with minimal LLM calls
- [x] Streaming responses (`llm_stream` tool + `call_llm_stream()` async generator)
- [x] Usage auto-refresh hook (PostToolUse staleness detection + usage pulse wiring)
- [x] Published to PyPI as `claude-code-llm-router`
- [x] Multi-layer auto-classification: scoring heuristic → Ollama local LLM (qwen3.5) → cheap API (Gemini Flash/GPT-4o-mini)
- [x] Savings awareness (PostToolUse hook tracks routed calls, periodic cost savings reminders)
- [x] Structural context compaction (5 strategies: whitespace, comments, dedup, truncation, stack traces)
- [x] Quality logging (`routing_decisions` table + `llm_quality_report` tool)
- [x] Savings persistence (JSONL + SQLite import, lifetime analytics)
- [x] Gemini media APIs (Imagen 3 images, Veo 2 video)
- [x] Global hook installer (`llm_setup(action='install_hooks')` + `llm-router-install-hooks` CLI)
- [x] Global routing rules (auto-installed to `~/.claude/rules/llm-router.md`)
- [x] Session context injection (ring buffer + SQLite summaries, injected into all text tools)
- [x] `llm_save_session` MCP tool (auto-summarize + persist session for future context)
- [x] Cross-session memory (previous session summaries prepended to external LLM calls)
- [x] Auto-update routing rules (version header + silent update on MCP startup after pip upgrade)
- [x] Token arbitrage enforcement — routing hint override bug fixed; simple tasks now correctly route to cheap models
- [x] Claude Code subscription mode (`LLM_ROUTER_CLAUDE_SUBSCRIPTION`) — exclude Anthropic from chains; route to DeepSeek/Gemini/GPT-4o instead
- [x] Quality-cost tier sorting — within 5% quality band, prefer cheaper model (GPT-4o over Sonnet, DeepSeek over everyone when near-equal quality)
- [x] DeepSeek Reasoner in cheap tier — $0.0014/1K leads at >85% pressure (was treated as "paid" tier alongside o3 at $0.025)
- [x] Codex injection fix — no longer injected at position 0 when subscription mode removes Claude from chain (caused 300s timeouts)
- [x] Codex task filtering — excluded from RESEARCH (no web access) and QUERY (too slow) chains

### Completed (v0.7)

- [x] **Availability-aware routing** — P95 latency from `routing_decisions` table folded into benchmark quality score. Penalty range 0.0–0.50 (<5s=0, <15s=0.03, <60s=0.10, <180s=0.30, ≥180s=0.50). 60s cache prevents repeated DB hits per routing cycle.
- [x] **Codex cold-start defaults** — `_COLD_START_LATENCY_MS` applies pessimistic 60-90s P95 before any history exists, preventing Codex from being placed first in chains on a fresh install.
- [x] **`llm_edit` MCP tool** — Routes code-edit reasoning to a cheap CODE model. Reads files locally (32 KB cap), gets `{file, old_string, new_string}` JSON back, returns formatted instructions for Claude to apply mechanically. Keeps Opus out of the "what to change" loop.

### Completed (v0.8)

- [x] **3 routing correctness fixes** — async feedback loop, BUDGET hard cap, RESEARCH pressure tail (see CHANGELOG).
- [x] **PreToolUse[Agent] hook** — Intercepts subagent spawns; routes reasoning to cheap `llm_*` tools, approves pure retrieval. Biggest cost leak plugged.
- [x] **Session-end savings dashboard** — Reads real `routing_decisions` data; shows actual cost vs Sonnet 4.6 baseline per tool with ASCII bar charts.
- [x] **`usage.json` export** — MCP server writes `~/.llm-router/usage.json` so hooks can read quota pressure without importing Python packages.

### Completed (v0.9)

- [x] **Global MCP server registration** — `llm-router-install-hooks` now registers the MCP server globally (`~/.claude/settings.json`) so routing tools work in ALL Claude Code sessions, not just the llm-router project.
- [x] **Stale circuit breaker reset** — `HealthTracker.reset_stale()` clears failures older than 30 min on startup; yesterday's outage won't block today's routing.
- [x] **UUID session IDs** — Replaced `os.getppid()` with a UUID written at session start; prevents session data corruption across reboots.
- [x] **RESEARCH hard-fail** — `llm_research` immediately returns a helpful error when `PERPLEXITY_API_KEY` is not configured instead of silently using a non-web-grounded model.
- [x] **Sensible config defaults** — Monthly budget cap $20, daily token budget 500k, circuit breaker threshold 2 failures / 30s cooldown.
- [x] **Lifetime savings from real data** — `llm_usage` now computes savings vs Sonnet 4.6 baseline from `routing_decisions` table (actual token counts + real costs), not legacy estimates.

### Next Up (v1.0 — Evaluation & Learning)

- [ ] Classification outcome tracking (was the routed model's response good?)
- [ ] A/B testing framework for routing decisions
- [ ] Adaptive routing based on historical success rates

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Key areas where help is needed:
- Adding new provider integrations
- Improving routing intelligence
- Testing across different MCP clients
- Documentation and examples

---

## Ollama: Classifier vs Answerer

Ollama plays **two independent roles** in LLM Router. Configuring one does not enable the other.

### Role 1 — Local Classifier (hooks, free)
Used by `auto-route.py` to classify prompt complexity locally before calling cloud APIs.

```bash
LLM_ROUTER_OLLAMA_URL=http://localhost:11434   # hook classifier URL
LLM_ROUTER_OLLAMA_MODEL=qwen3.5:latest         # model used for classification
```

This runs in the Claude Code hooks pipeline. It classifies whether a prompt is simple/moderate/complex using a local LLM, saving ~$0.0001 per classification vs Gemini Flash.

### Role 2 — Local Task Answerer (router, for actual task responses)
Used by the MCP router to **answer tasks** with a local model instead of calling cloud providers.

```bash
OLLAMA_BASE_URL=http://localhost:11434          # router answerer URL
OLLAMA_BUDGET_MODELS=llama3.2,qwen2.5-coder:7b # comma-separated models
```

When configured, Ollama models are prepended to the routing chain in two scenarios:
1. **BUDGET profile** (simple tasks) — always tried first, for free
2. **Any profile at ≥ 85% Claude quota** — injected to spare subscription tokens

### Full local-first setup (both roles)
```bash
LLM_ROUTER_OLLAMA_URL=http://localhost:11434
LLM_ROUTER_OLLAMA_MODEL=qwen3.5:latest
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_BUDGET_MODELS=qwen3.5:latest,llama3.2
```

With this configuration, simple tasks never touch the cloud: classification is local, answering is local.

---

## License

[MIT](LICENSE) — use it however you want.

---

<p align="center">
  <sub>Built with <a href="https://litellm.ai">LiteLLM</a> and <a href="https://modelcontextprotocol.io">MCP</a></sub>
</p>
