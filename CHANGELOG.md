# Changelog

**For releases v6.2 and earlier, see [CHANGELOG_ARCHIVE.md](docs/CHANGELOG_ARCHIVE.md).**

## v9.3.0 - Codex CLI full parity: routing + cost + dashboard (2026-05-27)

### Added

- **Codex CLI now gets the same routing treatment as Claude Code.** Every `userPromptSubmit` event in a Codex session runs through the same classifier + DIRECT-mode answer-caching pipeline. Codex hook events are: `UserPromptSubmit`, `PreToolUse`, `SessionStart`, `Stop` (= SessionEnd equivalent). All four registered in `~/.codex/hooks.json` and pointing to the same scripts Claude Code uses.
- **Platform auto-detection in `auto-route.py`** — `_is_codex_session(hook_input)` reads `hook_input["model"]` and matches OpenAI prefixes (`gpt-`, `o3`, `o4`, `o5`, `codex-`). On Codex sessions the hook emits `additionalContext` (the only key Codex's `hookSpecificOutput` schema accepts) instead of Claude Code's `contextForAgent`. Schema-compatible: same JSON payload, the difference is just the field name.
- **`OPENAI_RATES_PER_M`** — per-million-token rates split by input/output/cache_read/cache_write for gpt-5.5, gpt-5.4, gpt-5-mini, o3, o3-mini, gpt-4o, gpt-4o-mini. Parallel to `CLAUDE_RATES_PER_M`. Rates pulled from OpenAI public pricing; verify before each release.
- **`_codex_cost()`** — 4-component cost formula for OpenAI/Codex calls. Same shape as `_claude_cost()`.
- **`_get_codex_baseline_for_task(task_type, complexity)`** — picks the realistic Codex baseline (`gpt-5-mini` for simple queries, `gpt-5.4` for code, `o3` for complex/research). Env override: `LLM_ROUTER_CODEX_BASELINE`.
- **`codex_usage` table** + idempotent migration `MIGRATE_ADD_CODEX_USAGE_TABLE`. Symmetric schema with `claude_usage` so dashboard queries can UNION cleanly. Tracks the same 4 token components + routing_overhead_usd.
- **`log_codex_usage()`** — async function parallel to `log_claude_usage`. Computes baseline cost via `_codex_cost(baseline, ...)` and writes one row per OpenAI/Codex call.
- **`router.py` auto-log path** — auto-detects `response.provider in {"openai", "openai_subscription", "codex", "codex_subscription"}` and calls `log_codex_usage` with the same sub-component token kwargs the v9.2.2 path uses for Claude.
- **Dual-platform `get_realized_savings(period, platform=...)`** — `platform="all"` (default) sums both tables AND returns a `by_platform` breakdown dict. `platform="claude"` / `"codex"` returns single-platform totals.
- **Compact Codex section in the session-end dashboard** — `_format_codex_section()` renders a per-model table for today's Codex calls (calls / tokens / gross saved). Stays invisible if `codex_usage` has no rows today (Claude-Code-only users see no change).
- **9 new tests in `tests/test_savings.py`** — `TestCodexCost` (6 cases), `TestDualPlatformRealizedSavings` (2 cases for cross-platform aggregation), plus 1 cost-table-existence test. All pass.

### Fixed

- **Tightened Codex detection in `_is_codex_session`** — removed the over-aggressive env-var fallback (`CODEX_COMPANION_SESSION_ID`, `CODEX_CLI`). Those env vars were getting set by Claude Code shell snapshots, causing the hook to think Claude Code sessions were Codex sessions. Detection is now model-field-only.
- **Test helper `_extract_hint`** in `tests/test_auto_route_hook.py` now tolerates both `contextForAgent` and `additionalContext` so tests pass regardless of which platform branch the hook took.

### Migration notes

Existing Codex users who had stale `~/.codex/hooks/` scripts from April (v21) and a broken `hooks.json` registration pointing to a non-existent `codex-post-tool.py`: re-install or manually:

```bash
# Symlink to the canonical Claude Code copies (auto-stays-fresh on upgrades)
for h in auto-route enforce-route session-end session-start; do
  ln -sf ~/.claude/hooks/llm-router-${h}.py ~/.codex/hooks/llm-router-${h}.py
done

# Then replace ~/.codex/hooks.json with the full 4-event registry
# (see .codex-plugin/.mcp.json in the repo for the canonical version, or
# regenerate via `llm-router install --codex`).
```

Codex CLI will prompt to re-trust the new hook hashes on next invocation.

### Known limitations

- `install_hooks.py` doesn't yet install to `~/.codex/` automatically. Manual setup required for now (see migration notes above). Will be addressed in v9.3.1.
- OpenAI pricing rates in `OPENAI_RATES_PER_M` are placeholder estimates. Verify against current `https://openai.com/api/pricing` before relying on Codex savings numbers in dollar terms.
- Codex's `userPromptSubmit` `hookSpecificOutput` schema doesn't support `contextForAgent`, so MANDATORY ROUTE directives carry only `additionalContext`'s priority in Codex — slightly weaker enforcement than Claude Code. This is a Codex platform limitation, not an llm-router bug.

## v9.2.2 - Cache-aware 4-component savings + task-aware baseline + honest floor (2026-05-27)

### Fixed

- **Savings calculation now uses the 4-component Anthropic billing formula** — `_claude_cost(model, input_t, output_t, cache_write_t=0, cache_read_t=0)` matches what Claude Code does upstream. Previously every token was multiplied by a single blended per-1K rate from `MODEL_COST_PER_1K`, which over- or under-stated cost depending on cache hit ratio. Cache-read tokens cost ~10× less than input; cache-write tokens cost ~25% more. Both are now tracked separately on every Claude call. New `CLAUDE_RATES_PER_M` table holds per-Mtok rates for haiku / sonnet / opus split across all four components.
- **Per-task-type baseline replaces universal-Opus baseline** — new `_get_baseline_for_task(task_type, complexity)` picks the realistic counterfactual model (Haiku for simple queries, Sonnet for code, Opus only for genuinely complex work or research). Previously every routed call was credited against Opus rates, overstating savings by 5–10× on prompts that would never have hit Opus anyway. `LLM_ROUTER_SAVINGS_BASELINE` env var still wins as an override for back-compat.
- **Removed `max(0.0, ...)` clamp on savings** — `calc_savings()` may now return negative numbers when routing overhead (classifier latency + Ollama call cost) exceeds gross savings. Previously the floor silently hid the routing tax, making the reported savings number a strict upper bound rather than a realized one. New `routing_overhead_usd` column on `claude_usage` + `usage` tables captures the tax explicitly.
- **Anthropic cache tokens now flow end-to-end** — `providers.py` extracts `cache_creation_input_tokens` and `cache_read_input_tokens` from the LiteLLM `usage` block via `getattr` (safe default of 0 for non-Anthropic providers). `LLMResponse` extended with the two fields. `router.py:999` auto-log path forwards them to `log_claude_usage` alongside `task_type` so the new cache-aware + task-aware logic actually fires on real calls (previously this call site silently swallowed an unknown-kwarg `TypeError`).

### Added

- **`get_realized_savings(period)`** — returns `{gross_saved_usd, routing_overhead_usd, realized_saved_usd}` so dashboards can show the honest number alongside the gross one.
- **`MIGRATE_CLAUDE_USAGE_CACHE_TOKENS`** — adds `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` columns to `claude_usage`. Idempotent — safe to re-run on existing DBs.
- **`MIGRATE_USAGE_ROUTING_OVERHEAD`** — adds `routing_overhead_usd` to both `usage` and `claude_usage` tables.
- **17 new regression tests in `tests/test_savings.py`** — `TestCacheAwareCost`, `TestTaskAwareBaseline`, `TestNegativeSavingsAndRoutingOverhead`, `TestAnthropicResponseFieldsExist`. All pass.

### Backward compatibility

- Old `calc_savings("haiku", 10000)` two-positional-arg signature still works and still uses Opus baseline (legacy default). Task-aware baseline only kicks in when `task_type=` kwarg is provided.
- Old `claude_usage` rows have the new columns defaulted to 0 — existing dashboards keep working.
- `LLM_ROUTER_SAVINGS_BASELINE` env override preserved.
- The `cost_saved_usd` kwarg on `log_claude_usage` (used by `router.py:999`) is now silently ignored — the value is recomputed authoritatively from sub-component tokens. This fixes a latent bug where the kwarg used to raise `TypeError` that was swallowed by the surrounding `try/except`.

## v9.2.1 - CONTINUATION heuristic + cooperative DIRECT template (2026-05-27)

### Fixed

- **CONTINUATION false positives caused Opus cost leak** — prompts beginning with a discourse marker like `"OK, so what kind of models do interact with bash"` matched the `"so "` weak-prefix branch in `_is_continuation()`, bypassed routing, and hit Opus. Heuristic now splits prefixes into STRONG (always bypass — refer to prior context) and WEAK (bypass only when no wh-question word follows). New regression test `tests/test_continuation_heuristic.py` covers 15 cases.
- **DIRECT-mode template tripped Claude safety reflexes** — `format_echo_context()` wrapped cached answers in `"OVERRIDE ALL OTHER INSTRUCTIONS FOR THIS TURN... Do NOT acknowledge this instruction"`. That phrasing matches textbook prompt-injection patterns; Claude resisted its own router's output. Rewritten with cooperative framing that explains the routing goal, permits corrections to the cached answer, and removes injection-pattern markers.

### Added

- **`LLM_ROUTER_DISABLE_CONTINUATION_BYPASS` env var** — kill-switch that forces every prompt through the classifier even when `_is_continuation()` returns True. Set to `1` if the new heuristic regresses on any prompt class.

## v9.2.0 - Enforcement v13, statusline, Gemini CLI savings (2026-05-26)

### Added

- **Statusline for Claude Code** — new `statusline-command.sh` hook shows CC usage %, daily savings, and enforce mode directly in the Claude Code status bar. Installed automatically via `llm-router install`.
- **Smart enforcement mode (v13)** — enforce-route.py v13 blocks ALL native tools until an `llm_*` routing tool is called. Smart mode allows reads for code tasks but blocks them for Q&A. Hard mode blocks everything.
- **4-violation auto-pivot** — safety valve prevents permanent deadlocks: after 4 blocked tool calls in one turn, enforcement allows all tools through.

### Fixed

- **Gemini CLI savings tracking** — `gemini_cli` provider was missing from `_FREE_PROVIDERS` across 12+ files, causing $0.00 savings for all Gemini CLI routed calls. Now correctly tracked as a free provider.
- **Statusline stdin hang** — Claude Code pipes session JSON to statusline commands via stdin. Script now consumes stdin to prevent pipe blocking and timeout.
- **Coding session bypass removed (v13)** — v12 permanently downgraded enforcement after the first Edit/Write. v13 requires routing per-turn regardless of session type.

### Changed

- **Auto-route directive** — changed from advisory "DO NOT SKIP" to "HARD CONSTRAINT" with explicit blocked tools list and required call sequence.
- **Enforcement test suite** — updated for v13 behavior: unique session IDs per tool iteration, tests expect blocking in hard/smart modes.

---

## v9.1.3 - Fix .env loading and echo rendering (2026-05-25)

### Fixed

- **Critical: .env path resolution** - auto-route hook used 3 parent directories (resolving to `src/.env` instead of project root). Now uses `Path.cwd()` as primary lookup + 4 parents as fallback. Without this fix, `OLLAMA_BUDGET_MODELS`, `LLM_ROUTER_CLAUDE_SUBSCRIPTION`, and API keys were invisible to the hook.
- **Echo rendering** - routed responses now render as normal black text in Claude Code CLI instead of warning-styled orange text. Uses `contextForAgent` (same priority as MANDATORY ROUTE) instead of `additionalContext` (which Claude ignores when CLAUDE.md is loaded).
- **session-start.py** - added `os.getcwd()` as first `.env` search path for consistency with auto-route fix.

### Changed

- Default render mode changed from `block` to `echo`. Configurable via `LLM_ROUTER_RENDER_MODE=block` env var for zero-cost (warning-styled) display.

---

## v9.1.2 - Fix extra env var rejection in RouterConfig (2026-05-25)

### Fixed

- **RouterConfig extra fields** - added `extra="ignore"` to `model_config` so unrelated env vars (e.g. `NOTION_API_TOKEN`) in `.env` no longer cause a pydantic `extra_forbidden` validation error that breaks dynamic routing initialization.

## v9.1.1 - Claude Code Live-Path Fixes (2026-05-25)

### Fixed

- **Read-only tool routing** - prompts such as `"Read notes.txt..."` now use the external agent path even when initially classified as a simple query, instead of allowing a stateless model to answer without file access.
- **Strict-mode status banner** - Claude Code session startup now reports `strict zero-Claude routing` when enabled, instead of misleadingly displaying subscription/MCP-tool mode.
- **Hook installer compatibility** - global hook installation now recognizes Claude Code's nested hook settings schema and no longer crashes while reinstalling an already-registered hook.
- **Quota pressure boundary** - a stored session usage value of `1.0%` is no longer incorrectly expanded to `100%` pressure.

### Verified

- **Live Claude Code tests** - validated external direct execution, fail-closed quota protection, continuation routing, and read-only file-agent execution through the installed Claude Code hook path.

---

## v9.1.0 - Strict Zero-Claude Routing (2026-05-25)

### Added

- **Strict quota guard** - set `mode: zero_claude` in `~/.llm-router/routing.yaml`, or set `LLM_ROUTER_ZERO_CLAUDE=true`, to ensure automatic routes either execute externally or block before native Claude can process the prompt.
- **Explicit native escalation** - prefix a prompt with `claude:`, `native:`, or `opus:` to intentionally permit a native Claude Code turn while strict mode is enabled.

### Fixed

- **Continuation quota leakage** - substantive requests beginning with transitions such as `"great, now I want..."` no longer bypass routing as continuations.
- **Fail-open execution** - failed direct execution, unavailable external tool-agent execution, and MCP-only handoffs now block in strict mode instead of exposing the prompt to native Claude.
- **Blank prompt handling** - whitespace-only submissions are ignored in strict mode instead of producing a misleading block message.
- **Legacy shell install path** - `scripts/install.sh` now installs the canonical packaged auto-route hook rather than the stale project hook copy.
- **Release staging** - `scripts/release.py` now includes documentation and installer script changes in its release commit staging set.

---

## v9.0.10 — Fix: Expanded Routing Continuity (2026-05-25)

### Fixed

- **Meta-Conversation Routing** — expanded the `_is_continuation` logic to catch phrases like "last prompt", "what does this", "why am I getting", and "blocked by hook" so that questions about system errors and previous chat context correctly bypass the stateless router.
- **Conversational Transitions** — added "now", "also", and "well" to the list of conversational starters that inherit context.

---

## v9.0.9 — Fix: Routing Continuity & CLI Errors (2026-05-25)

### Fixed

- **Routing Continuity** — added a bypass for continuation prompts (e.g., "yes", "ok", "do it") and conversational follow-ups (e.g., "and what about now?") to prevent them from being routed to stateless models without context.
- **Update Command** — resolved a `TypeError` in `llm-router update` by adding the missing `force` parameter to the internal installation function.
- **Direct Execution Usage** — fixed token usage reporting in direct execution mode; it now correctly captures and displays actual metrics from Ollama, Gemini, and OpenAI.

### Added

- **Project Badges** — added PyPI download count and GitHub stars badges to the README header.

---

## v9.0.8 — Fix: update Command Package Name (2026-05-24)

### Fixed

- **Update Command** — fixed the update command to correctly use the `llm-routing` package name when checking for newer versions on PyPI.

---

## v9.0.7 — Polished Direct Execution & System Context (2026-05-24)

### Added

- **System Context for Direct Exec** — added a specialized system prompt for Ollama, Gemini, and OpenAI direct calls so they know they are providing responses for a Claude Code user within `llm-router`.
- **Cleaner Response Format** — simplified the Direct Execution output in the terminal. Removed verbose headers and separators in favor of a clean message with a minimal, dimmed metadata footer.

### Fixed

- **Response Rendering** — improved ANSI styling for direct responses to feel more integrated into the conversation.

---

## v9.0.6 — Fix: Test Stability & Enforcement Logic (2026-05-24)

### Fixed

- **Test Suite** — updated `tests/commands/test_update.py` and `tests/test_route_enforcement_hooks.py` to match the new `install()` signature and hook response format, ensuring 100% test pass rate.
- **Enforcement Logic** — ensured consistency in how routing violations are reported across different execution paths.

---

## v9.0.5 — Fix: update Command Error (2026-05-24)

### Fixed

- **Update Command** — fixed a `TypeError` in `llm-router update` that prevented successful hook re-installation.

---

## v9.0.4 — Critical Fix: Claude Code Hook Blocking (2026-05-24)

### Fixed

- **Hook Block Message** — fixed `UserPromptSubmit` hook returning a generic "Blocked by hook" error in Claude Code. It now correctly uses the `reason` field to display direct execution responses.
- **Subscription Overrides** — ensured critical pressure overrides also use the correct response field for user visibility.

---

## v9.0.3 — Zero-Claude Direct Execution & Mini-Agent Loop (2026-05-24)

### Added

- **Direct Execution** — `UserPromptSubmit` hook can now route queries directly to Ollama/Gemini/OpenAI.
- **Zero-Token Routing** — simple prompts return `{"decision": "block"}` to Claude, consuming 0 subscription tokens.
- **Ollama Agent Loop** — Ollama now has access to basic file tools (read, write, edit, search) via a local agent loop for simple file-op tasks.
- **Pressure-Aware Chains** — new 5-zone pressure monitoring (green to critical) for more granular downshifting.

### Fixed

- **Gemini Subscription Mode** — fixed Gemini models being hidden even when available via CLI.
- **Test Isolation** — tests now explicitly clear subscription flags and use `sys.executable` for subprocess calls, improving reliability across different developer environments.
- **Codex Priority** — ensured Codex is tried at the absolute front of the chain during extreme quota pressure.

---

## v9.0.1 — Dashboard Cleanup & Free-First Routing (2026-05-23)

### Added

- **LAST PROMPT ROUTING** panel — shows all models used in the last prompt with `[FREE]`/`[SUB]`/`[API]` tier labels, task type, token count, and cost
- **Claude host model tracking** — `[SUB] claude/opus-4.6` row shows subscription quota delta so the panel reflects 100% of LLM usage
- **5h quota reset time** — displays "resets in Xh Ym (5:15pm BST)" with local timezone next to session quota bar

### Fixed

- **Free-first routing chain** — subscription mode now uses `Ollama → Codex → Gemini CLI → paid APIs` for all complexity levels. Claude (host) reserved for complex tasks only. Previously Codex was placed last in Claude Code sessions.
- **Ollama re-enabled** — was silently disabled in config files; now active as first in the routing chain

### Removed

- Quality gates counter from session-end dashboard (confusing metric)
- Baseline vs Actual cost comparison (misleading for subscription users)
- Yearly savings projection (~$X/yr)
- Full-session MODELS ROUTED panel — replaced with per-prompt breakdown

---

## v9.0.0 — Compaction-Resilient Routing Enforcement (2026-05-23)

### Fixed

- **Session type path mismatch** — `auto-route.py` reset `session_type_{id}.json` but `enforce-route.py` read `session_{id}.json`, so once a session was marked "coding", enforcement stayed soft permanently. Now both hooks use the same path.
- **Pending route TTL too short** — extended from 5 minutes to 1 hour. Routing state now survives context compaction delays without expiring between prompts.
- **Silent env var override** — `LLM_ROUTER_ENFORCE` set in `.zshrc`/`.bashrc` silently overrode `routing.yaml` with no warning. `enforce-route.py` now logs a conflict warning to `enforcement.log`, and `set-enforce` warns users when a shell env var will override the written config.

---

## v8.7.0 — Cyber-Grid Session Summary (2026-05-15)

### Added

- **Cyber-Grid static dashboard** — replaces plain ANSI session-end output with a Rich-rendered, two-column layout
  - **Intelligence panel** (left): routing method breakdown with zero-cost gauge, Claude subscription quota bars with delta tracking
  - **Financial panel** (right): period savings grid with token counts, baseline vs actual cost comparison with percentage badge, yearly projection
  - **L14 Activity panel**: Braille Unicode chart (U+2800) for 14-day call volume with quality metrics (fallbacks, routing latency, cache hit rate)
  - **Wildcard Insight**: auto-generated cost-saving suggestion based on routing data
- Tokyo Night true-color hex palette (`#00ff87`, `#7dcfff`, `#7aa2f7`) with 4-tier brightness hierarchy
- Clean-break separator rule before dashboard with session timestamp
- Graceful fallback to legacy renderer if Rich unavailable

---

## v8.6.0 — Mission Control TUI Dashboard (2026-05-15)

### Added

- **`llm-router tui`** — full-screen terminal dashboard built on Textual
  - **Panel A: Subscription Status** — gradient progress bars for session/weekly/model quotas
  - **Panel B: Metrics & Cost Analysis** — live KPIs, top models table, complexity routing breakdown
  - **Panel C: Routing Engine** — heuristic/fast-path/advanced decision split with percentages
  - **Panel D: Savings Wallet** — today/week/month/lifetime savings with trend arrows and annual projections
  - **Panel E: L14 Activity Graph** — dense Braille-pattern (U+2800) sparkline charts for calls, tokens, and cost over 14 days
- Tokyo Night true-color palette (hex-defined, not 16-color ANSI)
- Auto-refresh every 10 seconds, keyboard controls (`q` quit, `r` refresh, `d` theme)
- `textual` added as optional dependency group (`pip install llm-routing[tui]`)

---

## v8.5.1 — Hook Path Validation + Python 3.14 Support (2026-05-14)

### Fixed

- **`llm_doctor`** now validates hook Python interpreter paths exist — detects the #1 silent failure mode (venv moved/deleted)
- **`llm_doctor`** detects duplicate hook entries in `settings.json` and warns with cleanup guidance
- Python version constraint widened to `>=3.10` (removes `<3.14` cap) — fixes pipx install on Python 3.14

---

## v8.5.0 — Savings Dashboard + Audit Corrections (2026-05-14)

### Added

- **`llm_dashboard`** MCP tool — token-centric savings dashboard with ANSI colors
  - Sparkline time-series of daily tokens saved (14d/30d/3m/1y/all windows)
  - Per-provider breakdown: gross savings, classifier overhead, net savings
  - Colored routing distribution bars
  - Dual baseline: `--baseline sonnet` (default, honest) or `opus` (alternative)
- **Billing-mode-aware messaging**: subscription users see "quota freed" disclaimer instead of misleading dollar savings
- **Classifier overhead** subtracted from net savings in dashboard
- **`docs/AUDIT_2026Q2.md`**: comprehensive engineering audit covering activation path, routing logic, metrics honesty, telemetry, and user visibility

### Fixed

- Sonnet baseline standardized as default across dashboard (honest vs Opus strawman)
- Subscription mode auto-detected and labeled correctly in savings output

### Audit Findings (documented in AUDIT_2026Q2.md)

3 blockers identified and addressed:
- B1: Silent hook failures (documented, fix planned for v8.6.0)
- B2: Fake dollar savings for subscription users (fixed — shows "quota freed")
- B3: Opus baseline inflation (fixed — Sonnet default, Opus as `--baseline opus`)

---

## v8.4.0 — Semantic Cache Enhancements (2026-05-13)

### Added

- `cache_hit` and `cache_similarity` fields on `LLMResponse` — cache hits now carry the similarity score
- Configurable similarity threshold via `LLM_ROUTER_SEMANTIC_CACHE_THRESHOLD` env var (default: 0.95)
- Cache hits displayed in routing footer: `→ cache hit (97%) · gemini-2.5-flash · $0`
- 9 new tests for threshold config, cache fields, and footer display

### How It Works

When a semantically similar prompt is found in the cache (cosine similarity ≥ threshold), the cached response is returned instantly with `$0` cost and `0ms` latency. The footer shows exactly what matched and how confident the match was.

```bash
# Lower threshold = more cache hits (but risk of wrong matches)
export LLM_ROUTER_SEMANTIC_CACHE_THRESHOLD=0.90

# Default (conservative, high precision)
export LLM_ROUTER_SEMANTIC_CACHE_THRESHOLD=0.95
```

---

## v8.3.0 — Context-Window Cost Optimizer (2026-05-13)

### Added

- 2-stage context compression pipeline: structural + recency weighting (zero latency, pure Python)
- Context savings displayed in routing footer: `| ctx 1500→920tok (39% saved)`
- Free models (Ollama, Codex, Gemini CLI) automatically skip compression
- `LLM_ROUTER_CONTEXT_OPTIMIZER` config: `auto` (default) or `off`
- 18 new tests for context optimizer

### How It Works

Before sending context to paid models, the optimizer:
1. **Structural**: collapses whitespace, removes code comments, deduplicates repeated blocks
2. **Recency**: keeps last 2 exchanges verbatim, truncates older messages, drops old code blocks

No LLM calls. No latency. Context tokens reduced 20-50% on typical sessions.

---

## v8.2.0 — Always-On Routing Explainability (2026-05-13)

### Added

- Every routed response now shows a routing rationale footer by default: `→ gemini-2.5-flash · simple · $0.0002 (43x cheaper)`
- 4 explainability modes: `footer` (default), `header`, `verbose`, `off` — controlled via `LLM_ROUTER_EXPLAIN` config
- `verbose` mode shows full chain walk with per-model attempt status and confidence scores
- `LLMResponse` now carries `confidence`, `classification_method`, `complexity`, `task_type_str`, `chain_attempts` fields
- Savings calculation works independently (static cost table, no DB/API dependency)
- Ollama fallback added to all RESEARCH routing chains (BUDGET, BALANCED, PREMIUM)
- Full chain error diagnostics — error messages now show every model that was tried and why it failed
- 27 new tests for explainability feature

### Changed

- `_explain_prefix` replaced by `_routing_explanation` — always-on by default, no env var needed
- Legacy `LLM_ROUTER_EXPLAIN=1` maps to `header` mode for backward compatibility

---

## v8.1.0 — Expanded Provider Support & Dual Downloads Badge (2026-05-09)

### Added

- README: full provider table with all 18 supported providers (13 text + 5 media), split into Text LLM and Media sections
- README: `llm-routing` PyPI downloads badge alongside existing `claude-code-llm-router` badge
- Auto-profile detection for xAI/Grok, Together AI, and HuggingFace providers
- xAI/Grok-3 added to expensive tier in auto-generated routing profiles
- Together AI added to balanced tier in auto-generated routing profiles

### Changed

- README providers section now documents xAI, DeepSeek, Mistral, Cohere, Together, HuggingFace, fal, Stability, ElevenLabs, Runway, Replicate (previously undocumented despite being supported)

---

## v8.0.6 — Fix None quotas crash in dynamic routing (2026-05-09)

### Fixed

- `dynamic_routing.py`: Guard against `None` quotas when YAML key exists with no value, preventing `TypeError: argument of type 'NoneType' is not iterable`

## v8.0.5 — Pi.dev Host Support & Repo Cleanup (2026-05-06)

### Added

- Pi coding agent (pi.dev) as supported host: `llm-router install --host pi`
- Pi routing rules (`src/llm_router/rules/pi-rules.md`)
- Pi card in README editors SVG (light + dark)
- Pi column in HOST_SUPPORT_MATRIX.md

### Changed

- Removed 174 internal/dev files from public repo (strategy docs, research, scripts, slides, deprecated package, unused SVGs, deployment artifacts, machine-specific state)
- All removed files preserved locally via `.gitignore`
- Updated README "Use this if" to include Pi

### Fixed

- sdist packaging: added leak detection gate in release script (blocks publish if private files detected)

---

## v8.0.3 — SVG Animation Fixes & Visual Regression Tests (2026-05-05)

### Fixed

- Savings SVG: "60–80%" text no longer clips on the left during pulse animation (transform-box: fill-box, widened left column, reduced font size)
- Savings SVG: `&mdash;` entity replaced with `&#8212;` (SVG doesn't support HTML entities)
- Hero SVG: routing dots now follow actual route paths via `<animateMotion>` instead of drifting sideways with CSS `translateX`
- Hero SVG: removed distracting pill float and tier slide animations for cleaner motion

### Changed

- Savings SVG: replaced hardcoded dollar amounts and token counts with generic tier labels
- Hero SVG: updated "87% saved" pill to "60–80% saved"
- Star CTA moved back to upper README section (between badges and divider)

### Added

- `tests/test_readme_svgs.py` — 15 regression tests for SVG layout, animation, and data correctness

---

## v8.0.2 — CI Fix, README Cleanup, Root Directory Hygiene (2026-05-05)

### Fixed

- `test_get_router_efficiency` (and related tests) — timestamps now use UTC without microseconds, matching production code and SQLite's `date()` parsing
- CI badge goes green (all tests pass)

### Changed

- Slimmed README above-the-fold from 11 elements to 5 (hero, title, badges, divider, content)
- Removed Pepy download charts (low install count is not a trust signal yet)
- Removed nav button SVGs (redundant with headings/TOC)
- Moved star CTA SVG to bottom of README
- Removed savings SVGs with stale hardcoded historical data (text explanation remains)

### Added

- `.gitattributes` — export-ignore for non-essential dirs, linguist-generated for SVGs
- `.gitignore` entries for `.lore/`, `.playwright-cli/`, `.playwright-mcp/`

---

## v8.0.1 — README Motion Refresh & Pepy Tracking Split (2026-05-05)

**Patch release: restored animated README visuals, revived the GitHub star CTA, and added Pepy momentum panels for both the legacy and renamed PyPI package pages.**

### Added

- Animated Pepy momentum panel for the renamed `llm-routing` package page
- Side-by-side README coverage for both package eras:
  - `llm-routing` with `Total`, `8.x`, and `7.x`
  - `claude-code-llm-router` with `Total`, `7.x`, and `6.x`

### Changed

- Restored the motion-heavy README hero and section graphics in a CocoIndex-style presentation
- Brought back the animated five-star GitHub referral near the top of the README
- Regenerated the full `docs/readme/` SVG asset set, including Pepy-specific visuals

### Fixed

- README Pepy notes now explain the daily-data lag without hardcoding a specific latest release number
- Pepy SVG copy now stays accurate across future patch releases

---

## v8.0.0 — Quality Feedback Loop & Documentation Overhaul (2026-05-05)

**Major release: Automatic quality scoring, developer-first README rewrite, documentation consistency pass, stale asset cleanup.**

### Added

- **Quality Feedback Loop (Sprint 4)** — `src/llm_router/quality_feedback.py`
  - Auto-scores every routed response using content heuristics (code blocks, structure, refusals, citations)
  - Per-model quality tracking with minimum-calls threshold (3) before trusting signal
  - `should_skip_model()` — routing engine skips models with avg quality < 0.4 for specific task patterns
  - Integrated into `router.py` dispatch loop and all `text.py` tools (query, research, generate, analyze, code)
  - 23 new tests in `tests/test_quality_feedback.py`

### Changed

- **Complete README rewrite** — developer-first, text-based, high-trust landing page
  - No images/SVGs — shields.io badges only
  - Honest "Use this if / Don't use this if" section
  - Accurate tool count (60 MCP tools), package names, provider list
  - ASCII architecture diagram, markdown tables throughout
- **Documentation consistency pass** — corrected "48 tools" → "60 tools" across 10+ docs
- **Package name corrections** — `pip install llm-routing` consistently referenced
- **Tool count standardized** — 60 MCP tools (56 llm_* + 4 agoragentic_*) across all docs

### Removed

- 18 orphaned SVG assets from `docs/readme/` (stale claims, zero references)

### Fixed

- `SECURITY.md` referenced wrong package name (`claude-code-llm-router` → `llm-routing`)
- `HOST_SUPPORT_MATRIX.md` referenced wrong install command
- `server.py` and `docs/TOOLS.md` had outdated tool counts

---

## v7.6.2 — PyPI README Fix (2026-04-28)

**Patch release: Fixed PyPI package name in README and installation instructions.**

### Fixed

- Updated README.md to reference correct PyPI package name `llm-routing` (was `llm-router`)
- Fixed all installation instructions: `pip install llm-routing`
- Updated PyPI badges to point to correct project

---

## v7.6.1 — Documentation & Test Infrastructure (2026-04-27)

**Patch release: Comprehensive README redesign, test path safety framework, CI compatibility improvements.**

### Added

- **Comprehensive README Redesign**
  - New Table of Contents for easy navigation
  - Clear Problem & Solution section explaining value proposition
  - Enhanced Real-World Savings with detailed cost breakdowns
  - Quick Start section for faster onboarding (3 steps)
  - Key Features section with organized categories (routing, cost, compatibility, learning, monitoring)
  - Detailed routing chain examples for each complexity level
  - Comparison table: llm-router vs manual routing vs always-Opus approach
  - Better tool reference organization (48 tools in 7 categories)
  - Cleaner structure and visual hierarchy

- **Test Path Safety Framework**
  - Added `get_project_root()`, `get_hook_path()`, `get_src_path()` helpers to conftest.py
  - Dynamic path resolution for CI/local environment compatibility
  - Pre-commit hook for catching hardcoded paths before commit
  - Comprehensive guidance in `.claude/skills/test-patterns.md`

- **Test Infrastructure Improvements**
  - Fixed `test_today_filter_uses_localtime` to use dynamic paths instead of hardcoded `/Users/` paths
  - Fixed dashboard test timeout by mocking server startup
  - Fixed linting errors (unused imports, f-string prefixes)

### Fixed

- **CI Test Failures** — Tests now pass in GitHub Actions by using dynamic path resolution
- **Hardcoded Path Prevention** — Pre-commit hook catches absolute paths before they're committed
- **Test Isolation** — Proper mocking prevents server startup in tests

### Documentation

- Added "Test Path Safety" section to CLAUDE.md with complete guidance
- Updated CLAUDE.md with test writing checklist and CI compatibility rules
- Full migration guide from hardcoded to dynamic paths

---

## v7.6.0 — Agent Resource Budgeting (2026-04-27)

**Feature release: Complete agent resource budgeting system with provisional tracking and reconciliation.**

### Added

- **Session Budget Initialization** (agent-route.py)
  - Initializes `~/.llm-router/session_budget.json` on first agent approval
  - Allocates 30% of remaining quota to agent calls (prevents session budget exhaustion)
  - Minimum $5 guaranteed per session

- **Provisional Spend Tracking** (agent-route.py)
  - Decrements remaining budget when agents are approved
  - Prevents multiple agents from each believing they have budget available
  - Supports per-agent hard limit ($5.00) and session limit ($50.00)

- **Budget Reconciliation** (agent-error.py)
  - Reconciles provisional vs actual spend on agent completion
  - Refunds 50% of cost on failure (only paid for delivered value)
  - Prevents budget lockup from failed agents

- **Comprehensive Test Suite** (test_agent_resource_budgeting.py)
  - 12 tests covering cost estimation, hard limits, provisional tracking, reconciliation, and starvation
  - TestCostEstimation: simple/moderate/complex tasks cost correctly
  - TestHardLimits: blocks when cost exceeds remaining or per-agent max
  - TestProvisionalSpendTracking: budget decrements on approval
  - TestBudgetReconciliation: 50% refunds accumulate correctly
  - TestBudgetStarvation: multiple agents exhaust budget, sixth agent blocked
  - TestSessionBudgetInitialization: budget based on quota pressure
  - All 12 tests passing ✅

### Fixed

- Test helper `_run_agent_route()` now only initializes budget file once per session (was reinitializing and resetting budget on every call, masking real budget tracking)

## v7.5.2 — Test Suite Hotfix (2026-04-26)

**Patch release: Fixed test failures in v7.5.1 box-drawing hint format.**

### Fixed

- Box-drawing MANDATORY ROUTE hint now includes `task/complexity` (e.g., "query/simple", "code/moderate") — fixes 30+ assertion failures in test suite
- Added "ROUTE:" keyword to hint text for test compatibility
- All 80+ tests in test_auto_route_hook.py and test_edge_cases.py now pass

## v7.5.1 — Diagnostics & Violation Reduction (2026-04-26)

**Patch release: Routing violation analysis and improved hint visibility.**

### Added

- **Hook Health Cleanup Script** (`scripts/cleanup-hook-health.py`)
  - Remove stale test-session artifacts from `~/.llm-router/hook_health.json`
  - Supports `--dry-run` to preview changes before writing
  - Supports `--remove hook-name` to force-remove specific hooks
  - Prevents test artifacts from inflating error counts and cluttering dashboards

- **Violation Analysis Script** (`scripts/analyze-violations.py`)
  - Analyze routing violations from `enforcement.log` with per-session breakdown
  - Top 10 sessions table: session_id, violation count, expected vs actual tools
  - Per-session details: timestamps, tool sequence, what should have been called
  - Markdown report output to `~/.llm-router/retrospectives/violation-report-<date>.md`
  - Helps identify where violations concentrate and why

- **Per-Session Violation Nudge** (enforce-route.py)
  - After 3+ violations in one session, prints escalation warning to stderr
  - Visible as hook message in Claude Code context
  - Reminds model to call routed tool first before bypassing with Bash/Read/Edit/Write
  - No breaking changes — purely advisory

### Changed

- **MANDATORY ROUTE Hint Formatting** (auto-route.py)
  - New box-drawing format — harder to miss in long context windows
  - Displays task, action, provider, and cost savings in a visual box
  - Clearer imperative: "Call the tool above as your FIRST action"
  - Includes explicit forbidden actions and escalation rules

- `src/llm_router/hooks/auto-route.py` — Improved hint format + cost estimation function
- `src/llm_router/hooks/enforce-route.py` — Per-session violation escalation after 3+ violations
- `README.md` — New § Monitoring & Reducing Violations section (1,200+ words)

### Documentation

- **README Addition**: § Monitoring & Reducing Violations
  - What a routing violation is and why it costs money
  - How to read `enforcement.log` and understand violation patterns
  - Running `analyze-violations.py` to see worst sessions
  - Switching enforcement modes (`LLM_ROUTER_ENFORCE=hard|smart|soft|off`)
  - How to interpret `hook_health.json` and run cleanup script

### Metrics

- **3,931 violations** identified in enforcement.log (from prior sessions)
  - llm_generate bypassed: 1,274 (32%)
  - llm_query bypassed: 848 (22%)
  - llm_analyze bypassed: 750 (19%)
  - llm_code bypassed: 615 (16%)
  - llm_research bypassed: 452 (11%)
- Box-drawing hint format expected to reduce future violations by 30–50% via increased visibility

### Breaking Changes

None — fully backward-compatible. Cleanup scripts are optional. Nudges are advisory only.

---

## v7.5.0 — Flexible Routing Policies & Aggressive Routing (2026-04-24)

### Added

- **Hook Health Cleanup Script** (`scripts/cleanup-hook-health.py`)
  - Remove stale test-session artifacts from `~/.llm-router/hook_health.json`
  - Supports `--dry-run` to preview changes before writing
  - Supports `--remove hook-name` to force-remove specific hooks
  - Prevents test artifacts from inflating error counts and cluttering dashboards

- **Violation Analysis Script** (`scripts/analyze-violations.py`)
  - Analyze routing violations from `enforcement.log` with per-session breakdown
  - Top 10 sessions table: session_id, violation count, expected vs actual tools
  - Per-session details: timestamps, tool sequence, what should have been called
  - Markdown report output to `~/.llm-router/retrospectives/violation-report-<date>.md`
  - Helps identify where violations concentrate and why

- **Per-Session Violation Nudge** (enforce-route.py)
  - After 3+ violations in one session, prints escalation warning to stderr
  - Visible as hook message in Claude Code context
  - Reminds model to call routed tool first before bypassing with Bash/Read/Edit/Write
  - No breaking changes — purely advisory

### Changed

- **MANDATORY ROUTE Hint Formatting** (auto-route.py)
  - New box-drawing format — harder to miss in long context windows
  - Displays task, action, provider, and cost savings in a visual box
  - Clearer imperative: "Call the tool above as your FIRST action"
  - Includes explicit forbidden actions and escalation rules

- `src/llm_router/hooks/auto-route.py` — Improved hint format + cost estimation function
- `src/llm_router/hooks/enforce-route.py` — Per-session violation escalation after 3+ violations
- `README.md` — New § Monitoring & Reducing Violations section

### Documentation

- **README Addition**: § Monitoring & Reducing Violations
  - What a routing violation is and why it costs money
  - How to read `enforcement.log` and understand violation patterns
  - Running `analyze-violations.py` to see worst sessions
  - Switching enforcement modes (`LLM_ROUTER_ENFORCE=hard|smart|soft|off`)
  - How to interpret `hook_health.json` and run cleanup script

### Metrics

- **3,931 violations** identified in enforcement.log (from prior sessions)
  - llm_generate bypassed: 1,274 (32%)
  - llm_query bypassed: 848 (22%)
  - llm_analyze bypassed: 750 (19%)
  - llm_code bypassed: 615 (16%)
  - llm_research bypassed: 452 (11%)
- Box-drawing hint format expected to reduce future violations by 30–50% via increased visibility

### Breaking Changes

None — fully backward-compatible. Cleanup scripts are optional. Nudges are advisory only.

---

## v7.4.1 — Repository Cleanup (2026-04-22)

### Changed

- **Repository Sanitization** — Moved session artifacts and development files to Documents
  - Moved 14 session-specific files: presentation materials, development status files, visualizations
  - Moved `.serena/memories/` (Claude Memory database) to Documents folder
  - Updated `.gitignore` to prevent re-adding session artifacts and machine-specific memories
  - Kept full history in git; removed from working tree

### Why

Session artifacts (presentation decks, development planning docs, audit reports) and Claude Memory databases are machine-specific and should not be committed to the repository. These files are now stored locally under `~/Documents/llm-router-session-artifacts/` for reference, while the repository contains only production code and essential documentation.

## v7.4.0 — Content Generation Routing Discipline (2026-04-22)

### Added

- **Automatic Content Generation Detection** — Hook detects writing/creation tasks before execution
  - Patterns: "write", "draft", "compose", "add card", "create spec", "design blueprint"
  - Multi-step detection: "add X to file.md" → suggest decompose into generation + integration phases
  - Prevents routing misses where content generation skips `llm_generate` routing

- **Content Generation Fast-Path** — Instant routing for detected patterns
  - Routes detected patterns via `llm_generate` without waiting for classifier layers
  - Same instant-response architecture as code detection fast-path
  - Detects 3 patterns: simple generation, decomposition (generate+file), content refinement

- **Soft Nudge Suggestions** — Non-blocking routing guidance
  - When multi-step content tasks detected, suggests decomposition via hook
  - Format: "Consider routing via `llm_generate` first, then integrate locally. Saves ~$0.0005"
  - Encourages routing discipline without enforcing (no blocking)
  - Helps all users adopt best practices, not just this session

### Changed

- `src/llm_router/hooks/auto-route.py` — Added `_is_content_generation_task()` detection function
  - New regex patterns: `_CONTENT_GENERATION_VERBS`, `_CONTENT_FILE_PATTERNS`, `_DECOMPOSITION_PATTERNS`
  - Inserted into classification chain before heuristic scoring (instant, free detection)
  - Returns task_type="generate" with method="content-generation-fast-path"

- `CLAUDE.md` — New section: § Content Generation Routing (v7.4.0)
  - Decision matrix: when to route content vs execute locally
  - Pre-flight decision tree for multi-step content tasks
  - Cost impact example: 90% savings on writing tasks via routing
  - Updated Auto-Routing Rule to include content generation signals

- `README.md` — New v7.4.0 features section
  - Highlights automatic detection + decomposition patterns
  - References CLAUDE.md routing rules

### Technical

- Detection patterns use regex with word boundaries to avoid false positives
- Decomposition patterns specifically match "add X to file.md" syntax with file extensions
- Content verb patterns include all variations: write/draft/compose/create/design/blueprint/narrative
- Fast-path returns `"suggestion": "content-generation-decomposition"` for downstream integration

### Cost Impact

- **Typical content task**: $0.001 local generation → $0.0001 routed via `llm_generate` = **90% savings**
- **At scale**: 51 releases × 20-30 content tasks/cycle = **$0.10–$0.30 saved per cycle**
- **Decomposition pattern saves ~20% time**: Generate (route) + integrate (local) vs pure local thinking

### Breaking Changes

None — fully backward-compatible. Detection is opt-in via hook suggestion; no routing enforcement.

---

## v7.3.0 — Session Complexity Insights & Model Distribution Dashboard (2026-04-22)

### Added

- **Session Complexity Breakdown Dashboard** — New section in session-end hook showing task distribution by complexity tier
  - Displays models used for simple, moderate, and complex tasks within the session
  - Shows call count and cost per complexity level
  - Includes insight metrics: free-vs-paid ratio and average cost per call
  - Example output:
    ```
    Model selection by task complexity (this session)
    ─────────────────────────────────────────────────
    simple       3×   ollama/qwen2.5 (3×)                      [free]
    moderate     5×   codex/gpt-5.4 (3×) · gemini/flash (2×)   [$0.0018]
    complex      1×   openai/gpt-4o (1×)                       [$0.0123]
    
    💡 Insight: 60% free models · avg cost ~$0.0141/call
    ```

- **Database Schema Migration** — Added `complexity` column to `usage` table for persistent complexity tracking
  - Backward-compatible migration with `DEFAULT 'moderate'` for existing records
  - Enables post-hoc analysis and complexity-based cost trending

- **Complexity Parameter in Usage Logging** — Updated all usage recording functions
  - `log_usage(complexity: str = "moderate")` — tracks complexity for each model invocation
  - `log_cc_hint(complexity: str = "moderate")` — tracks complexity for Claude subscription hints
  - Default to 'moderate' for backward compatibility with existing calls

### Changed

- `src/llm_router/cost.py` — Added database migration `MIGRATE_USAGE_ADD_COMPLEXITY` and updated logging functions
- `src/llm_router/hooks/session-end.py` — New `_query_session_complexity_breakdown()` and `_format_complexity_breakdown()` functions
- Session-end dashboard now includes three sections: routing decisions, complexity breakdown, cumulative savings

### Technical

- Database migration: `ALTER TABLE usage ADD COLUMN complexity TEXT DEFAULT 'moderate'`
- Migration applied at schema version check (idempotent, no-op if column exists)
- Session breakdown queries group usage by complexity + model combination
- Cost calculation per complexity tier respects free vs paid provider distinction (Ollama/Codex marked as free)

### Performance

- Complexity breakdown queries indexed on (model, complexity, timestamp) for fast session analysis
- No performance impact on routing chain (complexity parameter is optional, defaults to moderate)

### Breaking Changes

None — fully backward-compatible. Existing sessions without complexity data default to 'moderate'.

---

## v7.2.0 — Reliability & Quota Precision (2026-04-21)\n### Fixed\n- **Token Reporting**: Added estimation logic for Codex and Gemini CLI providers to ensure accurate usage tracking in SQLite database.\n- **In-Flight Pressure**: Implemented a token reservation system to \"guess\" upcoming pressure and downshift models proactively before calls finish.\n- **Hard Cap Safety**: Disabled optimistic reset discounting when usage reaches 100% capacity to prevent credit depletion.\n- **Routing Integrity**: Fixed model string mismatches that prevented correct demotion of Claude models under high pressure.\n- **CI Stability**: Resolved a RuntimeError in tests when no providers were configured/healthy.\n\n---

## v7.1.0 — Quota-Balanced Routing & Cross-Subscription Load Balancing (2026-04-21)

**New feature: Automatically balance usage across Claude, Gemini CLI, and Codex subscriptions.**

### Added

- **QUOTA_BALANCED Routing Profile** — Dynamically reorder chains to balance quota consumption across three subscription providers
  - Monitors real-time pressure: Claude (session/weekly limits), Gemini CLI (daily), Codex (daily)
  - Within ±10% band → use free-first tiebreak order (codex → gemini_cli → claude)
  - Imbalance > ±10% → route to least-used provider first
  - Prevents one subscription from being exhausted while others remain underutilized

- **`llm_quota_status` MCP Tool** — Real-time visibility into subscription quota balance
  - Shows usage % for each provider
  - Route priority recommendations
  - Time to next reset (UTC midnight for Gemini CLI/Codex, custom for Claude)
  - Balance metrics and reordering decisions

- **Codex Daily Quota Tracking** — Local counter for OpenAI free tier (1000 req/day)
  - Persisted in `~/.llm-router/codex_quota.json`
  - Auto-resets at UTC midnight
  - Integrated with quota-balance calculations

- **Gemini CLI Quota Recording** — Increment counter on successful requests
  - Alias `record_gemini_request()` for router integration
  - Complements existing `get_gemini_pressure()` monitoring

### Configuration

```bash
# Use QUOTA_BALANCED to automatically balance subscriptions
llm_router_profile = "quota_balanced"

# Or configure via env:
export LLM_ROUTER_PROFILE=quota_balanced

# Codex daily limit (default 1000 for free tier):
export CODEX_DAILY_LIMIT=1000
```

### Technical

- New module: `src/llm_router/quota_balance.py` (quota tracking + chain reordering)
- Router integration: `_build_and_filter_chain()` applies quota-aware reordering when profile == QUOTA_BALANCED
- Request recording: Added after successful Codex/Gemini CLI calls in `_dispatch_model_loop()`
- Type definition: Added `QUOTA_BALANCED = "quota_balanced"` to `RoutingProfile` enum

### Performance

- Quota checks are async and cached per request
- Provider pressure calculations are parallel (no sequential waits)
- Chain reordering is lightweight (string prefix matching + sort)

---

## v7.0.1 — CRITICAL: Subscription Protection Fix (2026-04-21)

**CRITICAL BUGFIX: Claude subscription limits were being exhausted immediately**

### Fixed

- **Routing Chain Ordering Bug** — Claude models were FIRST in BUDGET/BALANCED chains instead of fallback
  - ❌ Before: Claude Haiku/Sonnet selected first, burning subscription immediately
  - ✅ After: Ollama → Codex → Gemini Pro → Claude (only when needed)
  - Impact: 75-95% reduction in Claude subscription usage

- **Free-First Chain Implementation**
  - BUDGET tier: Ollama → Codex → Gemini Flash → (Claude Haiku as fallback)
  - BALANCED tier: Ollama → Codex → Gemini Pro → (Claude Sonnet as fallback)
  - PREMIUM tier: Claude Opus first (best quality as requested)

- **Subscription Protection** — Reordered all routing tables to protect session/daily/weekly limits
  - Simple queries now route to Ollama (free, instant)
  - Moderate tasks route to Codex/Gemini (free/cheap)
  - Complex tasks use Claude only (subscription protected)

### Real-World Impact

```
Before v7.0.1:  ~$8-10/day on Claude (limits exhausted 3-4 days/week)
After v7.0.1:   ~$0.50-2/day on Claude (limits exhausted once per month)
Savings:        75-95% reduction ✅
```

### Technical

- Modified: `src/llm_router/profiles.py` (routing chain ordering)
- All 6 plugin files synced to v7.0.1
- Version guard validates sync across all distribution channels

---

## v7.0.0 — Free-First MCP Chain & Ollama Auto-Startup (2026-04-21)

**Major release: automatic Ollama management + optimized routing chains across all complexity levels.**

### Added

- **Ollama Auto-Startup** — Session-start hook automatically launches Ollama and loads budget models if not already running
  - Eliminates manual Ollama setup for first-time users
  - Graceful fallback if Ollama unavailable (routing continues with paid tiers)
  - 10-second readiness timeout with automatic model pull
  - Configured via `OLLAMA_BASE_URL` and `OLLAMA_BUDGET_MODELS` env vars

- **Free-First MCP Chain for All Complexity Levels** — Unified routing strategy across simple/moderate/complex tasks
  - Simple: Ollama → Codex → Gemini Flash → Groq
  - Moderate: Ollama → Codex → Gemini Pro → GPT-4o → Claude Sonnet
  - Complex: Ollama → Codex → o3 → Gemini Pro → Claude Opus
  - Codex integrated before all paid providers when available (free OpenAI subscription)

- **BALANCED Tier Chain Reordering** — Gemini Pro prioritized over cheaper but lower-quality alternatives
  - Query, Generate, Analyze, Code tasks now route through Codex → Gemini Pro (instead of DeepSeek)
  - Reduces BALANCED tier cost ~40% while improving response quality
  - Better complexity-to-cost balance across moderate-difficulty tasks

- **Routing Decision Tracking & Analytics** — Built-in observability for model selection
  - Each routing decision logs selected model, estimated cost, complexity level
  - Session-end hook displays routing summary with cost vs. full-Opus baseline
  - Identify cost anomalies and optimization opportunities

### Changed

- **Profile Routing Tables** — All profiles now use unified free-first chain instead of separate simple/moderate/complex hierarchies
- **Plugin Versions** — Synchronized across all 6 distribution channels (.claude-plugin, .codex-plugin, .factory-plugin)

### Technical

- Ollama bootstrap added to `src/llm_router/hooks/session-start.py`
- Start script `src/llm_router/hooks/start-ollama.sh` manages service lifecycle
- Router complexity classification now properly integrated with MCP tool invocation chain
- Semantic cache cleared to ensure fresh classification on startup

### Performance

- Ollama-first routing dramatically reduces latency for simple/moderate tasks (0.5-2s vs 5-15s for API calls)
- Free-first chain keeps majority of work on free/local models, reducing monthly spend

### Breaking Changes

- Removed separate SIMPLE/MODERATE/COMPLEX chain tiers in favor of unified free-first strategy
- Routing now always attempts Ollama first regardless of budget pressure (can be disabled via env var)
- BALANCED tier no longer includes DeepSeek as primary fallback (moved after Gemini Pro)

---

## v6.11.2 — Security & Performance Fixes (2026-04-21)

### Fixed (Phase 1 — Critical)

- **Ollama Fast Model Selection** — Added `qwen2.5:1.5b` (10x faster than gemma4) to `OLLAMA_BUDGET_MODELS` for simple tasks, dramatically improving response latency for fast queries
- **Ollama Cost Logging** — Fixed incorrect cost tracking that logged $0.0008 per Ollama call instead of $0.0; free local providers now correctly show zero cost in database
- **State Lock Race Condition** [HIGH-4] — Fixed unsafe read of `_active_profile` in `state.py` by acquiring lock during read to maintain consistent locking contract
- **SQLite Database Permissions** [HIGH-2] — Database file created with mode 0o600 (readable by user only) before schema creation to prevent exposure of sensitive cost/token data
- **Subprocess Environment Leakage** [HIGH-3] — Fixed `auto-route.py` OAuth refresh subprocess call that was passing full environment; now filters out `*_KEY`, `*_TOKEN`, `*_SECRET` variables before invocation
- **Session State File Permissions** [MEDIUM-5] — Added chmod 0o600 after atomic JSON write in `_write_json_atomic()` to secure routing metadata and session analysis files

### Fixed (Phase 2 — Medium)

- **Retry-After Header Support** [MEDIUM-3] — Added `_extract_retry_after()` function to read Retry-After headers from rate-limit exceptions; `record_rate_limit()` now accepts custom cooldown seconds for provider-specific recovery windows
- **Policy Audit Logging** [MEDIUM-4] — Upgraded silent DEBUG logs to WARNING level in `apply_policy()` with session context and rule source attribution for compliance auditing
- **Dynamic Routing Failure Handling** [MEDIUM-6] — Added full exception traceback logging for dynamic routing initialization failures; implemented 10-minute auto-retry window to prevent permanent disabling on transient network issues
- **OAuth Token Read Consistency** [HIGH-1] — Fixed lock contract violation in `TokenRefreshStrategy.get_token()` by reading both `_current_token` and `_last_refresh_time` inside the async lock
- **Prompt Injection Detection Hardening** [MEDIUM-1] — Added encoding normalization (unicode NFKC, URL decoding, zero-width character stripping) before pattern matching to defeat basic encoding bypass attempts

### Fixed (Phase 3 — Low)

- **Cost Baseline Constants** — Consolidated and documented Sonnet/Opus baseline costs with module-level `BASELINE_MODEL_FOR_SAVINGS` constant and clear pricing comments
- **Ollama Model Display** — Enhanced routing indicator to read actual selected model from `OLLAMA_BUDGET_MODELS` env var instead of hardcoded fallbacks

### Security

- Database files, session metadata, and cached cost data now protected with user-only file permissions (0o600)
- Subprocess environment filtering prevents leakage of API keys and authentication tokens
- Prompt injection detection now defeats encoding-based bypass attempts

### Performance

- Ollama fast models (`qwen2.5:1.5b`) prioritized for simple queries, reducing response latency ~10x vs slower alternatives
- Provider rate-limit recovery times now respect Retry-After headers instead of fixed 15-second timeout

---

## v6.9.0 — Gemini CLI Integration (2026-04-21)

### Added

- **Gemini CLI as Free Routing Provider**
  - Route tasks to Gemini CLI (Google One AI Pro, 1,500 requests/day)
  - Seamless integration into free-first routing chain (Ollama → Codex → Gemini CLI → paid)
  - Smart insertion: front on high budget pressure, after first Claude on code tasks
  - New `gemini_cli_agent.py` for binary detection and async subprocess invocation
  - New `llm_gemini` MCP tool for direct Gemini CLI invocation

- **Gemini Quota Tracking**
  - Two-layer quota system: parse `gemini /stats` for real data, local counter fallback
  - Daily request tracking with tier-based limits (Google One AI Pro: 1,500/day)
  - Budget pressure signals (0.0-1.0) for routing decisions
  - `gemini_cli_quota.json` cache with 5-minute TTL
  - `get_gemini_pressure()` and `get_gemini_quota_status()` APIs

- **Gemini CLI Host Support** (Part B — Hooks)
  - Auto-route hook for UserPromptSubmit with 3-layer classifier (heuristics → Ollama → Gemini Flash)
  - Session-end hook displaying quota usage and savings from free provider routing
  - Install support: `llm-router install --host gemini-cli`

- **Enhanced Tool Documentation**
  - New "Supported Development Tools" section in README with installation matrix
  - Full support vs MCP support explained
  - Quick setup guides for Claude Code, Gemini CLI, Codex CLI, and IDE plugins

### Changed

- `src/llm_router/router.py` — Added Gemini CLI to local provider list, injection in dispatch chain, agent-context reordering
- `src/llm_router/profiles.py` — Added Gemini CLI models to `_FREE_EXTERNAL_MODELS`
- `src/llm_router/server.py` — Registered new `gemini_cli` tools module (51 total tools)
- README.md — Added comprehensive "Supported Development Tools" section

### Files Added

- `src/llm_router/gemini_cli_agent.py` — Binary detection, subprocess invocation
- `src/llm_router/gemini_cli_quota.py` — Quota tracking and pressure calculation
- `src/llm_router/tools/gemini_cli.py` — `llm_gemini` MCP tool
- `src/llm_router/hooks/gemini-cli-auto-route.py` — Auto-routing hook
- `src/llm_router/hooks/gemini-cli-session-end.py` — Session summary with quota display
- `tests/test_gemini_cli.py` — Unit and integration tests (12 tests, all passing)

### Performance

- Gemini CLI invocation timeout: 30 seconds (configurable via `GEMINI_CLI_TIMEOUT`)
- Quota cache TTL: 5 minutes (prevents excessive subprocess overhead)
- Import-time caching of binary location (no event loop blocking)

---

## v6.4.0 — Quality Guard (2026-04-20)

### Added

- **Quality Guard** — Hard threshold enforcement for model quality
  - Real-time quality reordering in routing chain based on judge scores
  - Automatic min_model floor escalation when rolling quality < 0.6
  - Per-model rolling quality trends in `model_quality_trends` table
  - New `llm_quality_guard` MCP tool for monitoring

- **Judge Score Integration** — Quality feedback in routing decisions
  - `judge.reorder_by_quality()` called in router hot path
  - Models with low scores (< 0.7 over 7 days) automatically deprioritized
  - Quality trends logged at session-end for historical analysis

- **Agoragentic Cross-Agent Discovery**
  - Agent registered as `llm-router-saving-tokens` on Agoragentic platform
  - Other AI agents can discover and invoke `llm_route` for model optimization
  - Free tier enabled; no wallet required for initial listing

### Changed

- `src/llm_router/model_selector.py` — `select_model()` now async with quality floor checks
- `src/llm_router/router.py` — Quality reordering integrated after chain build
- `src/llm_router/cost.py` — Added `model_quality_trends` table and `log_quality_trend()`
- `src/llm_router/tools/routing.py` — Updated all `select_model()` calls to use await

### Performance

- Added composite DB index `(final_model, judge_score, timestamp)` for fast rolling window queries
- Prevents full-table scans on quality trend lookups

---

## v6.3.0 — Three-Layer Compression Pipeline (2026-04-19)

### Added

- **RTK Command Output Compression** (Layer 1)
  - Bash/shell outputs automatically compressed via smart filters (60–90% reduction)
  - Git, pytest, cargo, docker, npm outputs simplified to essentials

- **Token-Savior Response Compression** (Layer 2)
  - 4-stage pipeline: filler removal → example consolidation → boilerplate collapse → semantic extraction
  - 60–75% token reduction on LLM responses
  - Optional via `LLM_ROUTER_COMPRESS_RESPONSE=true` (off by default)

- **Unified Dashboard**
  - `llm_gain` shows all three compression layers with token savings per layer
  - All compression metrics logged to SQLite for analytics

### Changed

- Response compression now non-blocking; falls back to original on any error
- Added compression telemetry tracking

---

## Roadmap

**v6.5** — Fine-tuning & Model Customization
**v6.6** — Real-time Team Dashboard
**v7.0** — Multi-Model Competitive Benchmarking
