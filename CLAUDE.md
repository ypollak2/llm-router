# LLM Router — Project Instructions

## ⚠️ CRITICAL: Hook Deadlock Prevention

**NEVER configure enforce-route or any hook to block Claude's core tools.**
The following tools must ALWAYS be allowed, unconditionally:
`Read, Edit, MultiEdit, Write, Bash, Grep, Glob, LS, ToolSearch, Agent`

### Why This Matters

Blocking these creates an **unresolvable deadlock**:
1. Hook blocks Read/Edit/Bash (to enforce routing)
2. Claude tries to investigate or fix the issue
3. Hook blocks those tools as violations
4. Claude cannot read the hook source code to understand/fix it
5. **Session is permanently stuck** — only recovery is manual intervention

This pattern has destroyed 6+ sessions and is why enforce-route.py v5.6.0+ includes early file-op detection and auto-pivot mechanisms.

### Blocklist Safety Rules

**In enforce-route.py:**
- `_BASE_BLOCK_TOOLS` must NOT contain: Read, Glob, Grep, LS, Agent
- `_QA_ONLY_BLOCK_TOOLS` can block file-read tools ONLY in Q&A mode
- File-op detection (line 393) must come BEFORE blocklist check
- If first tool call is Read/Edit/Write → immediately mark "coding" session

**Tests verify this invariant:**
- tests/test_enforce_route_safety.py confirms core tools are never blocked simultaneously
- Run: `uv run pytest tests/test_enforce_route_safety.py -v`

### Recovery Procedure

If you suspect a deadlock:
```bash
export LLM_ROUTER_ENFORCE=off  # Disable enforcement entirely
# Claude can now use Read/Bash/Edit to investigate and fix the hook
# Then remove the env var and restart
```

### Architecture Guarantee

The enforce-route hook is structured to NEVER reach a state where all core tools are blocked together:
- Early file-op detection exits BEFORE blocklist
- Auto-pivot downgrades to soft after 2 violations
- Session-type tracking prevents overly aggressive blocking
- Investigation loop detection warns when stuck patterns form

## Project Overview

This project is **Python**. All new code must be Python unless explicitly asked otherwise.
Use type hints on all public functions, Pydantic for external data, `@dataclass` for domain objects.

## Environment Setup

✅ **User Configuration (Yali)**

Configured in `.env`:
- `OPENAI_API_KEY` — Present (gpt-5.4, o3, gpt-4o, gpt-4o-mini available)
- `GEMINI_API_KEY` — Present (Gemini Flash, Pro available)
- `OLLAMA_BASE_URL=http://localhost:11434` — Running locally
- `OLLAMA_BUDGET_MODELS=gemma4:latest,qwen3.5:latest` — Local free models
- `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` — Claude subscription mode enabled (no ANTHROPIC_API_KEY needed)

**Routing chain in use**:
- Simple tasks → Ollama (gemma4/qwen3.5) → OpenAI (gpt-4o-mini) → Gemini Flash
- Moderate tasks → Ollama → OpenAI (gpt-4o) → Gemini Pro → Claude Sonnet (subscription)
- Complex tasks → Ollama → OpenAI (o3) → Claude Opus (subscription)

**Guidelines for Claude**:
- Do NOT ask "do you have API keys?" — Yali's setup is complete
- Do NOT give generic setup advice — check `.env` and actual config first
- Always verify environment state before answering about LLM availability

## Security-Friendly Configuration (Enterprise)

**Problem**: Security teams often block `.env` files at the project level.

**Solution**: `~/.llm-router/config.yaml` — User-level fallback config file

### Setup

```bash
# Generate config.yaml template (auto-discovers current setup)
llm-router init-claude-memory

# Edit the template (located at ~/.llm-router/config.yaml)
nano ~/.llm-router/config.yaml

# Set secure permissions (readable by user only)
chmod 600 ~/.llm-router/config.yaml
```

### Configuration Priority

1. `.env` (project-level, if readable) — highest priority
2. `~/.llm-router/config.yaml` (user-level fallback)
3. Environment variables (system-wide)
4. Hardcoded defaults

If `.env` is blocked by security policy, the router automatically falls back to `config.yaml`.

### Example `config.yaml`

```yaml
# Text LLM API Keys (leave empty to disable)
openai_api_key: "sk-proj-..."
gemini_api_key: "AIzaSy..."
perplexity_api_key: ""

# Ollama (local inference — free, no API key needed)
ollama_base_url: "http://localhost:11434"
ollama_budget_models: "gemma4:latest,qwen3.5:latest"

# Router settings
llm_router_profile: "balanced"
llm_router_claude_subscription: true
```

### When to Use

