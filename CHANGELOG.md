# Changelog

## v2.6.0 — Latency-Aware + Personalized Routing (2026-04-08)

### Added

- **User-acceptance feedback loop** (`src/llm_router/cost.py`)

  New `get_model_acceptance_scores(window_days=30)` function queries the `was_good` column from `routing_decisions` table to compute per-model acceptance rates. Models with < 50% acceptance receive up to a 40% score penalty in benchmark ordering — pushing poorly-rated models down the routing chain automatically.

- **Acceptance penalty in benchmark ordering** (`src/llm_router/benchmarks.py`)

  New `get_model_acceptance_penalty(model, acceptance_scores)` function with three tiers:
  - ≥ 70% acceptance → no penalty
  - ≥ 50% acceptance → 20% penalty
  - < 50% acceptance → 40% penalty

  Wired into `apply_benchmark_ordering()` via the new `acceptance_scores` parameter, threaded through `get_model_chain()` in `profiles.py` and the `route_and_call()` async gather in `router.py`.

- **Model Performance section in `llm_usage`** (`src/llm_router/tools/admin.py`)

  New section in the usage dashboard showing per-model P50/P95 latency (7-day window) and user acceptance rate (30-day window). Lists top 8 models by call count.

- **`smart` enforcement mode** (`src/llm_router/hooks/enforce-route.py` v6)

  New default enforcement mode that achieves >80% routing compliance without blocking developer workflow:
  - **query / research / generate / analyze** tasks → hard block (Bash/Edit/Write blocked until `llm_*` called — the answer must come from the cheap model)
  - **code** tasks → soft (file tools are needed for actual editing, not blocked)

  Previous default `hard` → new default `smart`. Users can override with `LLM_ROUTER_ENFORCE=hard|soft|off`.

- **Stale pending state cleanup on session start** (`src/llm_router/hooks/session-start.py` v11)

  Session start now clears any orphaned `pending_route_*.json` files from crashed or killed sessions. Previously, a hard-killed Claude session left stale state files that would block Bash/Edit in the next session.

### Changed

- `LLM_ROUTER_ENFORCE` default changed from `hard` → `smart`
- `apply_benchmark_ordering()` signature extended with `acceptance_scores: dict[str, float] | None = None`
- `get_model_chain()` signature extended with `acceptance_scores: dict[str, float] | None = None`
- `route_and_call()` now fetches acceptance scores in parallel with failure_rates and latency_stats

---

## v2.5.0 — Context-Aware Routing (2026-04-08)

### Added

- **Continuation prompt state inheritance** (`src/llm_router/hooks/auto-route.py` v16)

  Short follow-up prompts (`yes`, `ok`, `go ahead`, `do it`, `sounds good`, etc.) now instantly reuse the prior turn's `task_type/complexity/tool` instead of re-running the full Ollama/API classifier chain (~1–3s saved per continuation turn).

  - `_is_continuation()` — detects affirmatives, negatives, and ≤5-word zero-signal prompts
  - `_save_last_route()` / `_load_last_route()` — session-scoped state at `~/.llm-router/last_route_{session_id}.json` with 30-minute TTL
  - Negative continuations (`no`, `stop`, `skip`, `cancel`) downgrade to `query/simple → llm_query`
  - Routing directive shows `[via context-inherit]` method tag
  - Session states are keyed by `session_id` so parallel Claude sessions never interfere

---

## v2.4.0 — Repo-Aware YAML Config (2026-04-08)

### Added

- **`src/llm_router/repo_config.py`** — Two-layer YAML configuration system

  Loads and merges two optional config files with clear precedence:
  ```
  env vars > .llm-router.yml (repo) > ~/.llm-router/routing.yaml (user) > defaults
  ```

  - `RepoConfig` dataclass: `profile`, `enforce`, `block_providers`, `routing` (per-task model/provider pins), `daily_caps`
  - `load_user_config()` — reads `~/.llm-router/routing.yaml`
  - `load_repo_config()` — searches cwd and ancestors for `.llm-router.yml`
  - `effective_config()` — merges both (repo wins over user config)
  - `fingerprint_repo()` — auto-detects Python/Node/Go/Rust/Java/Swift/Ruby/PHP from indicator files, suggests appropriate profile

- **`llm-router config` sub-commands** (`src/llm_router/cli.py`)

  - `llm-router config show` — displays effective settings with per-field source annotation
  - `llm-router config lint` — validates YAML files, surfaces unknown keys and invalid values
  - `llm-router config init` — creates starter `.llm-router.yml` with repo-fingerprinted profile suggestion

