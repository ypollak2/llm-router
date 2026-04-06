# Changelog

## v1.8.3 — Critical: fix routing format drift + MCP CLI registration (2026-04-06)

### Fixed

- **Hook format drift (routing silently ignored)** — `auto-route.py` was emitting `⚡ ROUTE→tool(...)` but the bundled rules file told Claude to look for `⚡ MANDATORY ROUTE:`. The mismatch caused Claude to treat every routing directive as informational text and ignore it, defaulting to Opus for all tasks. All tokens, zero routing. Fixed: hook now emits the canonical `⚡ MANDATORY ROUTE: {task_type}/{complexity} → call {tool}({args})` format.
- **MCP server not visible to `claude -p`** — `llm-router install` was writing `mcpServers` to `~/.claude/settings.json` (Claude Desktop location) but not to `~/.claude.json` (Claude Code CLI location). After a fresh install, running `claude mcp list` showed no llm-router entry, and `claude -p` couldn't call any `llm_*` tools. Fixed: `install()` now also registers in `~/.claude.json` via `claude mcp add --scope user` (with a direct JSON merge fallback for headless/Docker environments without the `claude` CLI).
- **Rules file updated** — `llm-router.md` bumped to version 4 with accurate format examples showing the `(complexity="...")` argument syntax.

### Hook version

- `auto-route.py`: v9 → v10 (format fix)

## v1.8.2 — Docker/agent headless mode (2026-04-06)

### Added

- **`llm-router install --headless`** — installs for Docker/CI/agent environments (API-key mode). Prints a complete Dockerfile snippet + settings.json merge example for wiring llm-router into a Claude Code agent container.
- **Dynamic session-start banner** — `session-start.py` now auto-detects the routing mode from `LLM_ROUTER_CLAUDE_SUBSCRIPTION`. When not set (API-key / Docker mode), shows "API-key routing in effect" banner with free-first chain instead of "subscription routing" + wrong pressure cascade info.
- **Skip OAuth on Linux/Docker** — `session-start.py` skips the `_refresh_claude_usage()` OAuth call when `LLM_ROUTER_CLAUDE_SUBSCRIPTION` is not set, eliminating noisy "Keychain not found" warnings in agent containers.

### How to use in Docker/K8s agents

```dockerfile
RUN pip install claude-code-llm-router && llm-router install --headless
# Pass API keys at runtime via K8s secret (do NOT set LLM_ROUTER_CLAUDE_SUBSCRIPTION):
# GEMINI_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, DEEPSEEK_API_KEY
```

## v1.8.1 — Fix: inline OAuth refresh prevents session exhaustion (2026-04-06)

### Fixed

- **Critical routing bug: session hitting 100%** — `auto-route.py` was reading stale `usage.json` and making routing decisions based on hours-old pressure data. When `usage.json` was >30 min old AND last known session pressure was ≥70%, the hook never triggered the pressure cascade to external providers, letting the Claude subscription session exhaust to 100%.

  **Root cause**: Subscription usage is monotonically increasing within a 5h window. Stale data always underestimates pressure. At 70%+ pressure, this underestimate caused catastrophic under-routing.

  **Fix**: `auto-route.py` now attempts an **inline OAuth refresh** (macOS Keychain + Anthropic API) when `usage.json` is stale AND last known session ≥ 70%. Refresh is rate-limited to once per 2 minutes to avoid API hammering. After the refresh, routing decisions use fresh pressure data — ensuring the cascade to external providers fires at the correct 95% threshold.

## v1.8.0 — claw-code hook install (2026-04-06)

### Added

- **`llm-router install --claw-code`** — installs hooks and MCP server into `~/.claw-code/settings.json`; auto-detects claw-code if present during regular `llm-router install`
- **claw-code-adapted hooks** — `session-end-clawcode.py` and `status-bar-clawcode.py` omit the Claude Code subscription pressure sections (claw-code has no Anthropic OAuth; every call is a paid API call)
- **`llm-router doctor`** — new section checks claw-code hook status and MCP registration when `~/.claw-code/` is detected
- **`install_claw_code()` / `uninstall_claw_code()`** in `install_hooks.py` — programmatic API for claw-code integration; detects `~/.claw-code/` with XDG fallback

