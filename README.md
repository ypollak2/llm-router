<p align="center">
  <img src="docs/llm-router-header.png" alt="LLM Router" width="100%">
</p>

<p align="center">
  <em>Route every AI call to the cheapest model that can do the job well.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/llm-routing/"><img src="https://img.shields.io/pypi/v/llm-routing?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://github.com/ypollak2/llm-router/actions"><img src="https://img.shields.io/github/actions/workflow/status/ypollak2/llm-router/ci.yml?style=flat-square&label=tests" alt="Tests"></a>
  <a href="https://pypi.org/project/llm-routing/"><img src="https://img.shields.io/pypi/dm/llm-routing?style=flat-square" alt="Downloads"></a>
  <a href="https://pypi.org/project/llm-routing/"><img src="https://img.shields.io/badge/python-3.10–3.13-blue?style=flat-square" alt="Python"></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-1.0+-purple?style=flat-square" alt="MCP"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/ypollak2/llm-router/stargazers"><img src="https://img.shields.io/github/stars/ypollak2/llm-router?style=flat-square&color=yellow" alt="Stars"></a>
</p>

<p align="center">
  <b>48 MCP tools</b> · <b>20+ providers</b> · <b>intelligent routing</b> · <b>60–80% cost reduction</b>
</p>

<p align="center">
  <a href="#quick-start"><b>Quick Start</b></a> ·
  <a href="docs/SETUP.md"><b>Docs</b></a> ·
  <a href="docs/TOOLS.md"><b>Tool Reference</b></a> ·
  <a href="CHANGELOG.md"><b>Changelog</b></a> ·
  <a href="https://github.com/ypollak2/llm-router/issues"><b>Issues</b></a>
</p>

---

## The Problem

Every AI coding assistant routes **every task** to the most expensive model. A simple "what does this error mean?" burns the same tokens as "design a distributed tracing system."

**You're overpaying by 5–10x on 70% of your tasks.**

## The Solution

```
pip install llm-routing && llm-router install
```

llm-router analyzes each task's complexity and routes it to the cheapest model that can handle it:

```
Simple  → Ollama / Haiku      (free or $0.0001)
Moderate → Gemini Pro / GPT-4o  (budget-friendly)
Complex  → Claude Opus / o3     (premium, only when needed)
```

No manual model picking. No workflow changes. It just works.

---

## Real-World Results

> **51 releases · 22.6M tokens · $6.95 spent** — vs $50–60 with Opus-everywhere.

<table>
<tr>
<td width="50%">

### Cost Impact

| Metric | Value |
|--------|-------|
| Actual spend | **$6.95** |
| Opus baseline | $50–60 |
| Savings | **87% reduction** |
| Annualized | ~$180/yr vs $1,500/yr |

</td>
<td width="50%">

### Token Distribution

```
 31%  Free models     7.0M tokens   $0.00
 38%  Budget models   8.6M tokens   $2.82
 31%  Premium models  7.0M tokens   $4.13
 ─────────────────────────────────────────
100%  Total          22.6M tokens   $6.95
```

</td>
</tr>
</table>

<p align="center">
  <img src="docs/slides/19.png" alt="Cost Breakdown" width="48%">
  <img src="docs/slides/20.png" alt="Token Distribution" width="48%">
</p>

---

## Quick Start

### 1. Install

```bash
pip install llm-routing && llm-router install
```

### 2. Add provider keys (optional)

```bash
export OPENAI_API_KEY="sk-..."     # GPT-4o, o3
export GEMINI_API_KEY="AIza..."    # Gemini (free tier available)
export OLLAMA_BASE_URL="..."       # Local Ollama (auto-starts)
```

> Works with **zero config** on Claude Code Pro/Max subscriptions. No API keys needed.

### 3. Done

Open Claude Code, Gemini CLI, Codex, VS Code, Cursor, or any MCP editor. Routing is automatic.

---

## How It Works