- **Team/Enterprise**: Security blocks `.env` → use `config.yaml` instead
- **Multi-Project**: Share credentials across projects without copying `.env` files
- **Simplified Setup**: Only Ollama + minimal keys → just configure `config.yaml`

## Caveman Mode — Token-Efficient Output (v5.9.0)

**Caveman reduces output tokens by ~75%** by removing filler, using fragments, and preserving only technical substance.

### Configuration

```bash
# Enable Caveman (default: "full")
export LLM_ROUTER_CAVEMAN_INTENSITY=full

# Options: off | lite | full | ultra
# - off:   Disable Caveman mode (use default verbose output)
# - lite:  Professional, readable, minimal filler (Recommended)
# - full:  Standard caveman with fragments (Max savings, still readable)
# - ultra: Telegraphic, maximum compression (Use with caution)
```

Or in `.env` / `config.yaml`:
```yaml
caveman_mode: "full"
```

### Example Output (Caveman vs Normal)

**Problem**: React component re-renders on every parent update.

**Normal** (69 tokens):
> "I think the best approach would be to basically use the `useMemo` hook here because it prevents unnecessary re-renders, which is really important for performance in React applications. The `useMemo` hook memoizes the component so it only re-renders when its dependencies change, rather than re-rendering on every parent update."

**Caveman Full** (18 tokens):
> "Wrap in useMemo. Prevents re-renders on parent update."

**Caveman Ultra** (12 tokens):
> "useMemo. Prevents parent re-renders."

### How It Works

Caveman applies structured terseness rules:
- **Removes**: "I think", "basically", "just", "really", articles ("a"/"the") when omittable
- **Preserves**: Code, file paths, command syntax, all technical detail
- **Uses**: Fragments ("Returns mutated object" not "This returns a mutated object")
- **Leads**: With answer, not explanation

### When to Use

- **Code generation & refactoring**: Fragment output is natural, saves tokens
- **Research & analysis**: Terse answers to direct questions work well
- **Long conversations**: 75% savings multiply across many turns
- **Budget-constrained sessions**: Max token efficiency

### When NOT to Use

- **Long-form content**: Articles, blog posts (brevity compromises quality)
- **Specialized domains**: Medical, legal (terseness risks critical detail)
- **User-provided system prompts**: Caveman only injects when no custom system message

## Testing

**Fast tests (10–15 seconds)** — dev iteration during active coding:
```bash
uv run pytest tests/ -q  # Runs only unmarked "fast" tests by default
uv run pytest tests/test_classifier.py -x -q  # Single file
```

**Full test suite (30–45 seconds)** — before commits:
```bash
uv run pytest tests/ -m "" -q  # Include @pytest.mark.slow tests
```

**Parallel execution** (faster on multi-core machines):
```bash
uv run pytest tests/ -n auto -q  # Uses all CPU cores
uv run pytest tests/ -n 4 -m "" -q  # Full suite in parallel
```

Never use bare `pytest` — it will fail without the venv context `uv run` provides.
All test runs automatically skip `test_agno_integration.py` (too slow/flaky).

## Version Management

Every user-facing change requires ALL of these files to stay in sync:
- `pyproject.toml` → `project.version`
- `.claude-plugin/plugin.json` → `version`
- `.claude-plugin/marketplace.json` → `version`

Verify sync before any commit:
```bash
python3 -c "
import tomllib, json
v1 = tomllib.load(open('pyproject.toml','rb'))['project']['version']
v2 = json.load(open('.claude-plugin/plugin.json'))['version']
v3 = json.load(open('.claude-plugin/marketplace.json'))['version']
assert v1==v2==v3, f'VERSION MISMATCH: pyproject={v1} plugin={v2} marketplace={v3}'
print(f'✅ All versions in sync: {v1}')
"
```

## Decision Logging

After completing any significant feature or architectural decision, append to `docs/decisions.md`:
```
## YYYY-MM-DD — <feature name>
**Decision**: what was decided
**Alternatives considered**: what else was evaluated
**Outcome**: result, any caveats
```
This replaces the need for separate observer/memory agent sessions.

## Auto-Routing Rule

When a user's task would clearly benefit from an external LLM — research requiring web access, content generation, deep analysis beyond your training data, or code generation that could leverage a specialized model — **automatically use the appropriate `llm_*` MCP tool** without the user needing to type `/route`.

### Decision Matrix

| Signal in user prompt | Action |
|---|---|
| Research, current events, "what's the latest" | `llm_research` |
| "Write", "draft", "create content", brainstorming | `llm_generate` |
| Deep analysis, compare, evaluate, debug complex logic | `llm_analyze` |
| "Generate code", "implement", "refactor" (for external model) | `llm_code` |
| Simple factual question, quick lookup | `llm_query` |
| Image/visual request | `llm_image` |
| Multi-step pipeline | `llm_orchestrate` |