### Fixed

- **CI lint failures** — removed unused `FREE_PROVIDERS` and `since_iso` variables in `cli.py`; fixed f-strings without placeholders in `session-end.py`

## v1.7.0 — Multi-harness docs: claw-code, OpenClaw, Agno (2026-04-06)

### Added

- **claw-code MCP snippet** in README — open-source Rust Claude Code alternative; same hook/MCP protocol, no subscription (API-only users save even more)
- **OpenClaw MCP snippet** in README — `openclaw mcp add llm-router` one-liner for the Skills marketplace
- **Agno MCP example** in README — `MCPTools(command="llm-router")` pattern; teaser for v2.0 `RouteredModel` drop-in
- **v1.7–v2.1 roadmap sections** — documented multi-platform integration strategy (claw-code hooks, OpenClaw skill, Agno adapter, Learning Router)
- **`docs/plans/v2.0-ecosystem-integrations.md`** — full integration architecture doc with 4-layer model, per-system breakdowns, `RouteredModel`/`RouteredTeam` sketches, and phase plan

## v1.6.0 — `llm-router share` + star CTA (2026-04-06)

### Added

- **`llm-router share`** — generates a savings card (box-drawn ASCII), copies it to the clipboard, and opens a pre-filled Twitter/X tweet with your real savings numbers. Works on macOS (`pbcopy`), Linux (`xclip`/`xsel`), and Windows (`clip`).
- **Star CTA in session-end hook** — the first time your lifetime savings crosses $0.50, the Stop hook shows a one-time prompt: `⭐ Enjoying the savings? A star on GitHub helps others find it`. Never shown again after that session. Regular sessions show `run llm-router share to post it` instead.

## v1.5.2 — Call breakdown in status bar (2026-04-06)

### Changed

- **Status bar now shows call breakdown** — the `UserPromptSubmit` status bar now displays `sub:N · free:N · paid:N` (Claude subscription / Ollama+Codex / paid API) alongside the savings figure. Example: `📊  CC 33%s · 25%w · 45%♪   │   sub:0 · free:15 · paid:27   │   $0.008 saved (29%)`

## v1.5.1 — Free-model savings in session-end summary (2026-04-06)

### Added

- **Session-end summary shows free-model savings** — Ollama and Codex calls are now separated from paid external calls in the stop hook output. A new "Free models" section shows per-provider call counts, token volumes, and savings vs Sonnet baseline. Codex savings estimated from avg tokens/call when token counts aren't tracked. The combined savings tip (`💡 Saved ~$X.XX`) now includes both paid routing savings and free-model savings.

## v1.5.0 — Filesystem tools + free-model savings (2026-04-06)

### Added

- **`llm_fs_find` MCP tool** — describe files to find in natural language; cheap model (Haiku/Ollama) generates glob patterns and grep commands. Use with Claude's Glob/Grep tools for zero-Opus file discovery.
- **`llm_fs_rename` MCP tool** — describe a rename/reorganise operation; cheap model returns `mv`/`git mv` commands. `dry_run=True` (default) prefixes with `echo` for safe review before execution.
- **`llm_fs_edit_many` MCP tool** — bulk edit across multiple files. Accepts a file list or glob pattern; reads all files locally (free), sends to moderate-tier model, returns `{file, old_string, new_string}` JSON for mechanical application.
- **Free-model savings in `llm-router status`** — new "Free-model savings" section shows Ollama and Codex calls separately: call count, token volume, and estimated savings vs Sonnet-3.5 API rates. Codex token savings are estimated from average paid-provider tokens/call when exact counts aren't available.

## v1.4.2 — Dashboard data fixes, animated SVG demo (2026-04-06)

### Fixed

- **Dashboard savings gauge now reads real data** — was querying the empty `savings_stats` table; now calculates savings from `usage` table using the Sonnet baseline formula (same as `llm-router status`). Lifetime savings and efficiency gauge now show correct values.
- **Dashboard "Recent Routed Traffic" now populated** — was querying the empty `routing_decisions` table; now reads from `usage` table. Rows appear immediately after first routing call.
- **Dashboard version string** — was hardcoded `v1.3`; now comes from `importlib.metadata` at runtime and displays the actual installed package version.

