# Changelog

## v0.9.0 — Operational Reliability (2026-03-31)

### Fixed

- **Global MCP server registration** — `llm-router-install-hooks` now registers the MCP server in `~/.claude/settings.json` so `llm_*` tools are available in all Claude Code sessions, not just the llm-router project directory. Previously, hooks fired everywhere but the routing tools were unreachable in other projects.
- **Session ID collisions** — `usage-refresh.py` used `os.getppid()` for session IDs; PIDs are recycled across reboots, corrupting per-session stats. Now writes a UUID to `~/.llm-router/session_id.txt` at session start and reads that instead.
- **Stale circuit breakers** — Provider health state persisted indefinitely; a Groq failure from yesterday could block it all day. `HealthTracker.reset_stale(max_age_seconds=1800)` now clears failures older than 30 min on every MCP server startup.
- **RESEARCH silent degradation** — `llm_research` previously fell through to a non-web-grounded model when `PERPLEXITY_API_KEY` was not set, returning plausible but potentially stale answers. Now returns a clear error with setup instructions immediately.
- **Health threshold too lenient** — `health_failure_threshold` was 3 (circuit breaker only fired after 3 consecutive failures); tightened to 2 for faster provider removal from chains.

### Changed

- **Config defaults tightened**:
  - `llm_router_monthly_budget`: `0.0` (unlimited) → `20.0` ($20/month cap)
  - `daily_token_budget`: `0` (unlimited) → `500_000` (500k tokens/day)
  - `health_failure_threshold`: `3` → `2`
  - `health_cooldown_seconds`: `60` → `30`
- `install_hooks.py` gains `uninstall()` MCP server removal to match install.

### Added

- **`HealthTracker.reset_stale(max_age_seconds)`** — Resets both `consecutive_failures` and `rate_limited` for any provider whose last failure event is older than the age limit. Returns list of reset provider names for logging.
- **Session UUID** — `session-start.py` v2 writes `~/.llm-router/session_id.txt` containing a fresh UUID on every session start, plus drops a `reset_stale.flag` for the server to act on startup.
- **`get_routing_savings_vs_sonnet(days=0)`** in `cost.py` — Queries `routing_decisions` for real token counts and actual cost, computes savings as `(input_tokens × $3/M + output_tokens × $15/M) − actual_cost_usd`. Per-model breakdown included.
- **`llm_usage` lifetime savings** now uses real `routing_decisions` data (above function) instead of the legacy JSONL-estimated `savings_stats` table. Shows actual cost, Sonnet 4.6 baseline, and savings per model.

---

## v0.8.1 — Agent Routing & Real Savings Dashboard (2026-03-31)

### Added

- **PreToolUse[Agent] hook** (`agent-route.py`) — Intercepts subagent spawning before it happens. Approves pure-retrieval tasks (file reads, symbol searches, `Explore` subagent type). Blocks reasoning tasks with a redirect instruction containing the exact `llm_*` MCP tool call to use instead. Prevents the main cost leak: every subagent ran Opus for reasoning; hook routes to Haiku/Sonnet/Opus based on complexity + quota pressure.
- **Pressure-aware profile selection in agent hook**: `< 85%` quota → simple=budget (Haiku), moderate=balanced (Sonnet), complex=premium (Opus). `≥ 85%` → balanced for all. `≥ 99%` → budget/external only.
- **Session-end hook v2** — Reads `routing_decisions` SQLite table directly (replaces JSONL scanning). Shows actual model used per tool, real `cost_usd` from provider API responses, and savings vs Sonnet 4.6 baseline. Per-tool breakdown with ASCII bar charts.
- **`usage.json` export** — `llm_update_usage` in `server.py` now writes `~/.llm-router/usage.json` containing `{session_pct, weekly_pct, highest_pressure, updated_at}`. Enables hook scripts to read real Claude quota pressure without importing Python packages.

### Fixed

- **Agent hook pressure detection** — `agent-route.py` reads `highest_pressure` (0.0–1.0) from `usage.json` directly; previously tried to divide percentage fields causing wrong values.
- **Complexity classifier threshold** — "Analyze the routing logic in profiles.py..." (91 chars) was misclassified as `simple`. Fixed: `simple` now requires BOTH an explicit simple signal AND `len < 80`; otherwise defaults to `moderate`.

---

## v0.8.0 — Routing Correctness Fixes (2026-03-31)