- **`llm-router onboard` wizard** (`src/llm_router/cli.py`)

  Interactive first-run setup: detects available providers (Ollama, Codex, API keys), recommends a profile, lets you pick enforcement mode (shadow/suggest/enforce), writes `~/.llm-router/.env`, and runs `llm-router install`.

- **Block providers + model/provider pins** (`src/llm_router/router.py`)

  Repo config `block_providers` list is applied before routing — blocked providers are removed from the fallback chain. Per-task `routing.{task_type}.model` / `.provider` pins prepend or reorder the chain accordingly.

### Technical

- `TaskRouteOverride` dataclass: `model`, `provider` fields (both optional)
- `VALID_TASK_TYPES`, `VALID_PROFILES`, `VALID_ENFORCE` schema constants for strict YAML validation
- `_merge()` combines two configs — override wins for scalars, lists are unioned

---

## v2.3.0 — Zero-Friction Activation (2026-04-08)

### Added

- **Yearly savings projection** (`src/llm_router/hooks/session-end.py` v14)

  Session-end summary now shows a `📈 Projection: ~$X/year · ~Xk tok/year (based on 7-day avg)` line alongside daily/weekly/monthly cumulative savings, giving a concrete annual ROI signal.

  - `_fmt_tok()` helper: human-readable token counts (e.g. `42k`, `1.3M`)
  - Projection extrapolates from 7-day average when available, falls back to today's rate

- **Monday weekly digest** (`src/llm_router/hooks/session-start.py` v10)

  Fires once per week (on Mondays or after 6-day gap), shows last 7 days: calls, tokens, USD saved, and yearly projection. Writes `~/.llm-router/last_weekly_digest.txt` to prevent repeat firing within the same week.

- **Shadow / suggest / enforce activation modes** (`src/llm_router/hooks/auto-route.py` v15, `enforce-route.py` v4)

  Control routing enforcement level via `LLM_ROUTER_ENFORCE` env var or `.llm-router.yml`:

  | Mode | Behaviour |
  |---|---|
  | `shadow` | Passive `👁 ROUTING OBSERVATION` — no pending state, no blocking |
  | `suggest` | Soft `💡 SUGGESTED ROUTE` hint — pending state written, enforce-route only logs |
  | `enforce` / `hard` | `⚡ MANDATORY ROUTE` — blocks non-routed tool calls (default) |

---

## v2.2.0 — Explainable Routing (2026-04-08)

### Added

- **`LLM_ROUTER_EXPLAIN=1` response prefix** (`src/llm_router/tools/text.py`)

  When set, every routed response (`llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`) is prefixed with a compact routing summary:

  ```
  [→ gemini-2.5-flash · query · $0.00003 · 42.9x cheaper than Sonnet]
  ```

  Shows: model used, task type, per-call cost, and cost ratio vs Sonnet baseline — the "why this model?" answer right in the response stream.

- **`llm_classify` cost comparison table** (`src/llm_router/tools/routing.py`)

  The classification output now includes a "Why not a more expensive model?" breakdown showing Opus/Sonnet/Haiku costs side-by-side with the chosen tier, including a multiplier for each skipped tier (e.g. "↑ 60x more expensive — unnecessary for simple task"). Always shown; no env var required.

- **`reason_code` DB column** (`src/llm_router/cost.py`)

  New column in `routing_decisions` table for storing classification reasoning codes (idempotent migration). `log_routing_decision()` updated with `reason_code: str | None = None` parameter.

- **`router.py` reason_code propagation** — passes `reason_code` from classification metadata to `log_routing_decision()`.

### Technical

- `_explain_prefix()` helper: pure function, zero overhead when env var not set.
- Cost table uses per-1k-output-token pricing — representative of real-world savings signal.
- Routing tip injected into `llm_classify` output when `LLM_ROUTER_EXPLAIN` is not set.

---

## v2.1.0 — Route Simulator + Savings Dashboard (2026-04-08)

### Added

- **`llm-router test "<prompt>"` dry-run CLI** (`src/llm_router/cli.py`)

  Simulates a routing decision for any prompt without making an API call. Uses the existing 5-layer classifier to determine task type, complexity, and confidence, then maps to the cheapest appropriate model and shows an estimated cost vs Sonnet baseline.

  ```
  llm-router test "refactor the auth module to use JWT"
  → Task: analyze / moderate / 85% confidence (via gemini-flash-lite)
  → Chosen: claude-sonnet-4-6  Baseline: claude-sonnet-4-5
  → Saved: $0.00465 (100% cheaper)
  ```

