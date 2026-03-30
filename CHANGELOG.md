# Changelog

## v0.5.2 — Ollama Local Models & Claude Mobile App Support (2026-03-30)

### Added

- **Ollama routing** — Local models are now first-class routing targets. Set `OLLAMA_BASE_URL=http://localhost:11434` and `OLLAMA_BUDGET_MODELS=llama3.2,qwen2.5-coder:7b` to route tasks to free local models before falling back to cloud providers. Supports per-tier model lists (`OLLAMA_BUDGET_MODELS`, `OLLAMA_BALANCED_MODELS`, `OLLAMA_PREMIUM_MODELS`).
- **Claude mobile app access** — `llm-router-sse` CLI entry point starts the MCP server with SSE transport (port 17891) for remote connection from Claude mobile app via cloudflared tunnel.
- **`SessionStart` hook** — New `mobile-access.sh` hook auto-starts the SSE server + cloudflared tunnel each session and prints the mobile connection URL.
- **Ollama llama emoji** — 🦙 icon for Ollama provider in CLI and MCP output.

### Changed

- README savings section rewritten with concrete per-task cost examples (factual queries: $0.000001, with Ollama: $0).
- Ollama added to providers table in README and full setup guide added to `docs/PROVIDERS.md`.
- `OLLAMA_API_BASE` exported to env automatically when `OLLAMA_BASE_URL` is set.

---

## v0.5.1 — Claude Haiku Budget Routing (2026-03-30)

### Added

- **Claude Haiku in BUDGET tier** — `anthropic/claude-haiku-4-5-20251001` added to all budget-tier text chains. Haiku is now the primary code model in budget routing (best code quality at budget price point) and a fallback in query/research/generate/analyze chains.

---

## v0.5.0 — Session Context Injection & Auto-Update Rules (2026-03-30)

### Added

- **Session context injection** — All text routing tools (`llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`, `llm_route`) now accept an optional `context` parameter. The router automatically prepends recent session messages and cross-session summaries so external models (GPT-4o, Gemini, Perplexity) receive conversation history they would otherwise miss.
- **Two-layer context system** — Ephemeral in-process ring buffer (last N messages, current session) + persistent SQLite session summaries (previous sessions). Previous sessions appear first, current session at the end.
- **`llm_save_session` MCP tool** — Summarizes the current session via a cheap model (budget profile) and persists it to SQLite for future context injection. Works as a session boundary marker.
- **Auto-summarization** — `auto_summarize_session()` routes the buffer through the cheapest available model, falling back to concatenation if LLM is unavailable.
- **Context compaction** — Injected context runs through the existing structural compaction pipeline to stay within a configurable token budget (default 1500 tokens).
- **4 new config settings** — `LLM_ROUTER_CONTEXT_ENABLED`, `LLM_ROUTER_CONTEXT_MAX_MESSAGES`, `LLM_ROUTER_CONTEXT_MAX_PREVIOUS_SESSIONS`, `LLM_ROUTER_CONTEXT_MAX_TOKENS`.
- **Auto-update routing rules** — `check_and_update_rules()` in `install_hooks.py` compares a version header embedded in the bundled rules file against the installed copy. Called automatically at MCP server startup so users get rule updates after `pip upgrade` without re-running `llm-router install`.
- **Rules versioning** — `<!-- llm-router-rules-version: N -->` header in `llm-router.md`. Increment N to push rule changes to all users silently on next startup.

### Fixed

- **Routing hint override bug** — `CLAUDE.md` previously listed "file edits, git operations, shell commands" as exceptions to routing. Removed: these tasks still consume Opus tokens and benefit most from cheap-model offloading. Token savings are the primary routing value, not just web access.
- **Global rules enforcement** — Updated `~/.claude/rules/llm-router.md` with an explicit "Why Routing Saves Tokens Even for Simple Tasks" section explaining the token arbitrage model (Opus orchestrates, cheap model reasons).

### Changed

- Tool count increased from 24 to 25 (`llm_save_session` added).
- `route_and_call()` signature gains `caller_context: str | None = None` parameter.
- Session buffer and summaries use SHA-256 key hashing consistent with classification cache.

---

## v0.4.0 — Quality & Global Enforcement (2026-03-29)

### Added

- **Structural context compaction** — 5 strategies (collapse whitespace, strip code comments, dedup sections, truncate long code, collapse stack traces) applied before sending prompts to external LLMs. Reduces token usage 10-40% on verbose prompts.
- **Quality logging** — `routing_decisions` SQLite table captures full routing lifecycle: classification input, model selection reasoning, and execution outcome. 21 columns per decision.
- **`llm_quality_report` MCP tool** — ASCII analytics dashboard: classifier breakdown, task type distribution, model usage, downshift rate, confidence averages, cost/latency aggregates.
- **Savings persistence** — JSONL file written by PostToolUse hook, imported into `savings_stats` SQLite table. `get_lifetime_savings_summary()` provides per-session and aggregate analytics.
- **Gemini Imagen 3** — Direct REST API integration for image generation via `generativelanguage.googleapis.com`. Supports aspect ratio mapping and both `imagen-3` and `imagen-3-fast` models.
- **Gemini Veo 2** — Video generation via `predictLongRunning` endpoint with async polling. ~$0.35/sec of generated video.
- **Global hook installer** — `llm_setup(action='install_hooks')` MCP tool + `llm-router-install-hooks` CLI. Copies hooks to `~/.claude/hooks/` and registers in `~/.claude/settings.json` so every Claude Code session auto-routes.
- **Global routing rules** — `~/.claude/rules/llm-router.md` installed by hooks installer. Enforces that Claude always follows `[ROUTE:]` hints regardless of task type.
- **`llm_setup(action='uninstall_hooks')`** — Clean removal of global hooks and rules.

