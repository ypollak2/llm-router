# LLM Router Tools Reference

Complete documentation of all 48 MCP tools provided by llm-router for Claude Code, Cursor, VS Code, Codex, and other AI development environments.

---

## Quick Reference

**[Routing Tools](#routing-tools)** — Core routing & model selection
**[Text Tools](#text-tools)** — Query, research, generate, analyze, code
**[Media Tools](#media-tools)** — Image, video, audio generation
**[Pipeline Tools](#pipeline-tools)** — Multi-step orchestration
**[Admin Tools](#admin-tools)** — Profile, usage, health, budget
**[Setup Tools](#setup-tools)** — Configuration, reports, sessions
**[Filesystem Tools](#filesystem-tools)** — Find, rename, bulk edit
**[Subscription Tools](#subscription-tools)** — Claude usage tracking

---

## Routing Tools

Core routing intelligence for intelligent model selection.

### `llm_classify`

Classify a prompt's complexity and get model recommendations.

**Parameters:**
- `prompt` (required) — Task or question to analyze
- `quality` (optional) — Override mode: "best", "balanced", "conserve"
- `min_model` (optional) — Override minimum: "haiku", "sonnet", "opus"

**Returns:** Classification with complexity, confidence, recommended model, budget pressure bar

**Use When:** You need to understand task complexity before choosing a model, or want to see budget pressure impact on routing

**Example:**
```
User: "What's the most efficient model for code review?"
Tool: llm_classify("Review this 500-line Python module")
Result: → Moderate complexity, recommend Sonnet (65% budget), budget pressure: 42%
```

---

### `llm_route`

Route a task to the optimal model from the configured chain.

**Parameters:**
- `prompt` (required) — Task to route and execute
- `task_type` (optional) — Hint: "code", "query", "research", "generate", "analyze"
- `complexity_override` (optional) — Skip classification, force: "simple", "moderate", "complex"
- `system_prompt` (optional) — System instructions for the model
- `temperature` (optional) — Sampling temperature (0.0-2.0)
- `max_tokens` (optional) — Maximum output tokens

**Returns:** LLM response, model used, tokens consumed, cost

**Use When:** You want automatic intelligent routing with full control over parameters

**Example:**
```
User: "I need to write a unit test for this function"
Tool: llm_route("Write pytest tests for this async function...", task_type="code")
Result: → Routes to Ollama (free) or Sonnet (if Ollama unavailable)
```

---

### `llm_auto`

Auto-routing wrapper with persistent savings tracking.

**Parameters:** Same as `llm_route`

**Returns:** Same as `llm_route`, plus cumulative session savings

**Use When:** You're in a host without hook support (Codex CLI, GitHub Copilot) and need savings tracking

**Advantage:** Works from any host; flushes savings records before routing for accuracy

---

### `llm_stream`

Stream responses for long-running tasks (shows output as it arrives).

**Parameters:** Same as `llm_route`, plus:
- `model` (optional) — Explicit model override

**Returns:** Streamed response chunks

**Use When:** Generating long-form content (writing, brainstorming, research) where partial output is valuable

---

### `llm_select_agent`

Classify a task and recommend which Claude Code agent to use.

**Parameters:**
- `prompt` (required) — Task description
- `profile` (optional) — Routing profile: "budget", "balanced", "premium"

**Returns:** Recommended agent (claude_code/codex/gemini_cli), model, task type, complexity, confidence

**Use When:** Deciding which agent CLI to use for a task (Claude Code vs Codex vs Gemini CLI)

---

### `llm_track_usage`

Report Claude Code model token usage for budget tracking.

**Parameters:**
- `model` (required) — Model used: "haiku", "sonnet", "opus"
- `tokens_used` (required) — Approximate tokens consumed
- `complexity` (optional) — Task type: "simple", "moderate", "complex"

**Returns:** Cumulative savings summary

**Use When:** You've manually invoked a model and want to record usage for budget tracking

---

### `llm_reroute`

Override last routing decision and record feedback for learning.

**Parameters:**
- `to_tool` (required) — Tool to use instead (e.g., "llm_analyze", "llm_code")
- `reason` (optional) — Why the original routing was wrong
- `original_tool` (optional) — Tool that made wrong decision
- `original_model` (optional) — Model that was selected

**Returns:** Confirmation with routing correction recorded

**Use When:** You think the router chose the wrong model; feedback improves future decisions

---

## Text Tools

High-level task routing for common AI work.

### `llm_query`

Simple factual questions and lookups.

**Parameters:**
- `prompt` (required) — Question to answer
- `complexity` (optional) — Override: "simple", "moderate", "complex"
- `model` (optional) — Explicit model override
- `system_prompt` (optional) — Custom instructions
- `max_tokens` (optional) — Output limit

**Use When:** Asking factual questions, quick lookups, "how does X work?"

**Routing:** Haiku/Gemini Flash → Sonnet → Opus (based on complexity)

---

### `llm_research`

Web-grounded research with current information.

**Parameters:**
- `prompt` (required) — Research question
- `system_prompt` (optional) — Custom instructions
- `max_tokens` (optional) — Output limit

**Use When:** Research requires current events, recent data, or web sources

**Routing:** Perplexity (web-grounded) with fallback to search-enabled models

---

### `llm_generate`

Creative and long-form content generation.

**Parameters:**
- `prompt` (required) — What to generate (writing, brainstorming, content)
- `complexity` (optional) — Task complexity
- `system_prompt` (optional) — Tone, format, audience
- `temperature` (optional) — Creativity level (higher = more creative)
- `max_tokens` (optional) — Output limit

**Use When:** Writing articles, brainstorming ideas, creating content

**Routing:** Gemini Flash (simple) → GPT-4o (moderate) → o3 (complex)

---

### `llm_analyze`

Deep analysis with strong reasoning.

**Parameters:**
- `prompt` (required) — What to analyze
- `complexity` (optional) — Override: "simple", "moderate", "complex"
- `system_prompt` (optional) — Analysis instructions
- `max_tokens` (optional) — Output limit

**Use When:** Debug complex issues, compare options, deep code review, architectural decisions

**Routing:** Sonnet (moderate) → Opus (complex) for maximum reasoning

---

### `llm_code`

Code generation and refactoring.

**Parameters:**
- `prompt` (required) — Coding task
- `complexity` (optional) — Override complexity
- `system_prompt` (optional) — Language, framework, style
- `max_tokens` (optional) — Output limit

**Use When:** Generate code, refactor, algorithm design, improve performance

**Routing:** Haiku (simple) → Sonnet (moderate) → Opus (complex)

---

### `llm_edit`

Bulk code edits with smart diff generation.

**Parameters:**
- `task` (required) — What to change (natural language)
- `files` (required) — List of file paths to modify
- `context` (optional) — Conversation context

**Returns:** JSON array of `{file, old_string, new_string}` edit instructions

**Use When:** Refactoring multiple files, bulk renames, applying fixes across codebase

**How It Works:**
1. Cheap model reads files
2. Generates exact edit instructions
3. You apply mechanically with Edit tool
4. 50-90% cost savings vs manual edits

---

## Media Tools

Image, video, and audio generation.

### `llm_image`

Generate images with auto-routing to best generation model.

**Parameters:**
- `prompt` (required) — Image description
- `model` (optional) — Override: "gemini/imagen-3", "openai/dall-e-3", "fal/flux-pro", "stability/stable-diffusion-3"
- `size` (optional) — Image size (e.g., "1024x1024", "1792x1024")
- `quality` (optional) — "standard" or "hd" (DALL-E only)

**Returns:** Image URL or base64 encoded image

---

### `llm_video`

Generate videos with auto-routing.

**Parameters:**
- `prompt` (required) — Video description
- `model` (optional) — Override: "gemini/veo-2", "runway/gen3a_turbo", "fal/kling-video"
- `duration` (optional) — Video length in seconds (default: 5)

**Returns:** Video URL

---

### `llm_audio`

Text-to-speech generation.

**Parameters:**
- `text` (required) — Text to convert
- `model` (optional) — Override: "openai/tts-1-hd", "elevenlabs/eleven_multilingual_v2"
- `voice` (optional) — Voice selection

**Returns:** Audio URL or file

---

## Pipeline Tools

Multi-step orchestration across multiple models.

### `llm_orchestrate`

Chain multiple reasoning, analysis, and generation steps.

**Parameters:**
- `task` (required) — Complex task description
- `template` (optional) — Pipeline template: "research_report", "competitive_analysis", "content_pipeline", "code_review_fix"

**Use When:** Task requires research → analysis → generation sequence

**Example:**
```
llm_orchestrate(
  "Create a blog post comparing React vs Vue",
  template="content_pipeline"
)
→ Perplexity (research) → Sonnet (analyze) → GPT-4o (write)
```

---

### `llm_pipeline_templates`

List available pipeline templates.

**Returns:** Available templates with descriptions and use cases

---

## Admin Tools

Profile management, usage tracking, health monitoring.

### `llm_set_profile`

Switch routing profile.

**Parameters:**
- `profile` (required) — "budget", "balanced", or "premium"

**Returns:** Confirmation with new routing behavior

**Profiles:**
- **budget** — Ollama → Codex → Gemini Flash (minimal cost)
- **balanced** — Ollama → Codex → GPT-4o → Claude Sonnet (default)
- **premium** — Ollama → Codex → o3 → Claude Opus (best quality)

---

### `llm_usage`

Show real-time usage dashboard.

**Parameters:**
- `period` (optional) — "today", "week", "month", "all"

**Returns:** Formatted usage table with costs per model

---

### `llm_savings`

Show cost savings from routing.

**Parameters:** None (reads cached session data)

**Returns:** Savings table with efficiency multiplier (how many $ saved per $1 spent)

---

### `llm_health`

Check provider health status.

**Returns:** Health status of all 20+ providers with circuit breaker state

---

### `llm_providers`

List all configured providers and API status.

**Returns:** Provider table with availability, features, cost/token

---

### `llm_dashboard`

Open web dashboard showing real-time routing stats.

**Parameters:**
- `port` (optional) — TCP port for dashboard (default: 7337)

**Returns:** URL to access dashboard (http://localhost:PORT)

---

### `llm_hook_health`

Check health of auto-routing hooks.

**Returns:** Hook status, success/error counts, recent errors

---

### `llm_quality_guard`

Show quality scores per model with degradation alerts.

**Returns:** Model quality table with trend arrows (↑↓→) and alerts if score < 0.7

---

### `llm_budget`

Check real-time budget pressure across providers.

**Returns:** Budget summary with pressure bars per provider (Claude sub, API keys, local)

---

## Setup Tools

Configuration, reports, session management.

### `llm_setup`

Set up and manage API providers, hooks, and routing enforcement.

**Parameters:**
- `action` (required) — "status", "guide", "discover", "add", "test", "provider", "install_hooks", "uninstall_hooks"
- `provider` (optional) — Provider name ("openai", "gemini", "anthropic")
- `api_key` (optional) — API key for "add" action

**Use When:** Configuring providers, testing API keys, installing auto-routing hooks

---

### `llm_save_session`

Save current session state for cross-session context.

**Use When:** Ending session; enables next session to have awareness of prior work

---

### `llm_quality_report`

Generate model classification accuracy report.

**Parameters:**
- `days` (optional) — Time period (default: 7 days)

**Returns:** Routing accuracy metrics, downshift frequency, cost efficiency

---

### `llm_benchmark`

Show routing accuracy benchmarks by task type.

**Returns:** Accuracy by task type (code, analyze, generate, etc.)

---

## Filesystem Tools

File operations with smart routing.

### `llm_fs_find`

Find files matching natural language description.

**Parameters:**
- `description` (required) — What you're looking for (e.g., "all Python files that import sqlite3")
- `root` (optional) — Directory to search

**Returns:** Glob/grep commands to execute

---

### `llm_fs_rename`

Generate safe rename/reorganization commands.

**Parameters:**
- `description` (required) — Rename operation (e.g., "move all test_*.py files from tests/unit/ into tests/")
- `dry_run` (optional) — Preview mode (default: true)

**Returns:** Echo-prefixed or executable commands

---

### `llm_fs_edit_many`

Bulk edit across multiple files.

**Parameters:**
- `task` (required) — What to change (e.g., "replace all `import sqlite3` with `import aiosqlite`")
- `files` (optional) — Explicit file list
- `glob_pattern` (optional) — Or use glob to find files
- `max_files` (optional) — Cap on files processed (default: 20)

**Returns:** JSON array of edit instructions

---

### `llm_fs_analyze_context`

Analyze workspace for routing context.

**Parameters:**
- `path` (optional) — Workspace root
- `max_files` (optional) — Files to scan (default: 20)

**Returns:** Semantic context summary for smarter routing

---

## Subscription Tools

Claude subscription usage and monitoring.

### `llm_check_usage`

Get Claude subscription usage (session limits, weekly, extra spend).

**Returns:** JS snippet or cached usage data

---

### `llm_update_usage`

Update usage cache from Claude API response.

**Parameters:**
- `data` (required) — JSON from claude.ai usage API

---

### `llm_refresh_claude_usage`

Refresh Claude usage via OAuth (macOS only).

**Requires:** Claude Code installed and authenticated

---

## Team & Collaboration

### `llm_team_report`

Generate team savings report.

**Parameters:**
- `period` (optional) — "today", "week", "month", "all"

**Returns:** Team usage and savings, auto-detected from git

---

### `llm_team_push`

Push team report to Slack/Discord/Telegram.

**Parameters:**
- `period` (optional) — Time range

**Uses:** `LLM_ROUTER_TEAM_ENDPOINT` environment variable

---

## Advanced Tools

### `llm_codex`

Route to local Codex desktop agent (OpenAI).

**Parameters:**
- `prompt` (required) — Task
- `model` (optional) — OpenAI model

---

### `llm_rate`

Rate last routing decision (thumbs up/down).

**Parameters:**
- `good` (required) — true or false
- `decision_id` (optional) — Which decision to rate (defaults to last)

---

### `llm_policy`

Show active routing policy and enforcement events.

---

### `llm_digest`

Generate savings digest with spend anomaly detection.

**Parameters:**
- `period` (optional) — Time range
- `send` (optional) — POST to webhook

---

## Tool Selection Matrix

| Question | Tool |
|----------|------|
| Should I use Haiku or Sonnet for this? | `llm_classify` |
| I have a web research task | `llm_research` |
| I need to write code | `llm_code` |
| I need deep analysis of a bug | `llm_analyze` |
| I need creative content | `llm_generate` |
| I have a complex pipeline | `llm_orchestrate` |
| How much am I saving? | `llm_savings` |
| Is my API key working? | `llm_setup` (action="test") |
| Which models are healthy? | `llm_health` |
| How's my budget? | `llm_budget` |
| Bulk refactor code | `llm_fs_edit_many` |
| Research + analysis + writing | `llm_orchestrate` (template="research_report") |

---

## Common Patterns

### Pattern: Optimize for Cost
```
1. llm_classify(prompt) → see complexity
2. Set profile to "budget"
3. llm_route(prompt) → uses cheap models
4. llm_savings() → track savings
```

### Pattern: Deep Analysis
```
1. llm_analyze(complex_problem)
→ Routes to Sonnet/Opus (strong reasoning)
2. llm_rate(true/false)
→ Feedback improves classification
```

### Pattern: Multi-Step Research
```
llm_orchestrate(
  task="Research and summarize...",
  template="research_report"
)
→ Perplexity (research) → Sonnet (analyze) → GPT-4o (summarize)
```

### Pattern: Bulk Code Refactor
```
1. llm_fs_edit_many(
     task="Replace import X with Y",
     glob_pattern="src/**/*.py"
   )
2. Review JSON edits
3. Apply with Edit tool
```

---

## Performance Tips

- Use `llm_query` for simple questions (cheaper, faster)
- Use `llm_classify` to understand complexity before deciding
- Batch filesystem operations with `llm_fs_edit_many`
- Monitor `llm_budget` when approaching limits
- Use `llm_health` to find best available model during provider outages
- Check `llm_quality_report` weekly to catch model degradation

---

## Troubleshooting

**"Model X is degraded"** → Check `llm_quality_guard` for score trends

**"Budget pressure too high"** → Switch to "budget" profile with `llm_set_profile`

**"Hook not running"** → Run `llm_setup action=install_hooks`

**"API key failing"** → Run `llm_setup action=test provider=openai`

**"Which model was used?"** → Check `llm_usage` or `llm_savings`

---

## See Also

- [README.md](../README.md) — Overview and quick start
- [CLAUDE.md](../CLAUDE.md) — Project configuration
- [Router Architecture](./architecture.md) — How routing works internally
