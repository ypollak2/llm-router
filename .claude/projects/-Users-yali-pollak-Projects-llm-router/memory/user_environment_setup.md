---
name: user_environment_setup
description: Yali's llm-router environment — OPENAI + GEMINI + Ollama configured; subscription mode enabled
type: project
---

# User Environment Configuration

**Location**: `/Users/yali.pollak/Projects/llm-router/.env`

## Configured Providers

| Provider | Status | Models | Notes |
|----------|--------|--------|-------|
| **OpenAI** | ✅ Configured | gpt-5.4, o3, gpt-4o, gpt-4o-mini | API key present |
| **Google Gemini** | ✅ Configured | Gemini Flash, Gemini Pro | API key present |
| **Ollama** (local) | ✅ Running | gemma4:latest, qwen3.5:latest | Budget models for simple tasks |
| **Claude** (subscription) | ✅ Active | Haiku 4.5, Sonnet 4.6, Opus 4.6 | Via Claude Code subscription, NOT API key |

## Routing Chain (Actual)

```
simple      → Ollama (gemma4/qwen3.5) → OpenAI (gpt-4o-mini) → Gemini Flash
moderate    → Ollama → OpenAI (gpt-4o) → Gemini Pro → Claude Sonnet (subscription)
complex     → Ollama → OpenAI (o3) → Claude Opus (subscription)
research    → Perplexity (no key configured yet)
```

## Key Decisions

- **Subscription mode enabled** (`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`) — Claude models accessed via subscription, not API
- **Free-first strategy** — Ollama tried first for all tasks (cost: $0)
- **Fallback chain** — If Ollama unavailable, routes to OpenAI then Gemini

## When Answering Questions

- **Do NOT assume missing keys** — check `.env` first
- **Do NOT give generic API setup advice** — user's environment is already optimized
- **Do mention what's available** — OpenAI/Gemini for fast API fallbacks, Ollama for free local inference