### Added

- **Animated SVG demo in README** — replaces the static `demo.png` with a crisp CSS-animated SVG generated from a synthetic asciinema cast (`docs/demo.cast`). Shows `llm-router demo` (routing table, color-coded complexity) then `llm-router status` (pressure bars, savings chart) in a 10-second loop. Regenerate with `python scripts/gen_cast.py`.

## v1.4.1 — Smarter demo, real routing history, uninstall --purge (2026-04-05)

### Added

- **`llm-router demo` — real routing history**: when `~/.llm-router/usage.db` has external routing calls, `demo` now shows your actual last 8 routing decisions (prompt snippet, task type, complexity, model, cost) instead of static examples. Falls back to examples with "(no routing history yet — showing examples)" when DB is empty.
- **`llm-router uninstall --purge`**: removes hooks and MCP registration (existing behaviour), then optionally deletes `~/.llm-router/` (usage DB, `.env`, logs). Prompts for confirmation before deleting; cancels if user types anything other than `yes`.

### Fixed

- Removed unused `import sqlite3` in `_run_demo()` (ruff F401).

## v1.4.0 — Real savings dashboard, update command, Linux/Windows compat (2026-04-05)

### Added

- **`llm-router status` — real cumulative savings**: now shows today / 7-day / 30-day / all-time savings with ASCII bar charts (green = saved, yellow = spent), top models used, and colored subscription pressure bars.
- **`llm-router update`**: re-installs hooks and routing rules to the latest bundled version, then checks PyPI for a newer package version with upgrade hint.
- **Linux/Windows compatibility**: `dst.chmod(0o755)` is now skipped on Windows; hook `command` now uses `sys.executable` (the running Python interpreter) instead of the hardcoded `python3`, ensuring hooks work in pipx/venv/pyenv setups on all platforms.

### Fixed

- **CI no longer hangs**: added `timeout-minutes: 10` to CI job and `--timeout=30` per-test via `pytest-timeout`; added `timeout = 30` to `pyproject.toml` pytest config as local default.

## v1.3.9 — High-quality demo screenshot (2026-04-05)

### Changed

- Replaced vhs GIF (unreadable font) with a Chrome-rendered PNG (`docs/images/demo.png`) — crisp SF Mono, Dracula theme, 2× resolution, no username in paths.
- README now shows the PNG demo image; PyPI page updated accordingly.

## v1.3.8 — Improved demo GIF and table layout (2026-04-05)

### Changed

- Demo tape now uses plain `llm-router` (no hardcoded user path); terminal widened to 1100px so output never wraps.
- `llm-router demo` table: slimmer column widths (prompt 44, task 8, complexity 12, model 18, cost 9), cleaner model names (`Claude Haiku` / `Claude Sonnet` / `Claude Opus` instead of `Haiku (sub)` etc.).
- Regenerated `docs/images/demo.gif`.

## v1.3.7 — Friendly auth error messages (2026-04-05)

### Fixed

- **Authentication errors now show actionable hints** — when a provider returns a 401 (missing/invalid API key), the router emits a clear message naming the exact env var to set (`GEMINI_API_KEY`, `OPENAI_API_KEY`, etc.) and explains that Claude Code subscription covers Haiku/Sonnet/Opus without an API key. Previously these surfaced as raw LiteLLM exception text.
- "All models failed" terminal error now suggests `llm-router setup` when the root cause was auth, vs. `llm_health()` for other failures.

## v1.3.6 — Demo GIF, Ruff fixes (2026-04-05)

### Added

- **Demo GIF** (`docs/images/demo.gif`) — generated via VHS; embedded at top of README showing `demo`, `doctor`, and `status` commands in action.

### Fixed

- Removed three unused imports/variables in `cli.py` (`_CLAUDE_DIR`, two `rules_src` assignments) that caused ruff F401/F841 CI failures.

## v1.3.5 — Setup Wizard, Demo, Deep Reasoning (2026-04-05)

### Added