### Fixed

- **Async feedback loop (critical)** — `get_model_failure_penalty()` and `get_model_latency_penalty()` always returned `0.0` in async contexts (every real production call), because they detected a running event loop and skipped the DB fetch to avoid deadlock. Fix: `route_and_call()` now pre-fetches `failure_rates` and `latency_stats` in parallel via `asyncio.gather()` before calling `get_model_chain()`, then passes them as optional dict parameters down through `apply_benchmark_ordering()` → penalty functions. The self-learning feedback system now works correctly in production.
- **BUDGET hard cap never fired** — `reorder_for_pressure()` had an early return for BUDGET profile (`if profile == BUDGET: return chain`), so the ≥ 99% Claude removal logic was never reached for BUDGET routing. Removed the early return — BUDGET chains now correctly strip Claude models at ≥ 99% pressure like BALANCED and PREMIUM.
- **RESEARCH chains ignored pressure entirely** — All RESEARCH tasks returned the static chain unchanged (skipping both benchmark and pressure reordering), so at 85%+ quota Claude Sonnet remained at position 2 in RESEARCH chains. Fix: RESEARCH chains now apply pressure reordering to the non-Perplexity tail only, keeping Perplexity first (web-grounded) while still demoting Claude and promoting cheap models when quota is tight.
- **RESEARCH fallback produces no web-grounded answer** — When Perplexity is unavailable, subsequent models (Gemini, Claude) produce plausible but stale answers with no source citations. Added explicit `log.warning()` and MCP notification when a RESEARCH task falls back to a non-web-grounded model.

### Changed

- `get_model_failure_penalty()` gains optional `failure_rates: dict[str, float] | None` parameter. When provided, uses it directly (no DB, no async conflict). Backward-compatible.
- `get_model_latency_penalty()` gains optional `latency_stats: dict[str, dict] | None` parameter. Same pattern.
- `apply_benchmark_ordering()` gains `failure_rates` and `latency_stats` optional parameters, passes them into penalty functions.
- `get_model_chain()` gains `failure_rates` and `latency_stats` optional parameters, passes them into `apply_benchmark_ordering()`. Also now fetches `get_claude_pressure()` internally (was only in router.py previously).
- `route_and_call()` now `await`s both `get_model_failure_rates()` and `get_model_latency_stats()` in parallel before building the model chain.

### Added

- **6 new tests** in `tests/test_profiles.py`:
  - `TestResearchPressureTail` — 3 tests verifying Perplexity stays first, Claude is demoted at ≥ 85%, Claude leads at < 85%.
  - `TestBudgetHardCap` — 1 test verifying BUDGET chains drop Claude at ≥ 99% pressure.
  - `TestPrefetchedPenalties` — 2 tests verifying `apply_benchmark_ordering()` uses pre-fetched dicts without DB access.

---

## v0.7.1 — Demo Reports & Docs (2026-03-31)

### Added

- **Demo report artifacts** — `demo/demo_report.{md,json}` and `demo/saas_demo_report.{md,json}` committed to repo as reference outputs from live routing sessions (app builder and PulseDB SaaS demos).

### Changed

- README: added `llm_edit` to MCP tools table, moved v0.7 items to Completed section, renamed Next Up to v0.8.

---

## v0.7.0 — Availability-Aware Routing & llm_edit Tool (2026-03-31)

### Added

- **Availability-aware routing** — Latency penalties are now folded into the benchmark-driven quality score. `get_model_latency_stats()` in `cost.py` queries the `routing_decisions` table for P50/P95 latency over a 7-day window. `get_model_latency_penalty()` in `benchmarks.py` maps P95 thresholds to a 0.0–0.50 penalty (<5s=0, <15s=0.03, <60s=0.10, <180s=0.30, ≥180s=0.50). `adjusted_score()` now multiplies failure penalty AND latency penalty into the base quality score.
- **Cold-start defaults** — `_COLD_START_LATENCY_MS` in `benchmarks.py` provides pessimistic P95 defaults for Codex models before any routing history exists (`codex/gpt-5.4` = 60s → 0.30 penalty, `codex/o3` = 90s → 0.30 penalty). Prevents Codex from being placed first in chains on a fresh install.
- **Latency cache** — `_latency_cache` is refreshed at most every 60 seconds per process to avoid repeated SQLite hits when many models are evaluated in a single routing cycle.
- **`llm_edit` MCP tool** — New tool that routes code-edit *reasoning* to a cheap model and returns exact `{file, old_string, new_string}` JSON instructions for Claude to apply mechanically via the Edit tool. Accepts a task description + list of file paths. Files are read locally (capped at 32 KB each), sent to the cheap code model, and parsed into `EditInstruction` dataclasses. Claude's role is execution-only — the expensive reasoning step is offloaded.
- **`src/llm_router/edit.py`** — New module with `read_file_for_edit()`, `build_edit_prompt()`, `parse_edit_response()`, `format_edit_result()` and `EditInstruction` dataclass (`frozen=True`).
- **Test coverage** — `tests/test_availability_routing.py` (12 tests covering latency stats, penalty thresholds, cold-start defaults, integration ordering) and `tests/test_edit.py` (12 tests covering file reading, prompt building, response parsing, formatting).