### Fixed

- **Mock config compaction crash** — `router.py` compaction code now guards against `MagicMock` config attributes in tests.
- **`import_savings_log` data loss** — JSONL file now truncated only after successful DB commit, preventing data loss on write failures.
- **CI test failures** — Hook classification tests updated to accept valid alternate classifications when Ollama is unavailable.

### Changed

- Tool count increased from 23 to 24 (`llm_quality_report` added).
- `install.sh` now also installs routing rules to `~/.claude/rules/`.
- Hook scripts bundled as package data for reliable installation.

---

## v0.3.0 — Caching & Automation (2026-03-29)

### Added

- **Prompt classification cache** — SHA-256 exact-match with in-memory LRU (1000 entries, 1h TTL). Caches `ClassificationResult` so budget pressure is always fresh. Zero overhead on misses.
- **`llm_cache_stats` MCP tool** — view hit rate, entries, memory estimate, evictions.
- **`llm_cache_clear` MCP tool** — clear the classification cache.
- **Auto-route hook** — `UserPromptSubmit` hook with fast heuristic classifier (~0ms). Injects `[ROUTE: task_type/complexity]` hints so Claude automatically picks the right `llm_*` tool without the user typing `/route`.
- **CLAUDE.md auto-routing rule** — project-level instruction that tells Claude to route external LLM tasks automatically.
- **Rate limit detection** — catches 429/rate_limit errors in `router.py` with shorter 15s cooldown (vs 60s for hard failures). `health.py` gains `record_rate_limit()`, `rate_limited` flag, and `rate_limit_count`.
- **`llm_setup(action='test')`** — validates API keys with minimal LLM calls (~$0.0001 each). Tests a specific provider or all configured providers.
- **`llm_stream` MCP tool** — stream LLM responses via `call_llm_stream()` async generator. Shows output as it arrives for long-running tasks. Yields content chunks + `[META]` JSON with cost/latency.
- **Usage-refresh hook** — `PostToolUse` hook that detects stale Claude subscription data (>15 min) after any `llm_*` tool call and nudges Claude to refresh via `/usage-pulse`.
- **Usage pulse wiring** — `llm_update_usage` now writes a refresh timestamp to `~/.llm-router/usage_last_refresh.txt` for the hook. Full cycle: usage-pulse skill → Playwright fetch → `llm_update_usage` → timestamp → hook stays quiet until stale.
- **Published to PyPI** as `claude-code-llm-router` — `pip install claude-code-llm-router`.

---

## v0.2.0 — Intelligence Layer (2026-03-29)

### Added

- **Complexity-first routing** — simple tasks → haiku, moderate → sonnet, complex → opus. Budget downshifting is now a late safety net at 85%+, not the primary mechanism.
- **Live Claude subscription monitoring** — fetches session/weekly usage from claude.ai internal JSON API via Playwright `browser_evaluate(fetch(...))`.
- **Time-aware budget pressure** — `effective_pressure` reduces downshift urgency when session reset is imminent (< 30 min away).
- **Codex desktop integration** — routes tasks to local Codex CLI (`/Applications/Codex.app`), free via OpenAI subscription.
- **Unified usage dashboard** (`llm_usage`) — single view of Claude subscription %, Codex status, API spend per provider, and routing savings.
- **`llm_setup` tool** — discover existing API keys on the laptop, add new providers with validation, view setup guides. All operations local-only with key masking and `.gitignore` checks.
- **Per-provider budget limits** — `LLM_ROUTER_BUDGET_OPENAI`, `LLM_ROUTER_BUDGET_GEMINI`, etc.
- **Gemini media routing** — Imagen 3 (images) and Veo 2 (video) added to all routing profiles.
- **`llm_classify` tool** — classify task complexity and see the routing recommendation with model/cost details.
- **`llm_check_usage` / `llm_update_usage`** — fetch and store Claude subscription data for routing decisions.
- **`llm_track_usage`** — record usage for a specific provider.
- **External fallback ranking** — when Claude quota is tight, rank available external models by quality (descending) and cost (ascending).

### Changed

- Dashboard output switched to **ASCII box-drawing** (`+`, `-`, `|`, `=`, `.`) for reliable rendering in Claude Code's MCP output.
- Pressure thresholds updated from 50%/80% to **85%/95%** safety net — complexity routing handles the rest.
- Classification headers use text tags (`[S]`, `[M]`, `[C]`) instead of emoji.
- Budget bars use ASCII (`[====........]`) instead of Unicode blocks.
- Tool count increased from 17 to 20.

### Fixed

- MCP tool output rendering issues (Unicode blocks, markdown, emoji all garbled in collapsed JSON view).
- f-string backslash errors in dashboard formatting code.

## v0.1.0 — Foundation (2026-03-15)

### Added

- Text LLM routing via LiteLLM (10 providers)
- Three routing profiles: budget, balanced, premium
- Cost tracking with SQLite
- Health checks with circuit breaker pattern
- Image generation (DALL-E, Flux, Stable Diffusion)
- Video generation (Runway, Kling, minimax)
- Audio/voice routing (ElevenLabs, OpenAI TTS)
- Monthly budget enforcement with hard limits
- Multi-step orchestration with pipeline templates
- Claude Code plugin with `/route` skill
- Freemium tier gating (free / pro)
- CI with GitHub Actions
