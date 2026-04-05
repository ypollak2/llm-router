# Changelog

## v1.3.4 — Trendiness & Usability (2026-04-05)

### Added

- **`llm-router doctor`** — comprehensive health check command: verifies hooks, routing rules, Claude Code MCP registration, Claude Desktop registration, Ollama reachability (with model list), usage data freshness, API keys, and installed version. Prints colored ✓/✗/⚠ results with copy-paste fix commands.
- **Cursor / Windsurf / Zed install snippets** in README — the router works in any MCP-compatible IDE; Quick Start now includes ready-to-paste config blocks for Cursor (`~/.cursor/mcp.json`), Windsurf (`~/.codeium/windsurf/mcp_config.json`), and Zed (`settings.json`).
- **Colored `install --check` output** — `✓`/`✗`/`⚠` symbols with ANSI colors (respects `NO_COLOR` and non-tty); broken items show a `→ fix command` hint inline.
- **Better first-run install message** — after `llm-router install`, shows a "Try it" prompt to test routing immediately and lists all subcommands including the new `doctor`.

### Changed

- `llm-router status` subcommand list now includes `llm-router doctor`.

## v1.3.3 — Visibility & Usability (2026-04-05)

### Added

- **Visible routing indicator** (`auto-route.py`) — terminal now shows `⚡ llm-router → {tool} [{task_type}/{complexity} · {method}]` each time the hook fires. Users can see routing happen in real time instead of it being silent.
- **Shareable savings line** (`session-end.py`) — session summary now prints `💡 Saved ~$X.XX with llm-router · github.com/ypollak2/llm-router` when external savings exceed $0.001.
- **`llm-router status` command** — new CLI subcommand showing Claude subscription pressure, today's external routing calls/cost/savings, and top models used, all from local state files (no network calls).
- **Smithery listing** (`smithery.yaml`) — one-click install via Smithery MCP marketplace with full `configSchema` and `commandFunction`.
- **PyPI download badge + Smithery badge** in README.
- **Zero-config pitch** in README Quick Start — prominently explains the router works with just a Claude Code subscription, no API keys required.
- **`pipx` one-line install** in README — `pipx install claude-code-llm-router && llm-router install`.

### Changed

- `auto-route.py` hook version bumped to 7; `session-end.py` to 9.

## v1.3.2 — Distribution & Install (2026-04-05)

### Added

- **Claude Desktop auto-install** (`install_hooks.py`) — `llm-router install` now writes the MCP server entry to `claude_desktop_config.json` on macOS, Windows, and Linux. Safe merge — never overwrites unrelated entries. `uninstall` removes it cleanly.
- **API key validation on install** — `llm-router install --check` and post-install output now show which provider API keys are set and warn when no external providers are configured.
- **Automated PyPI publish CI** (`.github/workflows/publish.yml`) — pushes to `v*` tags trigger: test suite → version verification → `uv build` → PyPI publish. Blocks bad releases by running tests first.
- **PyPI discoverability keywords** — added `llm-router`, `claude-desktop`, `cost-optimization`, `model-routing`, `mcp-server` to package metadata.
- **Glama MCP registry listing** (`glama.json`) — full tool/environment/resource metadata for Glama and compatible registries.

### Fixed

- **sdist bloat** — excluded `.claude/`, `.serena/`, `.playwright-mcp/`, screenshots, and dev files from the source distribution (331 KB vs 28 MB previously).
- **aiosqlite teardown warning** — added targeted `filterwarnings` for the benign `call_soon_threadsafe` race in pytest-asyncio function-scoped loops.
- **Hook absolute paths** — `.claude/settings.json` project hooks now use absolute paths, preventing `ENOENT` failures when Claude is opened from a different directory.

### Changed

- `mcp-registry.json` version bumped to `1.3.2`.

## v1.3.0 — Observability (2026-04-04)

### Added