```
User Prompt
     │
     ▼
┌─────────────────────┐
│  Heuristic Fast-Path │ ← pattern match (instant, free)
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Complexity Classifier│ ← local or cheap API
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  Free-First Router   │ ← Ollama → Codex → Gemini → OpenAI → Claude
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  Budget + Health     │ ← pressure adjustment + circuit breakers
└──────────┬──────────┘
           ▼
    Execute on Best Model
    Track decision & cost
```

<details>
<summary><b>Example: Simple task</b> — "What does this error mean?"</summary>

```
Ollama (free, local)
  ↓ if unavailable
Codex gpt-5.4 (prepaid)
  ↓ if unavailable
Gemini Flash ($0.0001/1M tokens)
  ↓ if degraded
Groq (free tier)
  ↓
GPT-4o-mini (fallback)
```

</details>

<details>
<summary><b>Example: Moderate task</b> — "Implement OAuth authentication"</summary>

```
Ollama (free, local)
  ↓ if unavailable
Codex (prepaid)
  ↓ if unavailable
Gemini Pro (quality+cost sweet spot)
  ↓ if degraded
GPT-4o (moderate cost)
  ↓
Claude Sonnet (subscription)
```

</details>

<details>
<summary><b>Example: Complex task</b> — "Design a distributed tracing system"</summary>

```
Ollama (free, local)
  ↓ if unavailable
Codex (prepaid)
  ↓ if unavailable
o3 (reasoning powerhouse)
  ↓ if unavailable
Claude Opus (max reasoning)
```

</details>

---

## Features

<table>
<tr>
<td width="50%" valign="top">

### Intelligent Routing

- **Complexity classification** — simple / moderate / complex
- **Free-first chains** — Ollama → Codex → paid APIs
- **Budget pressure awareness** — auto-downgrades near quota
- **Quality monitoring** — demotes degraded models
- **Decision logging** — every choice tracked with cost

</td>
<td width="50%" valign="top">

### Cost Optimization

- **Zero-config** — works with Claude subscription out of the box
- **Session budgeting** — prevents runaway Agent costs
- **Real-time savings** — see exact $ saved per session
- **Caveman mode** — compress output tokens 65–87%
- **Usage analytics** — breakdowns by model, task, tier

</td>
</tr>
<tr>
<td width="50%" valign="top">

### Adaptive Learning

- **Personal routing memory** — learns your patterns
- **Judge scores** — real-time model quality monitoring
- **Session context** — detects code vs research vs Q&A
- **Policy enforcement** — prevents expensive violations

</td>
<td width="50%" valign="top">

### Comprehensive Monitoring

- **Savings dashboard** — session / week / month / all-time
- **Quota pressure** — real-time budget remaining
- **Violation detection** — logs when hints are ignored
- **Performance reports** — quality trends, model health

</td>
</tr>
</table>

---

## Universal Compatibility

<table>
<tr>
<td align="center" width="16%"><b>Claude Code</b><br><code>Full</code></td>
<td align="center" width="16%"><b>Gemini CLI</b><br><code>Full</code></td>
<td align="center" width="16%"><b>Codex CLI</b><br><code>Full</code></td>
<td align="center" width="16%"><b>VS Code</b><br><code>MCP</code></td>
<td align="center" width="16%"><b>Cursor</b><br><code>MCP</code></td>
<td align="center" width="16%"><b>Any MCP</b><br><code>MCP</code></td>
</tr>
</table>

**Full** = auto-routing hooks enforce your policy before the model responds.
**MCP** = 48 tools available, model decides when to use them.

```bash
llm-router install                    # Claude Code (default)
llm-router install --host gemini-cli  # Gemini CLI
llm-router install --host codex       # Codex CLI
llm-router install --host vscode      # VS Code
llm-router install --host cursor      # Cursor
```

---

## Configuration

<details>
<summary><b>Environment variables</b></summary>