### Changed

- `adjusted_score()` in `apply_benchmark_ordering()` now applies both failure and latency penalties multiplicatively: `base * (1 - failure_pen) * (1 - latency_pen)`.
- Tool count increased from 25 to 26 (`llm_edit` added).

---

## v0.6.0 — Subscription Mode & Quality-Cost Routing (2026-03-30)

### Added

- **Claude Code subscription mode** (`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`) — When enabled, `anthropic/*` models are excluded from all routing chains. Claude Code users already have Claude via their subscription; routing back to Anthropic via API would require a separate API key and double-bill. In this mode the router routes every task to non-Claude alternatives (DeepSeek, Gemini, GPT-4o, Perplexity, Codex) to preserve Claude quota.
- **Quality-cost tier sorting** in `benchmarks.py` — Models are grouped into 5% quality bands relative to the tier leader. Within each band, cheaper models sort first. Example for BALANCED/CODE: `DeepSeek(1.0) → GPT-4o($0.006) → Sonnet($0.009)` — GPT-4o and Sonnet are within 5% quality of each other, so GPT-4o leads because it's cheaper.
- **Cost pricing table** (`_MODEL_COST_PER_1K`) — blended per-1K-token costs for all 20+ routed models, used by the quality-cost sort.
- **DeepSeek added to BALANCED and PREMIUM chains** — Previously missing from BALANCED/QUERY, BALANCED/GENERATE, PREMIUM/QUERY, PREMIUM/CODE, PREMIUM/GENERATE. At >85% pressure (subscription mode), DeepSeek Chat/Reasoner now correctly leads these chains instead of GPT-4o or o3.
- **Demo scripts** — `demo/app_builder_demo.py` (6-task todo app) and `demo/saas_builder_demo.py` (12-task analytics SaaS) exercise all 3 routing tiers, generate a Markdown + JSON report.

### Fixed

- **Codex injection at front (subscription mode)** — When `llm_router_claude_subscription=True` all `anthropic/*` models are filtered, so `last_claude = max(..., default=-1)` resolved to `insert_at=0`, putting Codex first in every BALANCED/PREMIUM chain. This caused 300s timeouts before fallback. Fix: when no Claude is in the chain, Codex is appended at the **end** as a free fallback — quality-ordered models go first.
- **Codex injected into RESEARCH chains** — Codex has no web access, so injecting it into RESEARCH chains silently replaced Perplexity (when unavailable). Fix: Codex is only injected for `CODE`, `ANALYZE`, and `GENERATE` tasks; never for `RESEARCH` or `QUERY`.

### Changed

- `deepseek/deepseek-reasoner` added to `_CHEAP_MODELS` — at $0.0014/1K it belongs in the cheap tier (priority 1) for pressure reordering, not the paid tier (priority 2). This ensures it leads at >85% pressure instead of being buried behind GPT-4o and o3.
- `_CHEAP_MODELS` threshold comment updated: "< $0.002/1K tokens" (was "< $0.001/1K").
- BALANCED/ANALYZE at >85% pressure: `deepseek-reasoner → gpt-4o → gemini-pro` (was `gpt-4o → gemini-pro → deepseek-reasoner`).
- PREMIUM/CODE at >85% pressure: `deepseek-reasoner → gpt-4o → o3` (was `o3 → gpt-4o`).
- PREMIUM/ANALYZE at >85% pressure: `deepseek-reasoner → gemini-pro → o3` (was `o3 → deepseek-reasoner → gemini-pro`).

---

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