- **Anthropic prompt caching** (`prompt_cache.py`) — auto-injects `cache_control: {"type": "ephemeral"}` breakpoints on long stable context before every Anthropic model call, saving up to 90% on cached token reads. Two breakpoints are placed at the most cache-effective positions: the system message (if ≥1024 tokens) and the last context message before the current user turn. Non-Anthropic models pass through unchanged. Activated by default; controlled by `LLM_ROUTER_PROMPT_CACHE_ENABLED` (bool) and `LLM_ROUTER_PROMPT_CACHE_MIN_TOKENS` (int, default 1024).

- **Hard daily spend cap** (`router.py`) — `LLM_ROUTER_DAILY_SPEND_LIMIT` (float, default 0 = disabled) now raises `BudgetExceededError` before any LLM call when daily spend ≥ limit. Checked inside the existing `_budget_lock` alongside the monthly cap so concurrent callers can't both slip past. Error message includes the reset time (midnight UTC) and the env var to raise the limit.

- **Semantic dedup cache** (`semantic_cache.py`) — embeds prompts via Ollama's `nomic-embed-text` model and skips the LLM call entirely when a recent response (within 24h, same task type) has cosine similarity ≥ 0.95. Returns a zero-cost `LLMResponse` with `provider="cache"`. New `semantic_cache` table added to the usage SQLite DB via `CREATE TABLE IF NOT EXISTS` (existing DBs unaffected). Only active when `OLLAMA_BASE_URL` is set; silently no-op otherwise.

- **Web dashboard** (`dashboard/`) — `llm-router dashboard [--port N]` starts a local `aiohttp` HTTP server at `localhost:7337`. Also accessible via the `llm_dashboard` MCP tool. Shows: today's calls/cost/tokens, monthly spend, lifetime savings vs Opus baseline, model and task-type distribution (7 days), daily cost trend (14 days), recent routing decisions table, and session quota. Auto-refreshes every 30 seconds. Self-contained single-file HTML — no build step. All DB values rendered via `textContent`/Chart.js arrays (no `innerHTML` XSS surface).

### Fixed

- **Cross-platform desktop notifications** (`cost.py`) — `fire_budget_alert` now dispatches to `osascript` (macOS), `notify-send` (Linux), or `win10toast` (Windows, optional). Previously macOS-only; alerts were silently dropped on Linux and Windows.

- **Dashboard background process** (`tools/admin.py`) — `llm_dashboard` uses `start_new_session=True` on macOS/Linux and `DETACHED_PROCESS` on Windows, ensuring the dashboard survives terminal close on all platforms.

## v1.2.0 — Foundation Hardening (2026-04-02)

### Changed

- **`server.py` decomposed into `tools/` modules** — the 2,328-line monolith is now a 110-line thin entrypoint. All 24 MCP tools live in 8 focused modules: `routing.py`, `text.py`, `media.py`, `pipeline.py`, `admin.py`, `subscription.py`, `codex.py`, `setup.py`. Each module exports `register(mcp)`. Backward-compatible: all imports from `llm_router.server` still work.
- **`state.py` module** — shared mutable state (`_last_usage`, `_active_profile`) extracted from `server.py` into a dedicated module with `get_*`/`set_*` accessors, eliminating circular import risk across tool modules.

### Added

- **`llm-router install` subcommand** — the main `llm-router` CLI now accepts `install`, `install --check`, `install --force`, and `uninstall` subcommands. Running `llm-router` without arguments still starts the MCP server (unchanged behavior). The `--check` flag previews what would be installed; `--force` updates paths even if already registered.
- **`mcp-registry.json`** — registry manifest at repo root for `registry.modelcontextprotocol.io` submission, listing all 18 primary tools, 1 resource, and 2 hooks with descriptions.

## v1.1.0 — Subscription-Aware Routing + Observability (2026-04-01)

### Added

