<p align="center">
  <img src="docs/logo.svg" alt="LLM Router" width="120" />
</p>

<h1 align="center">LLM Router</h1>

<p align="center">
  <strong>One MCP server. Every AI model. Smart routing.</strong>
</p>

<p align="center">
  Route text, image, video, and audio tasks to 20+ AI providers — automatically picking the best model for the job based on your budget, quality needs, and remaining credits.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#how-it-works">How It Works</a> &bull;
  <a href="#providers">Providers</a> &bull;
  <a href="#routing-profiles">Profiles</a> &bull;
  <a href="#budget-aware-routing">Budget Routing</a> &bull;
  <a href="docs/PROVIDERS.md">Provider Setup</a>
</p>

<p align="center">
  <a href="https://github.com/ypollak2/llm-router/actions"><img src="https://img.shields.io/github/actions/workflow/status/ypollak2/llm-router/ci.yml?style=flat-square&label=tests" alt="Tests"></a>
  <a href="https://pypi.org/project/llm-router/"><img src="https://img.shields.io/pypi/v/llm-router?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://github.com/ypollak2/llm-router/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.10%E2%80%933.13-blue?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/MCP-1.0+-purple?style=flat-square" alt="MCP">
  <img src="https://img.shields.io/badge/providers-20+-orange?style=flat-square" alt="Providers">
</p>

---

## The Problem

You use Claude Code (or any MCP client). You also have access to GPT-4o, Gemini, Perplexity, DALL-E, Runway, ElevenLabs — but switching between them is manual, slow, and expensive.

**LLM Router** gives your AI assistant one unified interface to all of them — and it automatically picks the right one based on what you're doing, what you can afford, and which models are performing best this week.

```
You:     "Research the latest AI funding rounds"
Router:  → Perplexity Sonar Pro (search-augmented, best for current facts)

You:     "Generate a hero image for the landing page"
Router:  → Flux Pro via fal.ai (best quality/cost for images)

You:     "Write unit tests for the auth module"
Router:  → GPT-4o (top coding benchmark this week, within budget)

You:     "Create a 5-second product demo clip"
Router:  → Kling 2.0 (best value for short video generation)
```

---

## Quick Start