- **`llm-router setup`** — interactive wizard: walks through Claude subscription mode + optional provider API keys (Gemini, Perplexity, OpenAI, Groq, DeepSeek, Mistral, Anthropic), writes to `~/.llm-router/.env`, offers to run `llm-router install` at the end.
- **`llm-router demo`** — shows a table of routing decisions for 7 sample prompts against your active config (subscription mode, which providers are set), with savings estimate vs always-Opus. Color-coded by complexity, ANSI-aware column alignment.
- **`deep_reasoning` complexity tier** — new complexity value above `complex`: triggers extended thinking on Claude models (`thinking={"type": "enabled", "budget_tokens": 16000}` via LiteLLM `extra_params`). Routes to PREMIUM chain. Classifier system prompt updated to recognize it. Auto-route hook heuristics detect formal proofs, first-principles derivation, theorem proving, and philosophical analysis.

### Changed

- `auto-route.py` hook bumped to v8 (deep_reasoning heuristics).
- `ClassificationResult.header()` and `RoutingRecommendation.header()` show `[D]` tag for deep_reasoning.
- `COMPLEXITY_BASE_MODEL` and `COMPLEXITY_ICONS` include `deep_reasoning` entry.

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

- **Web dashboard** (`dashboard/`) — `llm-router dashboard [--port N]` starts a local `aiohttp` HTTP server at `localhost:7337`. Also accessible via the `llm_dashboard` MCP tool. Shows: today's calls/cost/tokens, monthly spend, lifetime savings vs Sonnet baseline, model and task-type distribution (7 days), daily cost trend (14 days), recent routing decisions table, and session quota. Auto-refreshes every 30 seconds. Self-contained single-file HTML — no build step. All DB values rendered via `textContent`/Chart.js arrays (no `innerHTML` XSS surface).

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

- `get_model_failure_penalty()` gains optional `failure_rates: dict[str, float] | None` parameter.
- `get_model_latency_penalty()` gains optional `latency_stats: dict[str, dict] | None` parameter.
- `apply_benchmark_ordering()` gains `failure_rates` and `latency_stats` optional parameters.
- `get_model_chain()` gains `failure_rates` and `latency_stats` optional parameters. Also now fetches `get_claude_pressure()` internally.
- `route_and_call()` now `await`s both `get_model_failure_rates()` and `get_model_latency_stats()` in parallel before building the model chain.

### Added

- **6 new tests** in `tests/test_profiles.py`: `TestResearchPressureTail`, `TestBudgetHardCap`, `TestPrefetchedPenalties`.

---

## v0.7.1 — Demo Reports & Docs (2026-03-31)

### Added

- **Demo report artifacts** — `demo/demo_report.{md,json}` and `demo/saas_demo_report.{md,json}` committed to repo as reference outputs from live routing sessions.

---

## v0.7.0 — Availability-Aware Routing & llm_edit Tool (2026-03-31)

### Added

- **Availability-aware routing** — Latency penalties folded into benchmark-driven quality score. P50/P95 latency tracked in `routing_decisions` over 7-day window. Thresholds: <5s=0, <15s=0.03, <60s=0.10, <180s=0.30, ≥180s=0.50 penalty.
- **Cold-start defaults** — `_COLD_START_LATENCY_MS` provides pessimistic P95 defaults for Codex models before any routing history exists.
- **`llm_edit` MCP tool** — Routes code-edit reasoning to a cheap model, returns exact `{file, old_string, new_string}` JSON for Claude to apply mechanically.
- **`src/llm_router/edit.py`** — New module with `read_file_for_edit()`, `build_edit_prompt()`, `parse_edit_response()`, `format_edit_result()`.
- **24 new tests** — `tests/test_availability_routing.py` and `tests/test_edit.py`.

---

## v0.6.0 — Subscription Mode & Quality-Cost Routing (2026-03-30)

### Added

- **Claude Code subscription mode** (`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`) — When enabled, `anthropic/*` models excluded from routing chains; no API key needed.
- **Quality-cost tier sorting** — Models grouped into 5% quality bands; within each band cheaper models sort first.
- **Cost pricing table** (`_MODEL_COST_PER_1K`) — blended per-1K-token costs for all 20+ routed models.
- **DeepSeek added to BALANCED and PREMIUM chains**.
- **Demo scripts** — `demo/app_builder_demo.py` and `demo/saas_builder_demo.py`.

