# LLM Router — Project Instructions

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

## Model Routing Strategy (v1.2.0)

Everyone runs in **Claude Code subscription mode** (`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`).
Anthropic models are **never** called via API — all Claude tiers are used via subscription.

### No Pressure (default)

| Complexity | Action | Model |
|---|---|---|
| `simple` | `/model claude-haiku-4-5-20251001` hint | Haiku (subscription) |
| `moderate` | Passthrough — no switch | Sonnet (current, subscription) |
| `complex` | `/model claude-opus-4-6` hint | Opus (subscription) |
| `research` | `llm_research` | Perplexity (web-grounded) |

### Pressure Cascade (each tier forces all lower tiers external too)

| Condition | simple | moderate | complex |
|---|---|---|---|
| session < 95%, sonnet < 95% | Haiku (sub) | Sonnet (sub) | Opus (sub) |
| sonnet ≥ 95% | EXTERNAL | EXTERNAL | Opus (sub) |
| weekly ≥ 95% **or** session ≥ 95% | EXTERNAL | EXTERNAL | EXTERNAL |

### External Fallback Chains (no Anthropic API, free-first ordering)

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

## Push to Production Rules

Push to `main` after every logical change that passes tests. Rules:

1. **Always run tests before pushing** — `uv run pytest tests/ -q --ignore=tests/test_integration.py`
2. **Deploy hooks after any hook change** — `install -m 755 src/llm_router/hooks/<hook>.py ~/.claude/hooks/<hook>.py`
3. **Bump server tool count in test_server.py** when adding new MCP tools
4. **Version bump in pyproject.toml** for user-facing changes (new tools, routing strategy changes)
5. **Update CHANGELOG.md** for any version bump
6. **Push immediately after commit** — never let local main diverge from remote
7. **One concern per commit** — routing changes, tool additions, and hook fixes each get their own commit
8. **Publish to PyPI after every version bump** — run these three commands in order:
   ```bash
   rm -rf dist/ && uv build
   PYPI_TOKEN=$(python3 -c "import configparser; c=configparser.ConfigParser(); c.read('/Users/yali.pollak/.pypirc'); print(c['pypi']['password'])")
   uv publish --token "$PYPI_TOKEN"
   git tag v$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])") && git push origin --tags
   ```
