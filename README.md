![LLM Router](docs/llm-router-header.png)

> Route every AI call to the cheapest model that can do the job well.
> 48 tools · 20+ providers · personal routing memory · budget caps, dashboards, traces.

[![PyPI](https://img.shields.io/pypi/v/claude-code-llm-router?style=flat-square)](https://pypi.org/project/claude-code-llm-router/)
[![Tests](https://img.shields.io/github/actions/workflow/status/ypollak2/llm-router/ci.yml?style=flat-square&label=tests)](https://github.com/ypollak2/llm-router/actions)
[![Downloads](https://img.shields.io/pypi/dm/claude-code-llm-router?style=flat-square)](https://pypi.org/project/claude-code-llm-router/)
[![Python](https://img.shields.io/badge/python-3.10–3.13-blue?style=flat-square)](https://pypi.org/project/claude-code-llm-router/)
[![MCP](https://img.shields.io/badge/MCP-1.0+-purple?style=flat-square)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Stars](https://img.shields.io/github/stars/ypollak2/llm-router?style=flat-square&color=yellow)](https://github.com/ypollak2/llm-router/stargazers)

**Average savings: 60–80% vs running everything on Claude Opus.**

## Install

```bash
pipx install claude-code-llm-router && llm-router install
```

| Host | Command |
|------|---------|
| Claude Code | `llm-router install` |
| VS Code | `llm-router install --host vscode` |
| Cursor | `llm-router install --host cursor` |
| Codex CLI | `llm-router install --host codex` |

## What It Does

Intercepts prompts and routes them to the cheapest model that can handle the task. Most AI sessions are full of low-value work: file lookups, small edits, quick questions. Those burn through expensive models unnecessarily.

llm-router keeps cheap work on cheap/free models, escalates to premium models only when needed. No micromanagement required.

- Works in: Claude Code, Cursor, VS Code, Codex, Windsurf, Zed, claw-code, Agno
- Free-first: Ollama (local) → Codex → Gemini Flash → OpenAI → Claude (subscription)

## Mental Model

Think of llm-router as a **smart task dispatcher**. When you ask a question:

1. **Analyze** — What kind of task is this? (simple lookup vs. complex reasoning)
2. **Choose** — Which model can handle this best *and* cheapest?
3. **Check Constraints** — Are we over budget? Is this model degraded?
4. **Execute** — Send to that model

The dispatcher learns over time: if a model starts performing poorly (judge scores drop), it gets demoted in future decisions. If you're running low on quota (budget pressure), it automatically uses cheaper models. You don't manage any of this—it just happens behind the scenes.

**Example:** "Explain this error message" → Simple task → Route to Haiku (fast, cheap) → Done. vs. "Refactor this complex architecture" → Complex task → Route to Opus (expensive but thorough) → Done.

The savings come from not using Opus for every question.

## New in v6.4 — Quality Guard

- **Judge-based quality feedback** integrated into routing decisions
- **Quality reordering** — models demoted if scores drop below threshold
- **Hard floor enforcement** — poor-performing models automatically escalated to better tier

See [CHANGELOG.md](CHANGELOG.md) for all changes.

## New in v6.3 — Three-Layer Compression

- **RTK command compression** — bash output filtered (60–90% reduction)
- **Model-based routing** — existing cost reduction (70–90%)
- **Response compression** — LLM outputs condensed (60–75% reduction)
- **Unified dashboard** — `llm_gain` shows all layers

## How It Works

```
User Prompt
    ↓
[Complexity Classifier] — Haiku/Sonnet/Opus?
    ↓
[Free-First Router] — Ollama → Codex → Gemini Flash → OpenAI → Claude
    ↓
[Budget Pressure Check] — Downshift if over 85% budget
    ↓
[Quality Guard] — Demote if judge score < 0.6
    ↓
Selected Model → Execute
```

## Configuration

Zero-config by default if you use Claude Code Pro/Max (subscription mode).

Optional env vars:
```bash
OPENAI_API_KEY=sk-...                   # GPT-4o, o3
GEMINI_API_KEY=AIza...                  # Gemini Flash (free tier)
OLLAMA_BASE_URL=http://localhost:11434  # Local Ollama (free)
LLM_ROUTER_PROFILE=balanced             # budget|balanced|premium
LLM_ROUTER_COMPRESS_RESPONSE=true       # Enable response compression
```

For full setup guide, see [docs/SETUP.md](docs/SETUP.md).

## MCP Tools (48 total)

**Routing:**
- `llm_route` — Route task to optimal model
- `llm_classify` — Classify task complexity
- `llm_quality_guard` — Monitor model health

**Text:**
- `llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`

**Media:**
- `llm_image`, `llm_video`, `llm_audio`

**Admin:**
- `llm_usage`, `llm_savings`, `llm_budget`, `llm_health`, `llm_providers`

**Advanced:**
- `llm_orchestrate` — Multi-step pipelines
- `llm_setup` — Configure provider keys
- `llm_policy` — Routing policy management

[Full tool reference](docs/TOOLS.md) — Complete documentation for all 48 tools

## Architecture

See [CLAUDE.md](CLAUDE.md) for:
- Design decisions
- Module organization
- Development workflow
- Release process

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for:
- Three-layer compression pipeline
- Judge scoring system
- Quality trend tracking
- Budget pressure algorithm

## Development

```bash
uv run pytest tests/ -q          # Run tests
uv run ruff check src/ tests/    # Lint
uv run llm-router --version      # Check version
```

## License

MIT — See [LICENSE](LICENSE)

## Support

- Issues: [GitHub Issues](https://github.com/ypollak2/llm-router/issues)
- Discussions: [GitHub Discussions](https://github.com/ypollak2/llm-router/discussions)
- Releases: [PyPI](https://pypi.org/project/claude-code-llm-router/)
