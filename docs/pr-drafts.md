# PR Drafts for Awesome Repos

## 1. hesreallyhim/awesome-claude-code (34K stars)

### PR Title
Add resource: LLM Router

### Section: Project Scaffolding & MCP

Add this entry after the existing entries:

```markdown
- [LLM Router](https://github.com/ypollak2/llm-router) by [ypollak2](https://github.com/ypollak2) - One MCP server for every AI model. Routes text, image, video, and audio tasks to 20+ providers (GPT-4o, Gemini, Perplexity, DALL-E, Flux, Runway, ElevenLabs) — automatically picking the best model based on task complexity and budget. Features a multi-layer auto-route hook (heuristic → Ollama → cheap API), prompt classification cache, structural context compaction, circuit breaker per provider, and session context injection across models. Saves 40–70% on API costs by matching task complexity to model capability. Install: `pip install claude-code-llm-router` or `claude plugin add ypollak2/llm-router`.
```

### PR Body

```markdown
## Summary
- Adds [LLM Router](https://github.com/ypollak2/llm-router) to the **Project Scaffolding & MCP** section
- MCP server that gives Claude Code unified access to 20+ AI providers with intelligent routing

## Why it fits
LLM Router is an MCP server purpose-built for Claude Code that:
- Provides 24 MCP tools for text, image, video, and audio generation
- Auto-routes tasks to the optimal provider/model based on complexity classification
- Includes a `UserPromptSubmit` hook for automatic prompt classification
- Has a Claude Code plugin (`claude plugin add ypollak2/llm-router`)
- Available on PyPI: `pip install claude-code-llm-router`

## Key features
- Multi-layer classifier: heuristic scoring → local Ollama LLM → cheap API fallback
- Budget-aware routing with provider circuit breakers
- Cross-session context injection (session summaries + conversation history)
- Structural prompt compaction (5 strategies, saves ~30% tokens)
- Rate limit detection with automatic provider switching
- Streaming support, orchestration pipelines, quality analytics

MIT licensed, actively maintained, full test suite (300+ tests).
```

---

## 2. appcypher/awesome-mcp-servers (5.3K stars)

### PR Title
Add LLM Router to AI Services

### Section: 🤝 AI Services

Add this entry:

```markdown
- [LLM Router](https://github.com/ypollak2/llm-router) - Multi-LLM router MCP server — route text, image, video, and audio tasks to 20+ providers (OpenAI, Gemini, Perplexity, Anthropic, fal, ElevenLabs, Runway) with automatic complexity-based model selection, budget control, and provider failover.
```

### PR Body

```markdown
## Summary
- Adds LLM Router to the AI Services section

## What it does
LLM Router is an MCP server that provides unified access to 20+ AI providers through 24 MCP tools. It automatically routes tasks to the optimal model based on:
- Task complexity (simple → cheap models, complex → premium models)
- Budget constraints (monthly spend limits, daily token budgets)
- Provider health (circuit breakers, rate limit detection)

Supports text completion, web-augmented research (Perplexity), image generation (DALL-E, Flux, Imagen), video generation (Runway, Kling), and audio/TTS (ElevenLabs, OpenAI TTS).

- PyPI: `pip install claude-code-llm-router`
- License: MIT
- Python 3.10+
```

---

## 3. wong2/awesome-mcp-servers (3.8K stars)

### PR Title
Add LLM Router

### Entry (match existing format — check their list style)

```markdown
- [LLM Router](https://github.com/ypollak2/llm-router) - Route tasks to 20+ AI providers (OpenAI, Gemini, Perplexity, Anthropic, fal, ElevenLabs) with automatic complexity-based model selection and budget control. Supports text, image, video, and audio.
```

### PR Body

```markdown
## Summary
Adds LLM Router — an MCP server that provides unified routing to 20+ AI providers with intelligent model selection.

## Details
- 24 MCP tools covering text, image, video, and audio generation
- Multi-layer complexity classifier for automatic model selection
- Budget-aware routing with provider circuit breakers and failover
- PyPI: `pip install claude-code-llm-router`
- MIT licensed, Python 3.10+
```

---

## 4. ccplugins/awesome-claude-code-plugins (656 stars)

### PR Title
Add LLM Router MCP plugin

### Section: Development Engineering (or a new "AI Routing" section)

This repo uses a plugin directory structure. Check if they want a `plugins/llm-router/` directory or just a README entry.

### Entry

```markdown
- [llm-router](https://github.com/ypollak2/llm-router) - Multi-LLM routing MCP server with 24 tools for text, image, video, and audio. Auto-routes tasks to 20+ providers based on complexity and budget. Includes auto-route hook, streaming, orchestration pipelines, and cross-session context. Install: `claude plugin add ypollak2/llm-router` or `pip install claude-code-llm-router`.
```

### PR Body

```markdown
## Summary
- Adds LLM Router to the plugin directory

## Plugin type
- **MCP Server** — provides 24 tools to Claude Code
- **Hook** — `UserPromptSubmit` auto-route hook for automatic task classification
- **Skill** — `/route` slash command for smart routing

## Install
```bash
claude plugin add ypollak2/llm-router
# or
pip install claude-code-llm-router
```

## Features
- Routes to 20+ AI providers (OpenAI, Gemini, Perplexity, Anthropic, fal, Stability, ElevenLabs, Runway, etc.)
- Multi-layer auto-classifier: heuristic → Ollama → cheap API
- Budget control, provider failover, rate limit detection
- Session context injection for cross-model continuity
- Streaming, orchestration pipelines, quality analytics
- MIT licensed, 300+ tests
```

---

## 5. yzfly/Awesome-MCP-ZH (6.7K stars, Chinese)

### PR Title
Add LLM Router (多模型智能路由 MCP 服务)

### Entry

```markdown
- [LLM Router](https://github.com/ypollak2/llm-router) - 多模型智能路由 MCP 服务器，支持 20+ AI 提供商（OpenAI、Gemini、Perplexity、Anthropic、fal、ElevenLabs 等），自动根据任务复杂度和预算选择最优模型。提供文本、图像、视频、音频生成的 24 个 MCP 工具。安装：`pip install claude-code-llm-router`
```

### PR Body

```markdown
## Summary
添加 LLM Router — 智能多模型路由 MCP 服务器

## 功能
- 24 个 MCP 工具，覆盖文本、图像、视频、音频生成
- 多层分类器自动选择最优模型（启发式 → Ollama → 便宜 API）
- 预算感知路由，支持提供商断路器和故障转移
- 跨会话上下文注入
- PyPI: `pip install claude-code-llm-router`
- MIT 协议，Python 3.10+
```

---

## 6. chatmcp/mcpso (2K stars)

### PR Title
Add LLM Router

### Entry (check their format)

```markdown
- [LLM Router](https://github.com/ypollak2/llm-router) - Multi-LLM routing MCP server with intelligent model selection across 20+ AI providers. Supports text, image, video, and audio with budget control and provider failover.
```

---

## Submission Order (recommended)

1. **ccplugins/awesome-claude-code-plugins** — smaller, faster merge, establishes presence
2. **appcypher/awesome-mcp-servers** — high traffic, straightforward PR format
3. **hesreallyhim/awesome-claude-code** — highest stars, biggest impact
4. **wong2/awesome-mcp-servers** — secondary MCP list
5. **yzfly/Awesome-MCP-ZH** — Chinese market exposure
6. **chatmcp/mcpso** — additional directory listing