**Prerequisites**: Python 3.10+ and [uv](https://docs.astral.sh/uv/)

```bash
# Clone and install
git clone https://github.com/ypollak2/llm-router.git
cd llm-router
uv sync

# Set up API keys (interactive wizard — start with Gemini's free tier for $0)
uv run llm-router-onboard

# Add to Claude Code
./scripts/install.sh
```

Restart Claude Code. You now have 8 new tools available.

> **Start for free**: Google's Gemini API has a [free tier](https://aistudio.google.com/apikey) — no credit card needed. You can add more providers later.

---

## How It Works

```
                         ┌─────────────────────────────────────┐
                         │          Claude Code / MCP Client    │
                         └──────────────┬──────────────────────┘
                                        │ MCP Protocol
                         ┌──────────────▼──────────────────────┐
                         │          LLM Router Server           │
                         │                                      │
                         │  ┌──────────┐  ┌──────────────────┐ │
                         │  │ Budget    │  │ Quality          │ │
                         │  │ Tracker   │  │ Benchmarks       │ │
                         │  │          │  │ (weekly update)   │ │
                         │  └────┬─────┘  └────┬─────────────┘ │
                         │       │              │               │
                         │  ┌────▼──────────────▼─────────┐    │
                         │  │      Smart Router            │    │
                         │  │  profile + budget + quality  │    │
                         │  └──┬───┬───┬───┬───┬───┬──────┘    │
                         │     │   │   │   │   │   │           │
                         └─────┼───┼───┼───┼───┼───┼───────────┘
                               │   │   │   │   │   │
              ┌────────────────┤   │   │   │   │   ├────────────────┐
              │                │   │   │   │   │                    │
         ┌────▼────┐    ┌─────▼───▼┐ ┌▼───▼─┐ │  ┌─────▼─────┐ ┌──▼───────┐
         │ OpenAI  │    │  Google  │ │Perpl- │ │  │ Stability │ │  Runway  │
         │ GPT/o3  │    │  Gemini  │ │exity  │ │  │ DALL-E    │ │  Kling   │
         │ DALL-E  │    │  Imagen  │ │Sonar  │ │  │ Flux      │ │  Pika    │
         │ Sora    │    │  Veo     │ │       │ │  │           │ │          │
         └─────────┘    └─────────┘ └───────┘ │  └───────────┘ └──────────┘
                                               │
                                    ┌──────────▼──────────┐
                                    │  Mistral / Deepseek │
                                    │  Groq / Together    │
                                    │  ElevenLabs / more  │
                                    └─────────────────────┘
```

The router makes three decisions for every request:

1. **What type of task?** → text, image, video, audio, research, code
2. **Which model fits your budget?** → checks remaining credits, monthly spend
3. **Which model is best right now?** → uses weekly-updated quality benchmarks

---

## Providers

### Text & Code LLMs

| Provider | Models | Free Tier | Best For |
|----------|--------|-----------|----------|
| **OpenAI** | GPT-4o, GPT-4o-mini, o3, o4-mini | No | Code, analysis, reasoning |
| **Google Gemini** | 2.5 Pro, 2.0 Flash | **Yes** (1M tokens/day) | Generation, long context |
| **Perplexity** | Sonar, Sonar Pro | No | Research, current events |
| **Anthropic** | Claude Sonnet, Haiku | No | Nuanced writing, safety |
| **Mistral** | Large, Medium, Small | Yes (limited) | European hosting, multilingual |
| **Deepseek** | V3, R1 | Yes (limited) | Cost-effective reasoning |
| **Groq** | Llama 3, Mixtral | Yes | Ultra-fast inference |
| **Together** | Llama 3, CodeLlama | Yes (limited) | Open-source models |
| **xAI** | Grok | No | Real-time information |
| **Cohere** | Command R+ | Yes (trial) | RAG, enterprise search |

### Image Generation

| Provider | Models | Best For |
|----------|--------|----------|
| **OpenAI** | DALL-E 3 | Prompt adherence, text in images |
| **fal.ai** | Flux Pro, Flux Dev | Quality/cost ratio, fast generation |
| **Stability AI** | Stable Diffusion 3, SDXL | Fine control, open weights |
| **Ideogram** | Ideogram 2.0 | Typography, logos |
| **Leonardo AI** | Phoenix, Kino | Game art, concept design |

### Video Generation

| Provider | Models | Best For |
|----------|--------|----------|
| **Runway** | Gen-3 Alpha | Professional quality, motion control |
| **Kling** | Kling 2.0 | Value, longer clips |
| **Pika** | Pika 2.0 | Quick iterations, style |
| **Google** | Veo 2 | Realism, physics |
| **OpenAI** | Sora | Cinematic quality |
| **Luma** | Dream Machine | 3D understanding |

### Audio & Voice

| Provider | Models | Best For |
|----------|--------|----------|
| **ElevenLabs** | Multilingual v2 | Voice cloning, quality |
| **OpenAI** | TTS, Whisper | Cost-effective TTS/STT |
| **Google** | Cloud TTS | Enterprise, many languages |

> **20+ providers and growing.** See [docs/PROVIDERS.md](docs/PROVIDERS.md) for full setup guides.

---

## MCP Tools

Once installed, Claude Code gets these tools:

| Tool | What It Does |
|------|-------------|
| `llm_query` | General questions — auto-routed to the best text LLM |
| `llm_research` | Search-augmented answers via Perplexity |
| `llm_generate` | Creative content — writing, summaries, brainstorming |
| `llm_analyze` | Deep reasoning — analysis, debugging, problem decomposition |
| `llm_code` | Coding tasks — generation, refactoring, algorithms |
| `llm_image` | Image generation — auto-picks DALL-E, Flux, or SD |
| `llm_video` | Video generation — routes to Runway, Kling, Pika, etc. |
| `llm_audio` | Voice/audio — TTS, transcription, voice cloning |
| `llm_set_profile` | Switch routing profile (budget / balanced / premium) |
| `llm_set_budget` | Set monthly spending limit |
| `llm_usage` | View costs, token counts, per-model breakdown |
| `llm_health` | Check provider availability and circuit breaker status |

---

## Routing Profiles

Three built-in profiles control the cost/quality tradeoff:

| | Budget | Balanced | Premium |
|--|--------|----------|---------|
| **Text** | Gemini Flash, GPT-4o-mini | GPT-4o, Gemini Pro | o3, Gemini 2.5 Pro |
| **Research** | Perplexity Sonar | Sonar Pro | Sonar Pro |
| **Code** | Gemini Flash | GPT-4o | o3 |
| **Image** | Flux Dev | Flux Pro, DALL-E 3 | DALL-E 3 HD |
| **Video** | Kling | Runway Gen-3 | Sora |
| **Audio** | OpenAI TTS | ElevenLabs | ElevenLabs (cloned) |

Switch anytime:
```
llm_set_profile("budget")    # Development, drafts, exploration
llm_set_profile("balanced")  # Production work, client deliverables
llm_set_profile("premium")   # Critical tasks, maximum quality
```

---

## Budget-Aware Routing

Set a monthly budget and the router optimizes within it:

```
llm_set_budget(monthly_usd=50.00)
```

The router then:

- **Tracks real-time spend** across all providers
- **Adjusts model selection** as you approach your limit — gracefully downshifts from premium to balanced to budget models
- **Warns you** at 80% and 95% of budget
- **Never exceeds** your limit — switches to free-tier models or pauses

### Budget Dashboard

```
llm_usage("month")

## Usage Summary (March 2026)
Calls: 847
Tokens: 1,240,000 in + 380,000 out
Cost: $12.47 / $50.00 budget (24.9%)
Remaining: $37.53

### By Provider
- openai:    312 calls, $8.20  ████████░░ 65.8%
- gemini:    401 calls, $2.10  ██░░░░░░░░ 16.8%
- perplexity: 89 calls, $1.45  █░░░░░░░░░ 11.6%
- fal.ai:     45 calls, $0.72  █░░░░░░░░░  5.8%

### Budget Projection
At current pace: $38.20/mo (within budget)
Recommendation: staying on "balanced" profile
```

---

## Weekly Quality Benchmarks

Model quality changes. New versions ship. Benchmarks shift. LLM Router updates its routing table weekly based on:

- **Public benchmarks**: MMLU, HumanEval, LMSYS Chatbot Arena
- **Task-specific scores**: coding (SWE-bench), reasoning (MATH), vision (MMMU)
- **Latency and reliability**: p50/p95 response times, error rates
- **Cost efficiency**: quality-per-dollar rankings

The routing table automatically adjusts. If Gemini 2.5 Pro overtakes GPT-4o on coding benchmarks next week, the router shifts coding tasks to Gemini — no manual config needed.

```
llm_health()

## Quality Benchmarks (updated 2026-03-24)
### Text/Code
- Coding:    1. o3 (92.1)  2. Gemini 2.5 Pro (89.3)  3. GPT-4o (87.1)
- Reasoning: 1. o3 (95.2)  2. Gemini 2.5 Pro (91.0)  3. Deepseek R1 (88.4)
- Writing:   1. Claude Sonnet (91.8)  2. GPT-4o (89.5)  3. Gemini 2.5 Pro (88.2)

### Image
- Quality:   1. DALL-E 3 HD  2. Flux Pro  3. Ideogram 2.0
- Speed:     1. Flux Dev (2.1s)  2. SDXL Turbo (1.8s)  3. DALL-E 3 (8.2s)
- Value:     1. Flux Dev ($0.003)  2. SDXL ($0.002)  3. DALL-E 3 ($0.040)
```

---

## Pricing

**Everything routes for free.** Pay for the intelligence that makes routing smarter and cheaper.

| | Free | Pro ($12/mo) | Team ($39/mo) |
|--|------|-------------|---------------|
| **All 20+ providers** | BYOK | BYOK + Credits | BYOK + Credits |
| **All modalities** | Text, image, video, audio | Same | Same |
| **Basic routing** | 3 built-in profiles | Same + custom profiles | Shared team profiles |
| **Multi-step orchestration** | - | Auto-chain tasks across models | + batch jobs, scheduling |
| **Budget optimizer** | - | Monthly budget with auto-downshift | Per-project budgets |
| **Quality benchmarks** | - | Weekly auto-updated routing | Same |
| **Synergy detection** | - | "Chain X→Y saves 30%" recommendations | Same |
| **Analytics** | Total spend | Full breakdown, trends, projections | Team-wide, chargeback |
| **Savings reports** | - | "You saved $127 this month" | Team savings |
| **Universal credits** | - | $5/mo included (buy more anytime) | $15/mo included |
| **Support** | GitHub Issues | Email | Priority + onboarding |

### Universal Credits

Tired of managing 10+ API keys? Buy LLM Router credits and let the router handle it:

- **$1 credit = $1.10-$1.15 of API value** (we optimize your spend)
- Credits work across all providers — one balance, zero key management
- Pro includes $5/mo, Team includes $15/mo — buy more anytime
- Unused credits roll over month to month

> The open-source self-hosted version is **fully featured** for routing. Cloud adds the intelligence layer (benchmarks, budget optimization, orchestration) and credits convenience.

---

## Configuration

### Environment Variables

```bash
# Required: at least one provider
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
PERPLEXITY_API_KEY=pplx-...

# Optional: additional providers
ANTHROPIC_API_KEY=sk-ant-...
MISTRAL_API_KEY=...
DEEPSEEK_API_KEY=...
FAL_KEY=...
RUNWAY_API_KEY=...
ELEVENLABS_API_KEY=...
STABILITY_API_KEY=...

# Router config
LLM_ROUTER_PROFILE=balanced     # budget | balanced | premium
LLM_ROUTER_MONTHLY_BUDGET=50    # USD, 0 = unlimited
```

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

# Run integration tests (requires API keys)
uv run pytest tests/test_integration.py -v

# Lint
uv run ruff check src/
```

---

## Roadmap

- [x] Core text LLM routing (OpenAI, Gemini, Perplexity)
- [x] Configurable profiles (budget / balanced / premium)
- [x] Cost tracking with SQLite
- [x] Health checks with circuit breaker
- [x] Claude Code plugin with orchestrator agent
- [ ] Image generation routing (DALL-E, Flux, Stable Diffusion)
- [ ] Video generation routing (Runway, Kling, Pika, Sora)
- [ ] Audio/voice routing (ElevenLabs, OpenAI TTS)
- [ ] Budget-aware routing with monthly limits
- [ ] Weekly quality benchmark updates
- [ ] 10+ additional text LLM providers
- [ ] Cloud version with team features
- [ ] Web dashboard for usage analytics
- [ ] Streaming responses

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Key areas where help is needed:
- Adding new provider integrations
- Improving routing intelligence
- Building the benchmark update pipeline
- Testing across different MCP clients

---

## License

[MIT](LICENSE) — use it however you want.

---

<p align="center">
  <sub>Built with <a href="https://litellm.ai">LiteLLM</a> and <a href="https://modelcontextprotocol.io">MCP</a></sub>
</p>
