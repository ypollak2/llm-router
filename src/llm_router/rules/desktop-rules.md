<!-- llm-router-rules-version: 1 -->
# LLM Router — Claude Desktop Routing Rules

> These rules apply when using llm-router MCP tools inside Claude Desktop.
> Claude Desktop has no hook system, so cost-routing (avoiding Claude charges) is
> not possible — every call still goes through Claude. These rules enable
> **capability extension**: using specialized external models for tasks where
> they outperform Claude (e.g. live web search via Perplexity, image generation).

---

## What llm-router Adds in Claude Desktop

| Capability | Tool | Value |
|---|---|---|
| Live web search | `llm_research` | Perplexity Sonar — real-time results |
| Image generation | `llm_image` | DALL-E 3 / Flux / Imagen 3 |
| Video generation | `llm_video` | Veo / Kling / Gen-3 |
| Audio / TTS | `llm_audio` | ElevenLabs / OpenAI TTS |
| Savings visibility | `llm_savings` | Cross-session stats from all hosts |

---

## Honest Framing

Cost-routing (saving Claude API or subscription tokens) does NOT apply here —
Claude Desktop routes all tasks through Claude regardless. The additive cost
of external model calls is real. Use external tools only when:
- The task requires live/current data (`llm_research`)
- The task produces media that Claude cannot generate (`llm_image`, `llm_video`)
- You explicitly want a second opinion from a different model

---

## Savings Tracking

Even though cost-routing is unavailable, calls to `llm_*` tools are tracked
in the shared SQLite database. `llm_savings` shows cross-host totals — useful
to see savings accumulated via Claude Code or Codex alongside Desktop usage.