### When NOT to auto-route

- When the user explicitly asks YOU to do something (e.g. "you personally explain this to me")

**There are no other exceptions.** Codebase work still routes:
- Planning what to change → `llm_code` or `llm_analyze`
- Understanding how something works → `llm_query`
- File edits (Read/Edit/Write) are execution only — route the thinking first

### CRITICAL: Routing hint = hard constraint, not a suggestion

When `⚡ MANDATORY ROUTE:` appears in context, these actions are **forbidden**:
- Using the **Agent tool** to spawn subagents — this is the #1 violation
- Using WebSearch / WebFetch to answer the question yourself
- Answering from your own knowledge without calling the MCP tool
- Calling the MCP tool AND ALSO doing the research yourself

**One rule**: call the specified tool, return its output, done.

The economic reason: cheap models (Gemini Flash, Haiku) cost 50–100x less than Opus. A "good enough" answer from the cheap model is always correct — that IS the point. Do not optimize for quality over cost when routing is in play.

### Routing Hint

If a `⚡ MANDATORY ROUTE:` directive appears in context, use it to select the right tool and skip re-classification. The directive is injected by the UserPromptSubmit hook's multi-layer classifier:

1. `via heuristic` — High-confidence pattern match (instant, free)
2. `via ollama` — Local LLM classification via qwen3.5 (~1s, free)
3. `via api` — Cheap API classification via Gemini Flash/GPT-4o-mini (~$0.0001)
4. `via heuristic-weak` — Low-confidence pattern match
5. `via fallback` — No classification; `llm_route` should do full analysis

## Model Routing Strategy (v1.8.4)

All routing goes through MCP tools — the hook never emits `/model` directives
because Claude Code's model cannot execute slash commands from context. The
free-first MCP chain keeps costs low in both subscription and API-key modes.

### All Complexity Levels → MCP Tools (free-first chain)

| Complexity | MCP tool | Chain |
|---|---|---|
| `simple` | `llm_query` | Ollama → Codex → Gemini Flash → Groq |
| `moderate` | `llm_analyze` / `llm_generate` / `llm_code` | Ollama → Codex → GPT-4o → Gemini Pro |
| `complex` | `llm_code` / `llm_analyze` | Ollama → Codex → o3 → Gemini Pro |
| `research` | `llm_research` | Perplexity (web-grounded) |

`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` enables inline OAuth refresh (keeps subscription
usage data fresh for session-end delta reporting) but does not change routing behaviour.

### External Fallback Chains (free-first ordering)

Hierarchy: **free-local (Ollama) → free-prepaid (Codex) → paid-per-call**

Codex is injected before all paid externals for CODE, ANALYZE, GENERATE, QUERY tasks.

| Tier | Chain |
|---|---|
| BUDGET (simple) | Ollama → Codex/gpt-5.4 → Codex/o3 → Gemini Flash → Groq → GPT-4o-mini |
| BALANCED (moderate) | Ollama → Codex/gpt-5.4 → Codex/o3 → GPT-4o → Gemini Pro → DeepSeek |
| PREMIUM (complex under pressure) | Ollama → Codex/gpt-5.4 → Codex/o3 → o3 → Gemini Pro |

### Complexity Classifier

| Layer | Tool | Cost |
|---|---|---|
| 1. Heuristics | Regex in auto-route hook | Free, instant |
| 2. Ollama | Local qwen3.5 | Free, ~1-3s |
| 3. Gemini Flash | API fallback | ~$0.0001 |
| MCP `llm_classify` | Haiku → Gemini Flash Lite → Groq | Cheapest available |

> **Pressure data**: Run `llm_check_usage` at session start to populate `~/.llm-router/usage.json`.
> Without it, pressure defaults to 0.0 (subscription models used — correct conservative behavior).

## Development

```bash
# Run tests
uv run pytest tests/ -x -q

# Lint
uv run ruff check src/ tests/

# Run single test
uv run pytest tests/test_classifier.py -x -q

# Build
uv build

# Run server locally
uv run llm-router
```

## Architecture

### Entrypoints
- `src/llm_router/server.py` — Thin MCP entrypoint (~110 lines), calls `register(mcp)` on each tools module
- `src/llm_router/cli.py` — `llm-router install [--check|--force|uninstall]` CLI dispatcher
- `src/llm_router/state.py` — Shared mutable state: `_active_profile`, `_last_usage` with get/set accessors

