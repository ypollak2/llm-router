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
</p>

---

## The Problem

You use Claude Code (or any MCP client). You also have access to GPT-4o, Gemini, Perplexity, DALL-E, Runway, ElevenLabs — but switching between them is manual, slow, and expensive.

**LLM Router** gives your AI assistant one unified interface to all of them — and it automatically picks the right one based on what you're doing and what you can afford.

```
You:     "Research the latest AI funding rounds"
Router:  -> Perplexity Sonar Pro (search-augmented, best for current facts)

You:     "Generate a hero image for the landing page"
Router:  -> Flux Pro via fal.ai (best quality/cost for images)

You:     "Write unit tests for the auth module"
Router:  -> Claude Sonnet (top coding model, within budget)

You:     "Create a 5-second product demo clip"
Router:  -> Kling 2.0 via fal.ai (best value for short video)
```

---

## Quick Start

**Prerequisites**: Python 3.10+ and [uv](https://docs.astral.sh/uv/)

```bash
# Clone and install
git clone https://github.com/ypollak2/llm-router.git
cd llm-router
uv sync

# Set up API keys (interactive wizard)
uv run llm-router-onboard

# Add to Claude Code as MCP server + plugin
./scripts/install.sh
```

Restart Claude Code. You now have **17 new tools** available.

> **Start for free**: Google's Gemini API has a [free tier](https://aistudio.google.com/apikey) with 1M tokens/day — no credit card needed. [Groq](https://console.groq.com/keys) also offers a generous free tier with ultra-fast inference.

### Install as Claude Code Plugin

```bash
# 1. Add the marketplace
/plugin marketplace add ypollak2/llm-router

# 2. Install the plugin
/plugin install llm-router@llm-router
```

Or install manually from source:

```bash
git clone https://github.com/ypollak2/llm-router.git
cd llm-router
uv sync
./scripts/install.sh
```

The plugin adds:
- **17 MCP tools** — Smart routing, text, image, video, audio across 20+ providers
- **`/route` skill** — Smart task classification and routing in one command
- **Smart classifier** — Auto-picks Claude Haiku/Sonnet/Opus based on complexity and budget
- **LLM Orchestrator agent** — Autonomous multi-step task decomposition across models

---

## How It Works

```
                         +-------------------------------------+
                         |      Claude Code / MCP Client        |
                         +----------------+--------------------+
                                          | MCP Protocol
                         +----------------v--------------------+
                         |        LLM Router Server             |
                         |                                      |
                         |  +----------+  +-----------------+   |
                         |  | Budget   |  | Health          |   |
                         |  | Tracker  |  | Circuit Breaker |   |
                         |  +----+-----+  +----+------------+   |
                         |       |              |                |
                         |  +----v--------------v-----------+   |
                         |  |        Smart Router            |   |
                         |  |    profile + budget + health   |   |
                         |  +--+---+---+---+---+---+--------+   |
                         |     |   |   |   |   |   |            |
                         +-----+---+---+---+---+---+------------+
                               |   |   |   |   |   |
              +----------------+   |   |   |   |   +----------------+
              |                |   |   |   |   |                    |
         +----v----+    +-----v---v+  +v---v-+ |  +-----v-----+ +--v-------+
         | OpenAI  |    |  Google  |  |Perpl- | |  | fal.ai    | |  Runway  |
         | GPT/o3  |    |  Gemini  |  |exity  | |  | Flux/     | |  Kling   |
         | DALL-E  |    |  Flash   |  |Sonar  | |  | Stability | |          |
         | TTS     |    |  Pro     |  |       | |  |           | |          |
         +---------+    +----------+  +-------+ |  +-----------+ +----------+
                                                 |
                                      +----------v----------+
                                      |  Mistral / Deepseek |
                                      |  Groq / Together    |
                                      |  ElevenLabs / xAI   |
                                      +---------------------+
```

The router makes two decisions for every request:

1. **What type of task?** Text, image, video, audio, research, or code
2. **Which model fits your profile + budget?** Tries models in preference order, skips unhealthy providers, stays within budget

---

## Smart Routing (Claude Code Models)

Use Claude Code's own models (Haiku/Sonnet/Opus) **without extra API keys** via the smart classifier:

```
llm_classify("What is the capital of France?")
# -> simple (99%) -> haiku (free via Claude Code subscription)

llm_classify("Write a REST API with auth and pagination")
# -> moderate (98%) -> sonnet (free via Claude Code subscription)

llm_classify("Design a distributed CQRS architecture")
# -> complex (85%) -> opus (you're already here)
```

### Provider Indicators

Each provider gets a colored emoji for instant identification:

| Provider | Icon | Provider | Icon |
|----------|------|----------|------|
| OpenAI | `green circle` | Perplexity | `orange circle` |
| Gemini | `blue circle` | Anthropic | `purple circle` |
| Mistral | `red circle` | DeepSeek | `star-struck` |
| Groq | `yellow circle` | xAI | `white circle` |

### Progressive Budget Downshift

Set a daily token budget and the router automatically shifts to cheaper models as you approach the limit:

```bash
# In .env
DAILY_TOKEN_BUDGET=1000000   # tokens per day, 0 = unlimited
QUALITY_MODE=balanced        # best | balanced | conserve
MIN_MODEL=haiku              # floor: never route below this
```

| Budget Used | Effect |
|-------------|--------|
| 0-50% | No change — ideal model for complexity |
| 50-80% | Downshift by 1 tier (opus -> sonnet, sonnet -> haiku) |
| 80-95% | Downshift by 2 tiers (opus -> haiku) + warning |
| 95%+ | Max downshift + asks user before proceeding |

```
llm_classify("Complex task here")
# -> complex -> sonnet (downshifted from opus)
#    budget: [three-fifths bar] 60% | warning: downshifted from opus
```

---

## Providers

### Text & Code LLMs

| Provider | Models | Free Tier | Best For |
|----------|--------|-----------|----------|
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

### Image Generation

| Provider | Models | Best For |
|----------|--------|----------|
| **fal.ai** | Flux Pro, Flux Dev | Quality/cost ratio, fast generation |
| **OpenAI** | DALL-E 3, DALL-E 2 | Prompt adherence, text in images |
| **Stability AI** | Stable Diffusion 3 | Fine control, open weights |

### Video Generation

| Provider | Models | Best For |
|----------|--------|----------|
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

Once installed, Claude Code gets these 17 tools:

| Tool | What It Does |
|------|-------------|
| **Smart Routing** | |
| `llm_classify` | Classify complexity + recommend model (Claude Code or external) with budget awareness |
| `llm_route` | Auto-classify, then route to the best external LLM |
| `llm_track_usage` | Report Claude Code token usage for budget tracking |
| **Text & Code** | |
| `llm_query` | General questions — auto-routed to the best text LLM |
| `llm_research` | Search-augmented answers via Perplexity |
| `llm_generate` | Creative content — writing, summaries, brainstorming |
| `llm_analyze` | Deep reasoning — analysis, debugging, problem decomposition |
| `llm_code` | Coding tasks — generation, refactoring, algorithms |
| **Media** | |
| `llm_image` | Image generation — auto-picks DALL-E, Flux, or SD |
| `llm_video` | Video generation — routes to Runway, Kling, etc. |
| `llm_audio` | Voice/audio — TTS via ElevenLabs or OpenAI |
| **Orchestration** | |
| `llm_orchestrate` | Multi-step pipelines across multiple models |
| `llm_pipeline_templates` | List available orchestration templates |
| **Management** | |
| `llm_set_profile` | Switch routing profile (budget / balanced / premium) |
| `llm_usage` | View costs, token counts, per-model breakdown |
| `llm_health` | Check provider availability and circuit breaker status |
| `llm_providers` | List all supported and configured providers |

---

## Routing Profiles

Three built-in profiles control the cost/quality tradeoff:

| | Budget | Balanced | Premium |
|--|--------|----------|---------|
| **Text** | Gemini Flash, GPT-4o-mini | GPT-4o, Claude Sonnet | o3, Claude Opus |
| **Research** | Perplexity Sonar | Sonar Pro | Sonar Pro |
| **Code** | Deepseek, Gemini Flash | Claude Sonnet, GPT-4o | Claude Opus, o3 |
| **Image** | Flux Dev, SD3 | Flux Pro, DALL-E 3 | DALL-E 3 |
| **Video** | minimax | Kling, Runway Turbo | Runway Gen-3 |
| **Audio** | OpenAI TTS | ElevenLabs | ElevenLabs |

Switch anytime:
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

```
llm_orchestrate("Research AI trends and write a report", template="research_report")

# Pipeline: Research (Perplexity) -> Analyze (GPT-4o) -> Write (Gemini Pro)
# Each step feeds its output to the next
```

Built-in templates:
- `research_report` — Research -> Analyze -> Write (3 steps)
- `competitive_analysis` — Multi-source research -> SWOT -> Report (4 steps)
- `content_pipeline` — Research -> Draft -> Review -> Polish (4 steps)
- `code_review_fix` — Review -> Fix -> Test (3 steps)

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

- [x] Core text LLM routing (10 providers)
- [x] Configurable profiles (budget / balanced / premium)
- [x] Cost tracking with SQLite
- [x] Health checks with circuit breaker
- [x] Image generation routing (DALL-E, Flux, Stable Diffusion)
- [x] Video generation routing (Runway, Kling, minimax)
- [x] Audio/voice routing (ElevenLabs, OpenAI TTS)
- [x] Monthly budget enforcement
- [x] Multi-step orchestration with pipeline templates
- [x] Claude Code plugin with orchestrator agent and /route skill
- [x] Freemium tier gating
- [x] CI with GitHub Actions
- [x] Smart routing with complexity classification
- [x] Progressive budget-aware model downshifting
- [x] Claude Code model integration (haiku/sonnet/opus) — no extra API keys
- [x] Provider emoji indicators for visual identification
- [x] Quality mode and minimum model floor settings
- [ ] Streaming responses
- [ ] Weekly quality benchmark updates
- [ ] Web dashboard for usage analytics
- [ ] PyPI package distribution

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Key areas where help is needed:
- Adding new provider integrations
- Improving routing intelligence
- Testing across different MCP clients
- Documentation and examples

---

## License

[MIT](LICENSE) — use it however you want.

---

<p align="center">
  <sub>Built with <a href="https://litellm.ai">LiteLLM</a> and <a href="https://modelcontextprotocol.io">MCP</a></sub>
</p>