- **Subscription-aware MCP tools** (`server.py`) — `llm_query`, `llm_code`, `llm_research`, and `llm_analyze` now return a `⚡ CC-MODE:` hint when Claude Code subscription has headroom, directing Claude to switch model tier (`/model haiku` / `/model opus`) instead of making an external API call. External calls are only made when the relevant pressure threshold is exceeded (session ≥ 85% for simple, sonnet ≥ 95% for moderate, weekly ≥ 95% for complex).
- **Ollama live reachability probe** (`config.py`) — `ollama_reachable()` does a TCP socket check before marking Ollama available. Previously the health endpoint reported "healthy" even when the Ollama server was unreachable. The probe result is cached for 30 seconds to avoid per-call overhead.
- **E2E demo test suite** (`tests/test_demo_routing.py`, `tests/test_demo_session_summary.py`) — Two demo files doubling as executable documentation: 7 tests covering the full routing pipeline (CC hints, pressure cascade, Ollama health probe) and 4 tests covering the session-end hook (subprocess output, savings math, empty session, model name truncation).

### Fixed

- **Session-end hook Stop schema** (`hooks/session-end.py` v5) — Hook was wrapping output in `hookSpecificOutput` which is invalid for Stop events. Output is now `{"systemMessage": "..."}` at the top level, matching the Stop hook schema. Previously caused "JSON validation failed" errors at session end.
- **"improve ... performance" misrouted as query** (`hooks/auto-route.py`) — The code heuristic only matched `optimize` but not `improve`, causing "improve the database query performance" to fall through to Ollama and get classified as `query/simple`. Now matches both.
- **Session-end hook simplification** (`hooks/session-end.py` v4→v5) — Removed per-tool bar chart and verbose labels (60 lines). Summary is now a compact table: calls × model × cost, with total savings % vs Sonnet baseline.

## v1.0.0 — Production Stable: Bug Fixes & Routing Integrity (2026-03-31)

### Fixed (Critical)

- **Subscription flag now enforced** (`config.py`) — `available_providers()` previously had a comment saying anthropic was excluded in subscription mode, but never applied the filter. Now `providers.discard("anthropic")` is called when `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`, even if `ANTHROPIC_API_KEY` is set. Prevents accidental double-billing via API when already inside Claude Code.
- **`llm_route` uses unified pressure-aware profile** (`server.py`) — `llm_route` previously derived its routing profile from complexity alone, while `llm_classify` used `select_model()` (which applies budget pressure). Now both tools share the same decision path: `select_model()` is called in `llm_route` and its downshifted profile is used when pressure ≥ 85%. Eliminates the two-path divergence where the same prompt could route differently depending on which tool was called.

### Fixed (Moderate)

