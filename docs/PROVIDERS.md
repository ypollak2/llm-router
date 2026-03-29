# Provider Setup Guide

Get started with LLM Router by configuring at least one provider. We recommend starting with **Gemini's free tier** — no credit card needed.

## Quick Setup

```bash
uv run llm-router-onboard
```

The interactive wizard walks you through each provider. Or manually create a `.env` file from `.env.example`.

---

## Text & Code LLMs

### Google Gemini (Recommended — Free Tier)

1. Go to [Google AI Studio](https://aistudio.google.com/apikey)
2. Click "Create API Key"
3. Add to `.env`: `GEMINI_API_KEY=AIza...`

**Free tier**: 1M tokens/day, 15 RPM. Perfect for getting started.

### OpenAI

1. Go to [OpenAI Platform](https://platform.openai.com/api-keys)
2. Create a new API key
3. Add to `.env`: `OPENAI_API_KEY=sk-proj-...`

**Models**: GPT-4o, GPT-4o-mini, o3, o4-mini

### Perplexity

1. Go to [Perplexity Settings](https://www.perplexity.ai/settings/api)
2. Generate an API key
3. Add to `.env`: `PERPLEXITY_API_KEY=pplx-...`

**Models**: Sonar (search-augmented), Sonar Pro. Best for research tasks.

### Anthropic

1. Go to [Anthropic Console](https://console.anthropic.com/settings/keys)
2. Create a new API key
3. Add to `.env`: `ANTHROPIC_API_KEY=sk-ant-...`

**Models**: Claude Opus, Sonnet, Haiku

### Groq (Free Tier)

1. Go to [Groq Console](https://console.groq.com/keys)
2. Create an API key
3. Add to `.env`: `GROQ_API_KEY=gsk_...`

**Free tier**: Generous limits. Ultra-fast inference (Llama 3.3, Mixtral).

### Deepseek

1. Go to [Deepseek Platform](https://platform.deepseek.com/api_keys)
2. Create an API key
3. Add to `.env`: `DEEPSEEK_API_KEY=sk-...`

**Models**: DeepSeek V3 (general), DeepSeek Reasoner (reasoning). Very cost-effective.

### Mistral

1. Go to [Mistral Console](https://console.mistral.ai/api-keys)
2. Create an API key
3. Add to `.env`: `MISTRAL_API_KEY=...`

**Models**: Mistral Large, Small. European hosting, strong multilingual.

### Together AI

1. Go to [Together Settings](https://api.together.xyz/settings/api-keys)
2. Create an API key
3. Add to `.env`: `TOGETHER_API_KEY=...`

**Models**: Open-source models (Llama 3, CodeLlama, etc.)

### xAI

1. Go to [xAI Console](https://console.x.ai/)
2. Create an API key
3. Add to `.env`: `XAI_API_KEY=...`

**Models**: Grok 3

### Cohere

1. Go to [Cohere Dashboard](https://dashboard.cohere.com/api-keys)
2. Create an API key
3. Add to `.env`: `COHERE_API_KEY=...`

**Models**: Command R+ (RAG, enterprise search)

---

## Automated Key Discovery

The `llm_setup` tool can scan your laptop for existing API keys (environment variables and `.env` files) — no data leaves your machine.

```
# In Claude Code:
Use llm_setup with action="discover"   # find existing keys
Use llm_setup with action="status"     # see which providers are configured
Use llm_setup with action="guide" provider="gemini"  # setup instructions
Use llm_setup with action="add" provider="gemini" api_key="AIza..."  # save key to .env
```

All operations are local-only. Keys are masked in output and `.gitignore` protection is verified on write.

---

## Image Generation

### Google Gemini — Imagen 3

Uses the same `GEMINI_API_KEY` configured above.

**Models**: Imagen 3 (high quality), Imagen 3 Fast (lower latency). Included in Gemini API free tier.

### fal.ai (Recommended)

1. Go to [fal.ai Dashboard](https://fal.ai/dashboard/keys)
2. Create an API key
3. Add to `.env`: `FAL_KEY=...`

**Models**: Flux Pro, Flux Dev. Best quality/cost ratio.

### Stability AI

1. Go to [Stability Platform](https://platform.stability.ai/account/keys)
2. Create an API key
3. Add to `.env`: `STABILITY_API_KEY=sk-...`

**Models**: Stable Diffusion 3, SDXL

---

## Video Generation

### Google Gemini — Veo 2

Uses the same `GEMINI_API_KEY` configured above.

**Models**: Veo 2. High-quality video generation via Gemini API.

### Runway

1. Go to [Runway Dev](https://dev.runwayml.com/)
2. Get API access
3. Add to `.env`: `RUNWAY_API_KEY=...`

**Models**: Gen-3 Alpha. Professional quality video.

### Replicate

1. Go to [Replicate Account](https://replicate.com/account/api-tokens)
2. Create an API token
3. Add to `.env`: `REPLICATE_API_TOKEN=r8_...`

**Models**: Various open-source video/image models

---

## Audio & Voice

### ElevenLabs

1. Go to [ElevenLabs Settings](https://elevenlabs.io/app/settings/api-keys)
2. Create an API key
3. Add to `.env`: `ELEVENLABS_API_KEY=...`

**Models**: Multilingual v2. Voice cloning, highest quality TTS.

### OpenAI TTS

Uses the same `OPENAI_API_KEY` configured above.

**Models**: TTS-1, TTS-1-HD. Cost-effective text-to-speech.

---

## Verifying Setup

After configuring keys, verify with:

```
# In Claude Code:
Use llm_setup with action="status"    # see all configured providers
Use llm_health to check provider status  # verify connectivity
```

Or from the command line:
```bash
uv run python -c "from llm_router.config import get_config; c = get_config(); print(f'Providers: {c.available_providers}')"
```

---

## Tips

- **Start minimal**: Begin with Gemini (free) and add providers as needed
- **Budget profile first**: Use `budget` profile while testing to minimize costs
- **Check health**: Use `llm_health` to verify providers are reachable before heavy use
- **Rotate keys**: If a key stops working, regenerate it at the provider's dashboard
