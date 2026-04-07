# LLM Router

**One MCP server. Every AI model. Smart routing.**

Route text, code, image, video, and audio tasks to 20+ providers — automatically picking the right model based on task complexity and your budget. Works in Claude Code, Cursor, Windsurf, Zed, claw-code, and Agno.

[![Tests](https://img.shields.io/github/actions/workflow/status/ypollak2/llm-router/ci.yml?style=flat-square&label=tests)](https://github.com/ypollak2/llm-router/actions)
[![PyPI](https://img.shields.io/pypi/v/claude-code-llm-router?style=flat-square)](https://pypi.org/project/claude-code-llm-router/)
[![Downloads](https://img.shields.io/pypi/dm/claude-code-llm-router?style=flat-square)](https://pypi.org/project/claude-code-llm-router/)
[![Python](https://img.shields.io/badge/python-3.10–3.13-blue?style=flat-square)](https://pypi.org/project/claude-code-llm-router/)
[![MCP](https://img.shields.io/badge/MCP-1.0+-purple?style=flat-square)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Stars](https://img.shields.io/github/stars/ypollak2/llm-router?style=flat-square&color=yellow)](https://github.com/ypollak2/llm-router/stargazers)

---

## Why

Not every task needs the same model. Without a router, everything goes to the same expensive frontier model — like hiring a surgeon to change a lightbulb.

| Task | Without router | With router | Savings |
|------|---------------|-------------|---------|
| Simple queries (60% of work) | Opus — $0.015 | Haiku / Gemini Flash — $0.0001 | **99%** |
| Moderate tasks (30% of work) | Opus — $0.015 | Sonnet — $0.003 | **80%** |
| Complex tasks (10% of work) | Opus — $0.015 | Opus — $0.015 | 0% |
| **Blended monthly** | **~$50/mo** | **~$8–15/mo** | **70–85%** |

With Ollama: simple tasks route to a free local model — those 60% of queries cost **$0**.

---

## Quick Start

```bash
pipx install claude-code-llm-router && llm-router install
```

That's it. The installer registers the MCP server and installs hooks into `~/.claude/` so every prompt is evaluated automatically.

**Zero API keys required** if you have a Claude Code Pro/Max subscription. Add `GEMINI_API_KEY` for a free external fallback (1M tokens/day free tier).

```bash
# Optional: add providers in .env
GEMINI_API_KEY=AIza...      # free tier
OPENAI_API_KEY=sk-proj-...
PERPLEXITY_API_KEY=pplx-...

# If you use Claude Code subscription
LLM_ROUTER_CLAUDE_SUBSCRIPTION=true
```

---

## How It Works

Every prompt is intercepted by a `UserPromptSubmit` hook before your top-tier model sees it:

```
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

34 tools across 6 categories:

### Smart Routing
| Tool | What it does |
|------|-------------|
| `llm_route` | Auto-classify prompt → route to best model |
| `llm_classify` | Classify complexity + recommend model |
| `llm_select_agent` | Pick agent CLI (claude_code / codex) + model for a session |
| `llm_stream` | Stream LLM response for long-running tasks |

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

Use llm-router's 34 tools as an MCP tool provider in any Agno agent:

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

# Ollama (local models)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_BUDGET_MODELS=llama3.2,qwen2.5-coder:7b

# Spend limits
LLM_ROUTER_DAILY_SPEND_LIMIT=5.00   # USD, 0 = disabled
```

Full reference: [.env.example](.env.example)

---

## Budget Control

```bash
LLM_ROUTER_MONTHLY_BUDGET=50   # raises BudgetExceededError when exceeded
```

```
llm_usage("month")
→ Calls: 142 | Tokens: 320k | Cost: $3.42 | Budget: 6.8% of $50
```

The router tracks spend in SQLite across all providers and blocks calls when the monthly cap is reached.

---

## Dashboard

```bash
llm-router dashboard   # opens localhost:7337
```

Live view of routing decisions, cost trends, model distribution, and subscription pressure. Auto-refreshes every 30s.

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

| Version | Theme | Status |
|---------|-------|--------|
| v1.3–v1.9 | Dashboard, filesystem tools, enforcement, agent selection | ✅ Done |
| **v2.0** | **Agno `RouteredModel` + `RouteredTeam`; routing dry-run** | 🔜 Next |

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
