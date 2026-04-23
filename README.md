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

## Token Savings Proof

Real numbers from a 14-day sprint: **51 releases, 22.6M tokens, $6.95 spent.**

### Cost Impact
- **Actual spend:** $6.95 (22.6M development tokens)
- **Opus baseline:** $50–60 (300M+ tokens, traditional approach)
- **Savings:** $43–53 (87% cost reduction, 94% token reduction)

### Token Distribution by Routing Tier
Free-first routing achieved:
- **31% from free models** — Ollama (local) + Codex (prepaid): 7.0M tokens, $0 cost
- **38% from budget models** — Gemini Flash + GPT-4o-mini: 8.6M tokens, $2.82 cost  
- **31% from premium models** — GPT-4o, Gemini Pro, Claude: 7.0M tokens, $4.13 cost

### Monthly Projection
- **1 sprint (14 days):** $6.95
- **1 month (~30 days):** ~$15
- **1 year:** ~$180

Compare: Claude Opus baseline for same work = **$1,200–1,500/year**

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
| Gemini CLI | `llm-router install --host gemini-cli` |

## Supported Development Tools

llm-router works as an MCP server inside any tool that supports MCP, providing unified routing across your entire development environment.

| Tool | Status | What You Get |
|------|--------|--------------|
| **Claude Code** | ✅ Full | Auto-routing hooks + session tracking + quota display |
| **Gemini CLI** | ✅ Full | Auto-routing hooks + session tracking + quota display |
| **Codex CLI** | ✅ Full | Auto-routing hooks + savings tracking |
| **VS Code + Copilot** | ✅ MCP | llm-router tools available (routing is model-voluntary) |
| **Cursor** | ✅ MCP | llm-router tools available (routing is model-voluntary) |
| **OpenCode** | ✅ MCP | llm-router tools available (routing is model-voluntary) |
| **Windsurf** | ✅ MCP | llm-router tools available (routing is model-voluntary) |
| **Any MCP-compatible tool** | ⚡ Manual | Add llm-router to your tool's MCP config |

### Full Support vs MCP Support

**Full support** = auto-routing hooks fire before the model answers, enforcing your routing policy.
**MCP support** = tools are available, but the model chooses whether to use them.

### Quick Setup by Tool

#### Claude Code
```bash
pipx install claude-code-llm-router
llm-router install
```
Then in Claude Code, llm_route and friends appear as built-in tools. Your settings control the profile (budget/balanced/premium).

#### Gemini CLI
```bash
pipx install claude-code-llm-router
llm-router install --host gemini-cli
```
Gemini CLI users get full routing experience: auto-routing suggestions, quota display, and free-first chaining (Ollama → Codex → Gemini CLI → paid).

#### Codex CLI
```bash
pipx install claude-code-llm-router
llm-router install --host codex
```
Codex integrates deep into the routing chain as a free fallback when your OpenAI subscription is available.

#### VS Code / Cursor / Others
```bash
pipx install claude-code-llm-router
llm-router install --host vscode  # or --host cursor
```
The MCP server loads automatically. Tools appear in your IDE's model UI.

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

## New in v7.0.0 — Free-First MCP Chain & Ollama Auto-Startup

**Major release with optimized routing chains and automatic Ollama management.**

- **Ollama Auto-Startup** — Session-start hook automatically launches Ollama and loads budget models (gemma4, qwen3.5) if not running
  - Eliminates manual setup — local free inference available immediately
  - Graceful fallback if Ollama unavailable
  - 10-second readiness timeout with model auto-pull

- **Free-First MCP Chain for All Complexity Levels**
  - **Simple tasks** → Ollama → Codex → Gemini Flash → Groq
  - **Moderate tasks** → Ollama → Codex → Gemini Pro (improved quality-to-cost) → GPT-4o → Claude Sonnet
  - **Complex tasks** → Ollama → Codex → o3 → Gemini Pro → Claude Opus
  - Codex injected before all paid externals as free fallback when subscription available

- **BALANCED Tier Chain Reordering** — Gemini Pro prioritized after Codex injection
  - Previously defaulted to expensive DeepSeek for moderate tasks
  - Now balances cost + quality: Codex → Gemini Pro (better ROI) → paid fallbacks
  - Reduces BALANCED tier spend ~40% while maintaining output quality

- **Routing Decision Logging & Analytics**
  - Track which model selected for each task, cost impact, complexity distribution
  - Session-end hook shows routing summary with savings vs. full-Opus baseline
  - Identify anomalies (e.g., high-cost tasks that should route cheaper)

See [CHANGELOG.md](CHANGELOG.md) for full version history and v6.x features.

## New in v7.4.0 — Content Generation Routing Discipline

**Smart content generation detection with automatic routing suggestions.**

- **Automatic Content Generation Detection** — Hook detects "write", "draft", "add card", "create spec" patterns
  - Prevents routing misses where content-generation tasks skip `llm_generate` routing
  - Suggests decomposition: route generation first, integrate locally second
  - Example: "add carousel card about X to file.md" → auto-routes via `llm_generate`

- **Decomposition Patterns** — Multi-step content+file tasks now route intelligently
  - "Generate narrative" → `llm_generate` → Done
  - "Add card to blueprint" → `llm_generate` content → `Edit` file integration
  - Cost impact: ~90% savings on writing tasks (route cheap model vs. expensive local generation)

- **Soft Nudges via Hook Suggestion** (not blocking)
  - Detects multi-step content generation patterns
  - Suggests: "Consider routing via `llm_generate` first, then integrate locally"
  - Enforces routing discipline without forcing user behavior

- **Fast-Path for Content Tasks** — Content generation routed instantly without waiting for classifier
  - Patterns: simple generation, decomposition, refinement, documentation
  - Same speed as code detection fast-path
  - Seamless fallback if pattern doesn't match

See [CLAUDE.md § Content Generation Routing](CLAUDE.md#content-generation-routing-v740) for detailed decision tree.

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
