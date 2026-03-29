# Changelog

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