### Fixed

- **Codex injection at front (subscription mode)** — When no Claude in chain, Codex is appended at the end as a free fallback rather than placed first (causing 300s timeouts).
- **Codex injected into RESEARCH chains** — Codex has no web access; now only injected for CODE, ANALYZE, GENERATE tasks.

---

## v0.5.2 — Ollama Local Models & Claude Mobile App Support (2026-03-30)

### Added

- **Ollama routing** — Local models as first-class routing targets. Set `OLLAMA_BASE_URL` + `OLLAMA_BUDGET_MODELS` to route to free local models before cloud fallback.
- **Claude mobile app access** — `llm-router-sse` CLI with SSE transport (port 17891) for remote connection via cloudflared tunnel.
- **`SessionStart` hook** — Auto-starts SSE server + cloudflared tunnel, prints mobile connection URL.

---

## v0.5.1 — Claude Haiku Budget Routing (2026-03-30)

### Added

- **Claude Haiku in BUDGET tier** — `anthropic/claude-haiku-4-5-20251001` added to all budget-tier text chains.

---

## v0.5.0 — Session Context Injection & Auto-Update Rules (2026-03-30)

### Added

- **Session context injection** — All text routing tools accept optional `context` parameter. Router prepends recent session messages and cross-session summaries so external models receive conversation history.
- **Two-layer context system** — Ephemeral ring buffer (current session) + persistent SQLite session summaries (previous sessions).
- **`llm_save_session` MCP tool** — Summarizes and persists current session to SQLite.
- **Auto-update routing rules** — `check_and_update_rules()` compares bundled vs installed rules version on startup; updates silently after `pip upgrade`.
- **Rules versioning** — `<!-- llm-router-rules-version: N -->` header in `llm-router.md`.

---

## v0.4.0 — Quality & Global Enforcement (2026-03-29)

### Added

- **Structural context compaction** — 5 strategies (collapse whitespace, strip comments, dedup sections, truncate long code, collapse stack traces). Reduces token usage 10-40%.
- **Quality logging** — `routing_decisions` SQLite table captures full routing lifecycle (21 columns).
- **`llm_quality_report` MCP tool** — ASCII analytics: classifier breakdown, task distribution, model usage, downshift rate.
- **Savings persistence** — JSONL file written by PostToolUse hook, imported into `savings_stats` SQLite table.
- **Gemini Imagen 3 + Veo 2** — Direct REST API for image and video generation.
- **Global hook installer** — `llm_setup(action='install_hooks')` + `llm-router-install-hooks` CLI.
- **Global routing rules** — `~/.claude/rules/llm-router.md` installed by hooks installer.

---

## v0.3.0 — Caching & Automation (2026-03-29)

### Added

- **Prompt classification cache** — SHA-256 exact-match + in-memory LRU (1000 entries, 1h TTL).
- **`llm_cache_stats` / `llm_cache_clear` MCP tools**.
- **Auto-route hook** — `UserPromptSubmit` hook with fast heuristic classifier (~0ms).
- **Rate limit detection** — catches 429 errors with 15s cooldown vs 60s for hard failures.
- **`llm_stream` MCP tool** — streaming LLM responses via async generator.
- **Usage-refresh hook** — `PostToolUse` hook detects stale Claude subscription data and nudges refresh.
- **Published to PyPI** as `claude-code-llm-router`.

---

## v0.2.0 — Intelligence Layer (2026-03-29)

### Added

- **Complexity-first routing** — simple → Haiku, moderate → Sonnet, complex → Opus.
- **Live Claude subscription monitoring** — fetches session/weekly usage from claude.ai.
- **Time-aware budget pressure** — reduces downshift urgency near session reset.
- **Codex desktop integration** — routes to local Codex CLI, free via OpenAI subscription.
- **`llm_usage` unified dashboard** — Claude %, Codex status, API spend, routing savings.
- **`llm_setup` tool** — discover/add API keys, view setup guides.
- **`llm_classify` tool** — classify task complexity, see routing recommendation.
- **`llm_check_usage` / `llm_update_usage`** — fetch and store Claude subscription data.

---

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
- CI with GitHub Actions