- **Pressure data staleness warnings in 3 hooks** — `auto-route.py`, `subagent-start.py`, and `agent-route.py` now check if `usage.json` is older than 30 minutes. When stale, a visible warning is appended to the routing directive/context. Previously all three hooks made routing decisions on potentially hours-old quota data with no indication to the user.
- **Ollama comment corrected** (`config.py` lines 55–65) — Updated outdated comment that claimed Ollama is "ONLY used for BUDGET tier". Ollama is also injected at pressure ≥ 85% for any profile. Comment now documents both injection scenarios and the OLLAMA_URL vs OLLAMA_BASE_URL separation.
- **`llm_set_profile` no longer mutates frozen config** (`server.py`) — Replaced `object.__setattr__(config, ...)` hack (which bypassed Pydantic's immutability) with a module-level `_active_profile` variable. A `get_active_profile()` helper returns the override or config default. Immutability contract is now preserved end-to-end.

### Fixed (Minor)

- **Atomic count write in `usage-refresh.py`** — `_write_count()` now writes to a `.tmp` file then renames atomically via `os.replace()`. Prevents concurrent PostToolUse hooks from corrupting the routed-call counter via interleaved reads/writes.
- **Health-aware classifier model ordering** (`classifier.py`) — Classifier model candidates are now sorted: healthy providers first, then by static list order. Unhealthy providers (circuit breaker open) are tried last instead of first, reducing classification latency when a provider is down.

### Changed

- `pyproject.toml`: Development status updated from `4 - Beta` to `5 - Production/Stable`.
- `tests/conftest.py`: `mock_env` fixture now explicitly sets `OLLAMA_BASE_URL=""` to disable Ollama in unit tests (mirrors existing Codex disable pattern). Prevents test failures when `OLLAMA_BASE_URL` is set in project `.env`.
- `tests/test_router.py`: `test_no_providers_configured` now clears all API keys explicitly (including shell env keys) for deterministic behavior.

---

## v0.9.2 — Claude Code Subscription Mode (2026-03-31)

### Added

- **Claude Code subscription mode** (`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`) — All Claude tiers (Haiku, Sonnet, Opus) are now accessed via the Claude Code subscription at zero API cost. No Anthropic API key required or used.
- **Tiered pressure cascade** — Three independent quota buckets each control a different complexity tier, cascading downward when pressure rises:
  - `session ≥ 85%` → simple tasks switch to external (Gemini Flash / Groq)
  - `sonnet ≥ 95%` → moderate tasks switch to external (GPT-4o / DeepSeek)
  - `weekly ≥ 95%` OR `session ≥ 95%` → all tiers go external (global emergency)
- **Per-bucket pressure tracking** — `auto-route.py` and `agent-route.py` now read `{session_pct, sonnet_pct, weekly_pct}` from `usage.json` instead of the single `highest_pressure` field. More granular and accurate fallback decisions.
- **Haiku for simple tasks** — Simple tasks emit a `/model claude-haiku-4-5-20251001` hint instead of routing to cheap external APIs. Haiku is free via subscription and avoids network latency.
- **Passthrough for moderate tasks** — Moderate tasks with no pressure are a no-op (Sonnet handles directly). No model switching, no external call.
- **Opus for complex tasks** — Complex tasks emit a `/model claude-opus-4-6` hint. Best quality, zero API cost while subscription quota is available.
- **Haiku as preferred complexity classifier** — `CLASSIFIER_MODELS` now lists `anthropic/claude-haiku-4-5-20251001` first. Skipped automatically when no `ANTHROPIC_API_KEY` is set; Gemini Flash / Groq serve as instant fallbacks.
- **Configurable `media_request_timeout`** — New `LLM_ROUTER_MEDIA_REQUEST_TIMEOUT` env var (default: 600s). Video generation can take several minutes; previously the 120s `request_timeout` caused false failures.
- **`asyncio.Lock` for budget enforcement** — Budget check in `router.py` is now wrapped in `_budget_lock`. Prevents concurrent requests both passing the monthly budget cap check (race condition when two tasks fire simultaneously near the limit).
- **90% budget soft-warning** — Logs a `WARNING` at 90% monthly spend, before the hard stop at 100%.

### Changed

- `auto-route.py` → version 5: pressure default changed from `0.3` (conservative) to `0.0` (no pressure assumed when `usage.json` absent). Subscription models are preferred when blind — no unnecessary external routing.
- `agent-route.py` → version 2: `_complexity_to_profile()` now takes `(complexity, session, sonnet, weekly)` instead of a single pressure float. Block message displays all three pressure values.
- `session-start.py` → version 3: BANNER updated to reflect subscription-first strategy; adds a `usage.json` freshness check (warns if missing or >1 hour old).

### Fixed

- **Silent exception handlers in `profiles.py`** — Three `except: pass` blocks now log `WARNING` messages instead of swallowing errors silently. Pressure reordering failures are now visible.

---

## v0.9.1 — Robustness & Error Hints (2026-03-31)

### Added

- **Codex path validation** — `llm_codex` now validates `CODEX_PATH` before attempting to run. Returns a clear error with platform-specific instructions (`which codex` / `where codex`) when the binary is missing.
- **Pressure fallback validation** — `reorder_for_pressure()` now logs a warning when the fallback chain is shorter than expected, so silent routing degradation surfaces in logs.

### Changed

- Version bump to 0.9.1.

---

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