### Tools Modules (each exposes `register(mcp)`)
- `src/llm_router/tools/routing.py` — `llm_classify`, `llm_route`, `llm_track_usage`, `llm_stream`
- `src/llm_router/tools/text.py` — `llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`, `llm_edit`
- `src/llm_router/tools/media.py` — `llm_image`, `llm_video`, `llm_audio`
- `src/llm_router/tools/pipeline.py` — `llm_orchestrate`, `llm_pipeline_templates`
- `src/llm_router/tools/admin.py` — `llm_set_profile`, `llm_usage`, `llm_health`, `llm_providers`
- `src/llm_router/tools/subscription.py` — `llm_check_usage`, `llm_update_usage`, `llm_refresh_claude_usage`
- `src/llm_router/tools/codex.py` — `llm_codex`
- `src/llm_router/tools/setup.py` — `llm_setup`, `llm_quality_report`, `llm_save_session`

### Core Modules
- `src/llm_router/router.py` — Core routing with fallback chains, Codex injection, pressure-aware ordering
- `src/llm_router/classifier.py` — LLM-based complexity classification
- `src/llm_router/model_selector.py` — Budget-aware model selection
- `src/llm_router/profiles.py` — Routing tables per profile/task_type
- `src/llm_router/types.py` — All dataclasses and enums (frozen)
- `src/llm_router/config.py` — Pydantic settings from env vars
- `src/llm_router/cost.py` — SQLite usage tracking + savings persistence
- `src/llm_router/health.py` — Circuit breaker per provider + rate limit detection
- `src/llm_router/claude_usage.py` — Live Claude subscription monitoring
- `src/llm_router/codex_agent.py` — Local Codex: binary detection, `is_codex_plugin_available()`, `run_codex()`
- `src/llm_router/cache.py` — Prompt classification cache (SHA-256 + LRU)
- `src/llm_router/quality.py` — Routing decision logging + quality reports
- `src/llm_router/install_hooks.py` — Global hook installer (CLI + MCP action)
- `src/llm_router/hooks/` — Bundled hook scripts (auto-route, usage-refresh)
- `src/llm_router/rules/` — Global routing rules for Claude Code

## Patterns

- All dataclasses are `frozen=True` — never mutate, create new instances
- All routing/API calls are `async def`
- Budget pressure is applied fresh per call (never cached)
- Classification results ARE cached (complexity doesn't change with budget)
- MCP tools return formatted strings, not structured data

## Automated Release Process

**One command to release:** `bash scripts/release.sh`

This script automates the entire release process with built-in verification and rollback:

### What the script does (automatically)

1. **Verifies version sync** — ensures pyproject.toml, plugin.json, and marketplace.json match
2. **Runs full test suite** — tests must pass before release
3. **Checks linting** — ruff validation
4. **Builds & publishes to PyPI** — handles token from ~/.pypirc
5. **Creates GitHub release** — tag, release, with changelog extracted
6. **Runs post-release verification** — confirms PyPI availability, GitHub release exists, tests still pass

### Release execution

```bash
# Full automated release (recommended)
bash scripts/release.sh

# Or run individual steps manually (not recommended — use script instead)
python3 scripts/verify-release.py  # Check current release status only
```

### Recovery on failure

If release fails at any step (test failure, PyPI unavailable, GitHub API down):

1. **Script automatically rolls back** — reverts version files, deletes git tags
2. **Displays error** — shows which check failed and why
3. **No manual cleanup needed** — local main is back to previous version
4. **Fix and retry** — address the issue, then run `bash scripts/release.sh` again

Example failure flow:
```
❌ Tests failed
Rolled back to v6.1.0
Fix the tests, then: bash scripts/release.sh
```

### Pre-release checklist (before running script)

Ensure these are done BEFORE calling the release script:

- [ ] All code changes committed: `git status` shows clean working tree
- [ ] CHANGELOG.md updated with new version entry (or script extracts existing)
- [ ] Version bumped in `pyproject.toml` (script validates it matches other files)
- [ ] No uncommitted version changes (script will reject if they exist)

### Version bumping

When ready to release, bump the version in `pyproject.toml`:

```bash
# Patch release (bugfixes): 6.2.0 → 6.2.1
# Minor release (features): 6.1.0 → 6.2.0
# Major release (breaking): 5.0.0 → 6.0.0

# Edit pyproject.toml:
version = "X.Y.Z"

# Then add CHANGELOG entry:
## vX.Y.Z — Release title (YYYY-MM-DD)
### Added/Fixed/Changed
- bullet points
```

The script will then synchronize version across plugin.json and marketplace.json automatically.

### Post-release monitoring

After the script completes successfully:
- Users can upgrade: `pip install --upgrade claude-code-llm-router`
- Plugin updates automatically via marketplace
- All three checks passed (PyPI ✅ GitHub ✅ Tests ✅)