- **`llm_savings` MCP tool** (`src/llm_router/tools/admin.py`)

  Text-based savings dashboard with time-bucketed aggregates: today / this week / this month / all-time. Shows actual spend, Sonnet baseline, savings, and the efficiency multiplier (Nx) — the "wow" metric that makes routing value tangible.

- **DB schema migration v2.1** (`src/llm_router/cost.py`)

  Four new columns added to `usage` table via idempotent `ALTER TABLE` (safe for existing DBs): `baseline_model`, `potential_cost_usd`, `saved_usd`, `is_simulated`.

- **`get_savings_by_period()`** (`src/llm_router/cost.py`) — async savings query used by status bar and `llm_savings`. Falls back to Sonnet estimation for pre-v2.1 rows.

- **Enhanced status bar v3** (`src/llm_router/hooks/status-bar.py`) — D/W savings, provider health icons (auto-hidden until `health.json` active), enforcement mode badge, Nx efficiency multiplier. Full mode via `LLM_ROUTER_STATUS_MODE=full`.

### Fixed

- **Ruff F541** (`src/llm_router/cli.py:1275`) — spurious `f` prefix on string with no placeholders; broke CI lint on both Python 3.11 and 3.12.

---

## v2.0.2 — Fix release CI for Agno tests (2026-04-07)

### Fixed

- **Release and CI test jobs now install the `agno` extra** (`.github/workflows/ci.yml`, `.github/workflows/publish.yml`)

  The workflows were running the full test suite, including `tests/test_agno_integration.py`, but only installing `--extra dev`. That made both CI and tag-based publish fail before release because `agno` was missing. Both workflows now install `uv sync --extra dev --extra agno` before running tests.

## v2.0.1 — Harder Claude Code routing enforcement (2026-04-07)

### Changed

- **Release metadata now matches the shipped hook changes** (`pyproject.toml`, `uv.lock`)

  The package version is now `2.0.1`, so the existing tag-based publish workflow can cut a real release for the Claude Code enforcement changes already merged on `main`.

- **Routing enforcement now defaults to `hard` in Claude Code hooks** (`src/llm_router/hooks/enforce-route.py`)

  `LLM_ROUTER_ENFORCE` now defaults to `hard` instead of `soft`. When `auto-route.py` issues a `⚡ MANDATORY ROUTE` directive and Claude tries to jump straight to `Bash`, `Write`, `Edit`, or `MultiEdit`, the `PreToolUse` hook blocks that work by default instead of merely logging it.

- **Missed routed turns are now surfaced on the next prompt** (`src/llm_router/hooks/auto-route.py`)

  Claude Code still has no hook that fires immediately before a plain text response, so same-turn self-answering cannot be blocked directly. To make those misses visible, `auto-route.py` now detects a leftover `pending_route_{session_id}.json` from the prior turn, logs it as `NO_ROUTE` in `~/.llm-router/enforcement.log`, clears the stale state, and injects a warning into the next `⚡ MANDATORY ROUTE` context.

- **Installer and README now document hard-by-default behavior** (`src/llm_router/tools/setup.py`, `README.md`)

  Post-install messaging now tells users that routed work is blocked by default unless they explicitly set `LLM_ROUTER_ENFORCE=soft` or `off`.

- **Demo outputs now default to an ignored folder and repo noise was removed** (`demo/app_builder_demo.py`, `demo/saas_builder_demo.py`, `.gitignore`)

  The demo scripts now write reports to `demo/output/` by default, and the repo no longer tracks generated demo reports, Finder metadata, or stray root-level screenshots.

### Added

- **Hook regression tests for enforcement behavior** (`tests/test_route_enforcement_hooks.py`)

  Covers:
  - hard-default blocking of work tools
  - soft-mode override still logging violations
  - carry-over logging for unrouted previous turns

## v2.0.0 — Agno integration: RouteredModel + RouteredTeam (2026-04-07)

### Added

- **`RouteredModel` — drop-in Agno model with smart routing** (`src/llm_router/integrations/agno.py`)

  Use llm-router as a first-class Agno model. Every agent call is classified by complexity and routed to the cheapest capable provider (Ollama → Codex → paid APIs).

  ```python
  from llm_router.integrations.agno import RouteredModel
  from agno.agent import Agent

  agent = Agent(
      model=RouteredModel(task_type="code", profile="balanced"),
      instructions="You are a coding assistant.",
  )
  agent.print_response("Write a Python quicksort.")
  ```

  Parameters: `task_type` (query/research/generate/analyze/code/image/video/audio), `profile` (budget/balanced/premium), `model_override` (pin a specific model). Accepts both string values and enum instances.

