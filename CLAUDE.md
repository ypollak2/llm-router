# LLM Router — Project Instructions

## ⚠️ CRITICAL: Hook Deadlock Prevention

**NEVER configure enforce-route or any hook to block Claude's core tools.**
The following tools must ALWAYS be allowed, unconditionally:
`Read, Edit, MultiEdit, Write, Bash, Grep, Glob, LS, ToolSearch, Agent`

Blocking these creates an unresolvable deadlock — Claude cannot fix the hook
because the hook blocks the tools needed to fix it. This killed 6+ sessions.

- Use a **blocklist** approach: block specific routing violations only
- Never use an **allowlist** that omits core Claude tools
- If you suspect a deadlock: `export LLM_ROUTER_ENFORCE=off` then fix the hook

## Project Overview

This project is **Python**. All new code must be Python unless explicitly asked otherwise.
Use type hints on all public functions, Pydantic for external data, `@dataclass` for domain objects.

## Environment Setup

Before any implementation session, verify:
```bash
echo $OPENAI_API_KEY      # must be set and valid
echo $GEMINI_API_KEY      # must be set and valid
echo $ANTHROPIC_API_KEY   # must be set and valid
ollama list               # Ollama must be running
echo $LLM_ROUTER_ENFORCE  # check enforcement mode (off/soft/smart/hard)
```
If any key is missing or invalid, fix it before writing code.

## Testing

Always run tests with:
```bash
uv run pytest tests/ -q --ignore=tests/test_integration.py
```
Never use bare `pytest` — it will fail without the venv context `uv run` provides.
For a single test file: `uv run pytest tests/test_classifier.py -x -q`

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

## MANDATORY Release Checklist

**Every user-facing change MUST complete ALL steps before moving to the next feature.**
This checklist is not optional. Missing any step means the release is incomplete.

### Step 1 — Tests (always)
```bash
uv run pytest tests/ -q --ignore=tests/test_integration.py
uv run ruff check src/ tests/
```

### Step 2 — Hook deploy (after any hook change)
```bash
install -m 755 src/llm_router/hooks/auto-route.py ~/.claude/hooks/llm-router-auto-route.py
install -m 755 src/llm_router/hooks/session-end.py ~/.claude/hooks/llm-router-session-end.py
install -m 755 src/llm_router/hooks/session-start.py ~/.claude/hooks/llm-router-session-start.py
install -m 755 src/llm_router/hooks/enforce-route.py ~/.claude/hooks/llm-router-enforce-route.py
```

### Step 3 — Version bump (every user-facing change)
Bump `pyproject.toml` AND `.claude-plugin/plugin.json` to the same version:
```bash
# Edit both files, then verify:
python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
python3 -c "import json; print(json.load(open('.claude-plugin/plugin.json'))['version'])"
```

### Step 4 — CHANGELOG.md (every version bump)
Add entry at the top with: version, date, feature summary, technical notes.

### Step 5 — README.md (when new features/commands are added)
Update the feature list, command reference, and any version badges.

### Step 6 — Commit + push
```bash
git add -p   # stage deliberately, never `git add .`
git commit -m "feat(vX.Y.Z): ..."
git push
```

### Step 7 — PyPI publish (every version bump)
```bash
rm -rf dist/ && uv build
PYPI_TOKEN=$(python3 -c "import configparser; c=configparser.ConfigParser(); c.read('/Users/yali.pollak/.pypirc'); print(c['pypi']['password'])")
uv publish --token "$PYPI_TOKEN"
```

### Step 8 — Git tag + GitHub Release (every version bump)
```bash
# Tag
git tag v$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
git push origin --tags

# GitHub Release (use --latest for the newest version)
gh release create vX.Y.Z \
  --title "vX.Y.Z — <headline>" \
  --latest \
  --notes "$(cat <<'NOTES'
## What's new
- bullet points from CHANGELOG

## Upgrade
\`\`\`bash
pip install --upgrade claude-code-llm-router && llm-router install
\`\`\`
NOTES
)"
```

### Step 9 — Bump marketplace.json (REQUIRED — controls plugin install version)
```bash
# .claude-plugin/marketplace.json "version" is what `claude plugin install llm-router`
# uses to determine which git tag to clone. Without this, other machines stay on old versions.
# Update the version field to match pyproject.toml:
# "version": "X.Y.Z"
```

### Step 10 — Plugin reinstall (every version bump)
```bash
# Reinstall the CC plugin from the updated repo so the installed version matches
claude plugin reinstall llm-router
# Verify installed version matches pyproject.toml
claude plugin list | grep llm-router
```

---

**Why this matters**: skipping CHANGELOG/version/PyPI/plugin leaves users on stale builds,
breaks `pip install --upgrade`, and creates drift between installed plugin and live code.

---

## Push to Production Rules (legacy — superseded by checklist above)

Additional rules that apply on every push:

1. **Bump server tool count in test_server.py** when adding new MCP tools
2. **Push immediately after commit** — never let local main diverge from remote
3. **One concern per commit** — routing changes, tool additions, and hook fixes each get their own commit