```bash
# Provider API Keys (only set what you have)
export OPENAI_API_KEY="sk-proj-..."          # GPT-4o, o3
export GEMINI_API_KEY="AIza..."              # Gemini models
export PERPLEXITY_API_KEY="pplx-..."         # Web-grounded research
export ANTHROPIC_API_KEY="sk-ant-..."        # Non-subscription Claude

# Local Inference (Free)
export OLLAMA_BASE_URL="http://localhost:11434"
export OLLAMA_BUDGET_MODELS="gemma4:latest,qwen3.5:latest"

# Routing Policy
export LLM_ROUTER_PROFILE="balanced"         # budget | balanced | premium
export LLM_ROUTER_ENFORCE="smart"            # smart | hard | soft | off
export LLM_ROUTER_CAVEMAN_INTENSITY="full"   # off | lite | full | ultra
```

</details>

<details>
<summary><b>Enterprise config file</b> (for teams with security policies blocking .env)</summary>

```bash
llm-router init-config
# Edit ~/.llm-router/config.yaml
chmod 600 ~/.llm-router/config.yaml
```

```yaml
openai_api_key: "sk-proj-..."
gemini_api_key: "AIza..."
ollama_base_url: "http://localhost:11434"
llm_router_profile: "balanced"
```

</details>

<details>
<summary><b>Routing policies</b></summary>

| Policy | Threshold | Savings | Best For |
|--------|-----------|---------|----------|
| **Aggressive** | 2 | 60–75% | Maximum savings |
| **Balanced** | 4 | 35–45% | Cost/quality tradeoff (default) |
| **Conservative** | 6 | 10–15% | Quality over cost |

```bash
export LLM_ROUTER_POLICY=aggressive
# or
llm-router init-policy  # interactive wizard
```

</details>

---

## MCP Tools (48)

<details>
<summary><b>Routing & Classification</b> (3 tools)</summary>

| Tool | Purpose |
|------|---------|
| `llm_route` | Route task to optimal model by complexity/profile |
| `llm_classify` | Classify task complexity: simple / moderate / complex |
| `llm_track_usage` | Manually log token usage for budget tracking |

</details>

<details>
<summary><b>Text Generation</b> (6 tools)</summary>

| Tool | Purpose |
|------|---------|
| `llm_query` | Answer questions (Haiku-class, fast) |
| `llm_research` | Research with web access (Perplexity) |
| `llm_generate` | Create content (Flash-class, cheap) |
| `llm_analyze` | Deep analysis (Sonnet-class reasoning) |
| `llm_code` | Code generation & refactoring |
| `llm_edit` | Multi-file code edits with reasoning |

</details>

<details>
<summary><b>Media Generation</b> (3 tools)</summary>

| Tool | Purpose |
|------|---------|
| `llm_image` | Generate images (Gemini / DALL-E / Flux) |
| `llm_video` | Generate videos (Gemini Veo / Runway) |
| `llm_audio` | Generate speech (ElevenLabs / OpenAI TTS) |

</details>

<details>
<summary><b>Pipeline Orchestration</b> (2 tools)</summary>

| Tool | Purpose |
|------|---------|
| `llm_orchestrate` | Multi-step pipelines (research → analysis → generation) |
| `llm_pipeline_templates` | List available pipeline templates |

</details>

<details>
<summary><b>Admin & Monitoring</b> (6 tools)</summary>

| Tool | Purpose |
|------|---------|
| `llm_usage` | Cost breakdown (today / week / month / all) |
| `llm_savings` | Cost savings vs Opus baseline |
| `llm_budget` | Real-time budget pressure (0.0–1.0) |
| `llm_health` | Provider health & circuit breaker state |
| `llm_providers` | Configured providers & API key status |
| `llm_set_profile` | Switch routing profile |

</details>

<details>
<summary><b>Setup & Configuration</b> (7 tools)</summary>