- **`RouteredTeam` — multi-agent team with shared budget enforcement** (`src/llm_router/integrations/agno.py`)

  Agno `Team` subclass that automatically downshifts all `RouteredModel` members to the `budget` routing profile when monthly spend reaches a configurable threshold.

  ```python
  from llm_router.integrations.agno import RouteredModel, RouteredTeam

  team = RouteredTeam(
      members=[coder_agent, researcher_agent],
      monthly_budget_usd=20.0,
      downshift_at=0.80,  # downshift when 80% of budget spent
  )
  ```

  Budget check runs before each `run()` / `arun()` call. Non-fatal: if the cost database is unavailable, the team continues without downshifting.

- **`agno` optional dependency group** — install with `pip install "claude-code-llm-router[agno]"`.

- **19 new tests** (`tests/test_agno_integration.py`) covering model construction, invoke/ainvoke/stream, multi-turn message handling, token metadata, budget pressure downshifting, and team integration.

---

## v1.9.4 — Fix: content filter silent fallback + model_override subscription bypass (2026-04-07)

### Fixed

- **Content filter errors surfaced to user as scary warnings** — When Anthropic (or any provider) returned a `400 "Output blocked by content filtering policy"` error, the router was treating it as a generic failure: showing a warning notification to the user AND tripping the circuit breaker, penalising the provider for what is a content policy decision, not an infrastructure failure. Users saw the raw API error even though the router was successfully falling back to the next model.

  **Fix**: Added `_is_content_filter_error()` detection for content filtering markers. These errors are now **silently skipped** — no user-visible warning, no circuit breaker trip — so the router tries the next model transparently.

- **`model_override` bypassed subscription mode filter** — When a caller (tool or hook) passed an explicit `model="anthropic/claude-haiku-4-5-20251001"` override, the `available_providers` filter was never applied. In subscription mode this meant an Anthropic API call was attempted despite `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`.

  **Fix**: When `model_override` starts with `anthropic/` and subscription mode is active, the override is silently replaced with the balanced chain (Anthropic models excluded), logged at WARNING level.

---

## v1.9.3 — Fix: actively purge ANTHROPIC_API_KEY from live env in subscription mode (2026-04-07)

### Fixed

- **v1.9.2 fix was incomplete for already-running servers** — `apply_keys_to_env()` now skips exporting `ANTHROPIC_API_KEY` in subscription mode, but the currently running MCP server had already exported it at startup. Since the MCP server is a long-lived process, any `ANTHROPIC_API_KEY` already in `os.environ` before my fix persisted and LiteLLM continued using it.

  **Fix**: `get_config()` now actively calls `os.environ.pop("ANTHROPIC_API_KEY", None)` on every invocation when `llm_router_claude_subscription=True`. This purges the key from the live environment regardless of how or when it got there — covering pre-existing keys, keys injected by the shell, and keys exported before the server started.

---

## v1.9.2 — Fix: block ANTHROPIC_API_KEY export in subscription mode (2026-04-07)

### Fixed

- **Anthropic API still called in subscription mode** — `apply_keys_to_env()` was unconditionally exporting `ANTHROPIC_API_KEY` into `os.environ` at MCP server startup, even when `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`. LiteLLM reads keys directly from the environment at call time, so any code path (routing chain, classifier, tool fallback) that encountered an `anthropic/*` model would successfully make a direct API call — billing separately from the Claude Code subscription.

  The `available_providers` filter (which correctly excludes `anthropic` in subscription mode) guarded the main routing chains, but was insufficient as a sole defense because LiteLLM's environment-level key meant the call could still succeed if a model slipped through any bypass.

  **Fix**: `apply_keys_to_env()` now skips exporting `ANTHROPIC_API_KEY` when `llm_router_claude_subscription=True`. This is a hard guarantee: LiteLLM cannot call Anthropic at all in subscription mode, regardless of which model is requested.

---

## v1.9.1 — Fix: llm_select_agent codex detection (2026-04-06)

### Fixed

- **`llm_select_agent` reported `codex_binary: false` when the Codex CLI was installed** — the tool called `is_codex_plugin_available()` (which checks for the openai/codex-plugin-cc Claude Code plugin directory) instead of `is_codex_available()` (which checks for the actual `codex` binary at known paths and `$CODEX_PATH`). Users with Codex at `/usr/local/bin/codex` got `claude_code` as primary instead of `codex` even on budget profile.

---

## v1.9.0 — Routing enforcement + session-level agent selection (2026-04-06)

### Added

- **`enforce-route.py` hook (PreToolUse, all tools)** — hard-enforces routing compliance. When `auto-route.py` issues a `⚡ MANDATORY ROUTE` directive, it writes a session-scoped pending state file (`~/.llm-router/pending_route_{session_id}.json`). The new `enforce-route.py` hook fires before every tool call:
  - If Claude calls an `llm_*` tool → routing honored, state cleared, allow.
  - If the tool is context-gathering (Read, Glob, Grep, LS, NotebookRead) → always allow.
  - If a work tool (Write, Edit, MultiEdit, Bash) fires before routing → enforce per `LLM_ROUTER_ENFORCE`:
    - `soft` (default) — log violation to `~/.llm-router/enforcement.log`, allow the call.
    - `hard` — block the call with a remediation message telling Claude to call the routing tool.
    - `off` — disable enforcement entirely.
  - State expires after 5 minutes to avoid stale blocks.

- **`llm_select_agent` MCP tool** — session-level agent routing for orchestrators like claw-biz. Given a task prompt and profile, returns which agent CLI (claude_code / codex) + model to invoke for the whole session — before starting, not mid-session. Decision tree:
  ```
  budget   + simple/moderate → codex     + gpt-4o-mini
  budget   + complex         → codex     + gpt-4o
  balanced + simple          → codex     + gpt-4o-mini
  balanced + moderate        → claude_code + sonnet
  balanced + complex         → claude_code + opus
  premium  + any             → claude_code + opus
  research (any profile)     → claude_code + sonnet (needs web access)
  ```
  Returns JSON with primary agent, model, fallback, env_check, and a ready-to-run CLI invocation hint.

### Addresses GitHub Issues

- [Issue #1](https://github.com/ypollak2/llm-router/issues/1) — CC-MODE `/model` slash commands silently ignored: **fixed in v1.8.4**, upgrade resolves it (`pip install --upgrade claude-code-llm-router`).
- [Issue #2](https://github.com/ypollak2/llm-router/issues/2) — Hard-enforce routing directives: implemented via `enforce-route.py` + `LLM_ROUTER_ENFORCE`.
- [Issue #3](https://github.com/ypollak2/llm-router/issues/3) — `llm_select_agent` session-level routing classifier: implemented as new MCP tool.

### Hook versions

- `auto-route.py`: v11 → v12 (writes pending state for enforcement)
- `enforce-route.py`: v1 (new)

---

## v1.8.5 — Persistent statusline stats bar (2026-04-06)

### Changed

- **Statusline: persistent `📊` stats bar** — the Claude Code bottom status bar now shows the llm-router savings stats continuously instead of the old `🔀 last-model · HH:MM · Ntok` last-call indicator. The new display persists across the whole session:
  ```
  …/Projects/my-app  main | claude-sonnet-4-6 | 📊  CC 13%s · 24%w · 43%♪   │   sub:0 · free:15 · paid:27   │   $0.008 saved (29%)
  ```
  `%s` = session · `%w` = weekly · `%♪` = Sonnet monthly · `sub/free/paid` = call counts

- The statusline now calls `llm-router-status-bar.py` directly (the same hook that fires before every prompt) so the data is always fresh.

### How to apply

If you installed llm-router before this release, re-run the statusline installer or update `~/.claude/statusline-command.sh` to call the status-bar hook:

```bash
llm-router install          # re-installs all hooks and updates statusline
```

---

## v1.8.4 — Fix: remove broken /model directives, always route via MCP tools (2026-04-06)

### Fixed

- **CC-MODE `/model` directives never worked** — the hook was emitting `⚡ MANDATORY ROUTE: query/simple → /model claude-haiku-4-5-20251001 (subscription)` but Claude Code's model cannot execute slash commands from hook context (neither interactive nor `claude -p` mode). Every CC-MODE simple/complex routing directive was silently ignored. Removed the entire `/model` directive path.
- **All routing now goes via MCP tools** — `simple` → `llm_query`, `moderate` → `llm_analyze`/`llm_generate`/`llm_code`, `complex` → `llm_code`/`llm_analyze`. The free-first chain (Ollama → Codex → cheap API) keeps costs low in both subscription and API-key modes.
- **`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`** now only enables inline OAuth refresh and session-end delta reporting — it no longer changes routing behaviour (routing was broken anyway).
- **Session-start banner updated** to describe actual MCP-tool routing instead of the broken `/model` model-switch table.

### Hook versions

- `auto-route.py`: v10 → v11
- `session-start.py`: v7 → v8

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