| Tool | Purpose |
|------|---------|
| `llm_setup` | Interactive provider setup guide |
| `llm_policy` | View / manage routing policies |
| `llm_quality_report` | Judge scores & quality trends |
| `llm_save_session` | Archive session for cross-session learning |
| `llm_check_usage` | Refresh Claude subscription quota |
| `llm_update_usage` | Update usage cache from API response |
| `llm_refresh_claude_usage` | Auto-refresh Claude quota (OAuth) |

</details>

<details>
<summary><b>Advanced</b> (7+ tools)</summary>

| Tool | Purpose |
|------|---------|
| `llm_codex` | Route directly to Codex (prepaid OpenAI) |
| `llm_auto` | Host-agnostic routing wrapper |
| `llm_gemini` | Route directly to Gemini CLI |
| `llm_fs_find` | Find files by description |
| `llm_fs_rename` | Generate bulk rename commands |
| `llm_fs_edit_many` | Multi-file edits with cheap model reasoning |
| `llm_fs_analyze_context` | Build workspace context for routing |

</details>

**[Full Tool Reference with examples](docs/TOOLS.md)**

---

## What's New

<details>
<summary><b>v7.6.0 — Agent Resource Budgeting</b> (Latest)</summary>

- **Session Budget Allocation** — Smart carving: 30% of remaining quota per session
- **Provisional Spend Tracking** — Real-time budget decrements prevent overspend
- **Budget Reconciliation** — Refund 50% on failure (pay only for delivered value)
- **Hard Limits** — $5/agent, $50/session safety valve

</details>

<details>
<summary><b>v7.4.0 — Content Generation Routing</b></summary>

- **Smart Detection** — "write", "draft", "create spec" patterns auto-detected
- **Decomposition** — Route generation, then integrate locally (saves 90% on writing)
- **Soft Nudges** — Hook suggests routing without blocking

</details>

<details>
<summary><b>v7.0.0 — Free-First Chain & Ollama Auto-Startup</b></summary>

- **Ollama Auto-Startup** — Session hook launches Ollama + loads budget models
- **Free-First Chains** — Ollama → Codex → Gemini → OpenAI → Claude
- **Codex as Free Fallback** — Injected before all paid models
- **Routing Analytics** — Track model, cost, complexity per decision

</details>

[Full changelog](CHANGELOG.md)

---

## Comparison

| | llm-router | Manual Routing | Always Opus |
|---|---|---|---|
| **Cost** | $180–360/yr | Varies | $1,200–1,500/yr |
| **Setup** | One command | Manual each time | None |
| **Decision quality** | Learned from usage | Error-prone | Optimal but expensive |
| **Budget control** | Real-time pressure | None | Subscription limits |
| **Provider fallback** | Automatic chain | Manual | Single provider |
| **Learning** | Adapts over time | Static | None |

---

## Security

<table>
<tr>
<td width="50%" valign="top">

**What we do**
- Sanitize inputs against prompt injection
- Scrub API keys from logs before persistence
- Verify hook safety (no deadlocks)
- Store everything locally in `~/.llm-router/`

</td>
<td width="50%" valign="top">

**What you should know**
- Prompts are sent to your configured providers
- API keys stored locally (`.env` or `config.yaml`)
- Usage logs are unencrypted SQLite
- All providers share your routed content

</td>
</tr>
</table>

See [SECURITY.md](SECURITY.md) for responsible disclosure.

---

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

```bash
uv run pytest tests/ -q         # Run tests
uv run ruff check src/ tests/   # Lint
uv build                        # Build
```

See [CLAUDE.md](CLAUDE.md) for architecture decisions and module organization.

---

<p align="center">
  <a href="https://github.com/ypollak2/llm-router/issues"><b>Issues</b></a> ·
  <a href="https://github.com/ypollak2/llm-router/discussions"><b>Discussions</b></a> ·
  <a href="https://pypi.org/project/llm-routing/"><b>PyPI</b></a> ·
  <a href="CHANGELOG.md"><b>Changelog</b></a>
</p>

<p align="center">
  MIT License · Made with care for developers who value both cost and quality.
</p>
