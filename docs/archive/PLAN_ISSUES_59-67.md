# Implementation Plan: Issues #59–#67 (post-13.0.4)

Verified against HEAD by a Fable 5 planning pass. All nine diagnoses hold; two had path corrections.

## 1. Verification table — does each diagnosis hold at HEAD?

| Issue | Verdict | Evidence |
|---|---|---|
| #59 set-enforce reads wrong env var | **Confirmed** | `commands/set_enforce.py:79` and `enforce_config.py:91` read only `CLAUDE_SESSION_ID`. `session_store.py:252-278` (`resolve_session_id`) has the correct 4-tier chain incl. `CLAUDE_CODE_SESSION_ID` (line 266); neither enforce file uses it. On real Claude Code, `set_enforce.py:80` (`if session_id and not _global:`) is always false → global `routing.yaml` write at line 106 while the message claims session-only. |
| #60 `routing_decisions` never written from primary surface; snapshot crash | **Confirmed (both halves)** | Gate `if classification_data:` at `router.py:1979` in `_finalize_successful_route` (def 1817); `route_and_call` (def 3271) defaults `classification_data=None` (3284). Zero occurrences in `tools/text.py`; only `tools/routing.py:385/410/570/592` builds it. Consolidated `llm()` (`tools/consolidated.py:77`) delegates to text.py → structurally cannot log. Crash: `retrospective.py:185` returns `classification_accuracy: None` for empty decisions (deliberate, GH#56, comment 164-176) but consumers do `facts.get("accuracy", 1.0) * 100` — `commands/snapshot.py:66,103,139,143`, `monitoring/live_tracker.py:63,68,104,137-138`, `monitoring/periodic.py:91,173`. Key-present-with-None defeats `.get()` default → TypeError. |
| #61 unknown subcommand launches MCP server | **Confirmed** | `cli.py::main()` (def 806) ends ~1028 with `else: ... _mcp_main()`; flat if/elif over ~45 literal names, no unknown-command branch. |
| #62 ensemble model never validated | **Confirmed** (file is `src/llm_router/ensemble.py`, not `classifier/ensemble.py`) | `DEFAULT_PRIMARY = "ollama/qwen2.5:7b"` line 71, `_primary_model()` 74-75; `classify_ensemble` defaults `primary=.../qwen2.5:7b`, `secondary=.../qwen2.5-coder:32b` lines 223-224. `commands/doctor.py:928-951` and `commands/verify.py::check_ollama` (127-150) fetch `/api/tags` but never compare. |
| #63 statusline "no provider" misleading | **Confirmed** (path is `src/llm_router/hooks/statusline-command.sh`) | Lines 352-373: `keys` at 355 has no `LLM_ROUTER_CLAUDE_SUBSCRIPTION`; 30-min windows at 365 and 370; verdict at 373. |
| #64 `should_skip_model()` overrides pin | **Confirmed** | `quality_feedback.py`: `_MIN_CALLS_FOR_SIGNAL=3` (84), `QUALITY_THRESHOLD=0.4` (87), `should_skip_model` 247-260, zero `getenv` in file. `router.py:2418` (in `_dispatch_model_loop`, def 2086) exempts only `model_override`; routing.yaml pins enter via `_build_and_filter_chain` (`pinned_model`, 531-539) as ordinary chain members → skippable. Skip precedes `chain_attempts.append(model)` at 2493 → invisible. `llm()` exposes no `model=` (`consolidated.py:77-83`) so the exemption is unreachable from the primary surface. |
| #65 `LLM_ROUTER_PROFILE` collision | **Confirmed** | `repo_config.py:89` reads it for cost tier (`effective_profile`, def 87, unaware of `LLM_ROUTER_DEPLOYMENT_PROFILE`); `profile.py:38` canonical + `:51` legacy fallback for identity; `identity.py:3,79,216,328`; `server.py:265,274` comments acknowledge it. Blast radius small: only caller is `commands/config.py:142` (display). |
| #66 stale tool surface in docs/skills | **Confirmed** | `guide/TROUBLESHOOTING.md` references retired tools on ~20 lines (12,18,50-58,75,83,117,146,152,164,180,229-230,248-249,270,316,359,376,388-391,408). `LLM_ROUTER_SQL_DEBUG` (427) / `LLM_ROUTER_HOOK_DEBUG` (430): zero matches in `src/`; `LLM_ROUTER_LOG_LEVEL` does exist. `skills/route/SKILL.md:37-41,54,60-69` pre-consolidation names. `.claude-plugin/plugin.json` declares `"mcpServers": ".mcp.json"`; that file does not exist. `llm()` has no `model=` → FAQ claim false. |
| #67 naming sweep | **Confirmed, one wrinkle** | `rules/llm_router.md:11,13` says `llm_router set-enforce off` / `llm_router doctor`; `pyproject.toml:86` declares only `llm-router`. Same mistake in source strings: `doctor.py:920`, `install.py:138` print `fix="llm_router install"`. `skills/release/SKILL.md:49-54` old hook names vs real `llm_router-*.py` (`install_hooks.py:374-388 HOOK_SPECS`); ~82 `claude-code-llm-router`. `docs/BENCHMARKS.md:12` dead package name. `TROUBLESHOOTING.md:202,306,312` old hook globs; `:451` impossible import (`from llm_router.hooks.auto_route import main` — file is `auto-route.py`). Version-tag regex `install_hooks.py:116` matches only `llm_router-rules-version:`, so `rules/llm-router.md:1` and `.github/copilot-instructions.md:1` are invisible to staleness checks. The `llm-router.md` (v5) vs `llm_router.md` (v9) fork is real — `install_hooks.py:57-66` already treats destination `llm-router.md` as a pre-rebrand artifact to delete, so the shipped source fork is itself legacy. |

Wrinkle on #67: the reporter's own repros ran `llm_router ...` successfully — likely a stale pipx shim/alias on the host. `pyproject.toml`'s `llm-router` is ground truth.

## 2. Dependency / ordering analysis

- **#60 and #64 both touch `router.py`**, different functions (`_finalize_successful_route` ~1979 vs `_dispatch_model_loop` 2417-2493 + `_build_and_filter_chain` 531-539). No semantic dependency, but land #60 first and rebase #64 — and once #60 populates `routing_decisions` from the primary surface, verifying #64 gets much easier.
- **#59 and #63** share only the theme "what env does Claude Code actually export"; disjoint files, separate PRs.
- **#64 and #66 share a decision**: whether to re-add `model=` to `llm()`. If yes, #66's FAQ correction changes and #64's `model_override` exemption becomes reachable. Decide before finalizing either.
- **#66 + #67 are one docs PR** (`TROUBLESHOOTING.md` appears in both). Exception: the `.mcp.json`/`plugin.json` gap and the deletion of `rules/llm-router.md` are *functional*, flag for real review.
- **#62 and #63** both consume Ollama `/api/tags` but share no code (bash vs python). No ordering constraint.
- **#65** is independent; any new env name must be registered in `env_registry.py`, so land it before the docs PR.
- **#61** is fully independent.
- #60's snapshot-crash consumers must land in the **same** PR as the logging change: populating the table doesn't fix the empty-DB case, and `accuracy: null` is already on disk in 13.0.4-written snapshots — readers must tolerate `None` forever.

## 3. Sequenced plan — PR-sized batches

### PR 1 — #59: session-id resolution for enforce scoping
**Files:** `src/llm_router/enforce_config.py`, `src/llm_router/commands/set_enforce.py` (+ test)

- `enforce_config.py::_session_enforce` (91): replace bare `os.environ.get("CLAUDE_SESSION_ID")` with `session_store.resolve_session_id()` (guarded import — runs inside hooks, must never raise; keep the `_SAFE_SESSION_ID.fullmatch` validation).
- `set_enforce.py::_run_set_enforce` (79): same substitution. Writer and reader MUST use the identical resolver.
- Update the docstring at `enforce_config.py:83` to describe the real chain.

**Repro/test:** extend `tests/test_gh49_set_enforce_is_session_scoped.py` — env with **only** `CLAUDE_CODE_SESSION_ID`, `HOME` at a tmpdir, run `set-enforce soft`, assert `~/.llm-router/sessions/<sid>/enforce` == `soft` **and** `routing.yaml` untouched; then `resolve_enforce_mode()` returns `soft`. Negative test: neither var set → global write *and* the message says global, not "(this session only)" (the wording looks selected before the branch decision — verify and fix).

### PR 2 — #60: routing_decisions from the primary surface + None-safe accuracy
**Files:** `router.py`, `commands/snapshot.py`, `monitoring/live_tracker.py`, `monitoring/periodic.py` (+ tests)

**Part A (the gate):** in `_finalize_successful_route` (router.py:1979) relax `if classification_data:` so the decision logs when it's `None`, synthesizing from what the function already has: `task_type.value`, `profile.value`, `classifier_type="caller"`/`"unhinted"`, resolved complexity, `recommended_model=base_model=model`, final model/provider/tokens/cost from `response`. Fixing at the **sink** covers `llm`, `llm_query`, `llm_code`, `llm_analyze`, `llm_generate`, `llm_research` and future callers, instead of chasing every call site in `tools/text.py`. Optionally also pass the hook-hint complexity (`_effective_complexity`) through so `classifier_type` distinguishes hook-classified from unhinted.

**Part B (the crash):** every `facts.get("accuracy", 1.0)` consumer must handle key-present-`None` — `snapshot.py:66,103,139,143`, `live_tracker.py:63,68,104,137-138`, `periodic.py:173`. Pattern: `acc = facts.get("accuracy"); pct = "n/a" if acc is None else f"{int(acc*100)}%"` — display "n/a", honoring `measured: False` (retrospective.py:177-187), don't resurrect the fake 100%. `periodic.py:91` may keep passing `None` into snapshot files; readers are what must tolerate it, including files 13.0.4 already wrote.

**Repro/test:** (1) stub the dispatch layer, call `route_and_call(TaskType.QUERY, ..., classification_data=None)` against a tmp DB, assert one `routing_decisions` row — the reporter's controlled repro as CI; (2) feed `{"facts": {"accuracy": None}}` to `format_hourly_snapshots` and live_tracker — no exception, "n/a"; (3) regression: `analyze_facts([])["classification_accuracy"] is None` stays.

### PR 3 — #64: quality breaker vs. explicit pins (rebase on PR 2)
**Files:** `router.py`, `quality_feedback.py` (+ test). Three parts, pending decisions §4a.

1. **Never skip a pinned model** (recommend unconditional): `_build_and_filter_chain` already computes `pinned_model` (531-539). Propagate to `_dispatch_model_loop` and extend router.py:2418 to `if model not in (model_override, pinned_model) and should_skip_model(...)`. The CHZ-AUD-C-02 comment at 2412-2416 states the principle; this extends it to the second kind of explicit pin.
2. **Make skips visible**: at the skip branch (2418-2430) append a marker to `chain_attempts` (e.g. `f"{model} [quality-skipped avg={avg:.2f} n={n}]"`) so `routing_quality.jsonl` and error summaries show the model was a candidate. `route_log.info("model_quality_skip", ...)` exists but goes only to structured logs nobody reads.
3. **Configurable thresholds**: replace literals at 84/87 with env-read constants — `LLM_ROUTER_QUALITY_SKIP_THRESHOLD` (0.4), `LLM_ROUTER_QUALITY_MIN_CALLS` (3), `LLM_ROUTER_QUALITY_SKIP=off` to disable entirely. Register in `env_registry.py`.

**Repro/test:** seed `_quality_store` with `(pinned,"query","simple") → avg 0.2, n=5`, pin that model via tmp routing.yaml, stub the backend, assert the pinned model is *attempted*. Second: non-pinned → skipped AND visible in the trace. Third: `LLM_ROUTER_QUALITY_SKIP=off` disables skipping. Mirrors the reporter's repro without 8 live Ollama calls.

### PR 4 — #61: unknown CLI subcommand errors out
**Files:** `cli.py` (+ test)

In `main()` (def 806) change the final `else` (~1028): start the MCP server **only** when `args` is empty (preserve documented no-arg behavior). For a non-empty unrecognized `args[0]`: print `llm-router: unknown command '<x>' — see 'llm-router --help'` to stderr, `sys.exit(2)`. Cleanest mechanical route given the flat chain: collect known names into a set/dispatch dict, or insert an `elif args:` error branch before the final `else`. Optional `difflib.get_close_matches` "did you mean". Do **not** add `help`/`health` subcommands — the bug is narrower.

**Repro/test:** subprocess test — `llm-router nosuchcmd` exits 2 within a timeout, stderr contains "unknown command", and prints **no** MCP startup lines; plus no-args still reaches the server path (mock `server.main`).

### PR 5 — #62: doctor/verify flag missing ensemble model
**Files:** `commands/doctor.py`, `commands/verify.py`, `ensemble.py` (export only) (+ test)

In doctor's Ollama section (928-951), after fetching `model_names`, compare `ensemble._primary_model()` (strip `ollama/`, handle implicit `:latest`) against the list. If absent: `_warn` with an actionable fix — `ollama pull qwen2.5:7b` **or** `export LLM_ROUTER_ENSEMBLE_PRIMARY=ollama/<installed>` — and append to `issues`. Same for secondary when `allow_secondary` applies. Mirror one line in `verify.py::check_ollama` (127-150). Read-only, non-fatal (heuristic fallback still works).

**Repro/test:** monkeypatch `/api/tags` to omit the primary → warning present; include it → no warning. Cover the `:latest`/bare-name tag-matching edge.

**Out of scope (note in PR):** auto-selecting a substitute at classify time — see §4c.

### PR 6 — #63: statusline provider signal
**Files:** `src/llm_router/hooks/statusline-command.sh` (352-373) (+ test)

1. `providers = any(os.environ.get(k) for k in keys) or os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION","").lower() in ("1","true","yes")` — match doctor's parsing exactly.
2. Split "no provider configured" from "no recent activity": keep the 30-min savings-log scan as an *activity* signal, but when Ollama is the only provider and the window lapsed, probe reachability cheaply (`/api/tags`, ~0.3s timeout, or reuse doctor's circuit-breaker state file) and render **idle** rather than **down**. Only show the outage glyph when there is genuinely no configured provider *and* Ollama is unreachable. This script runs constantly and its #50 history is "python snippet threw" — every new line must be exception-safe (`tests/test_gh50_statusline_defines_every_var.py`).

**Repro/test:** truth table over the embedded snippet — (a) only `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` → "ok"; (b) no keys, last ollama entry 2h old, probe reachable → "idle" (new state); (c) nothing, probe unreachable → "down". Back-date a synthetic `savings_log.jsonl`; no waiting.

### PR 7 — #65: de-collide `LLM_ROUTER_PROFILE` (needs §4d first)
**Files:** `repo_config.py`, `env_registry.py`, docs mentions (+ test)

Assuming the recommended direction (rename the routing side): `repo_config.py:89` reads new `LLM_ROUTER_COST_PROFILE` first; falls back to legacy `LLM_ROUTER_PROFILE` **only if** its value is a routing tier (`budget/balanced/premium/quota_balanced/subscription_local`). Values like `developer`/`enterprise` are ignored with a one-shot deprecation note (mirroring `profile.py:96-110`). Making the two readers mutually exclusive on value domain kills the collision even during deprecation. Update `server.py:261-277`'s workaround comment and `commands/config.py:142`'s label. Register the new var.

**Repro/test:** `LLM_ROUTER_DEPLOYMENT_PROFILE=balanced` alone → `effective_profile()` is None, no crash, identity stays developer; `LLM_ROUTER_COST_PROFILE=premium` → `"premium"`; `LLM_ROUTER_PROFILE=enterprise` → routing ignores it (no crash in `get_config()`), identity still honors it as legacy.

### PR 8 — #66 + #67: docs/skills/naming sweep (after PR 7 so env names are final)
**Files:** `guide/TROUBLESHOOTING.md`, `skills/route/SKILL.md`, `skills/routing/SKILL.md`, `skills/release/SKILL.md`, `docs/BENCHMARKS.md`, `src/llm_router/rules/llm_router.md`, `src/llm_router/rules/llm-router.md` (delete), `.claude-plugin/plugin.json` (or new `.mcp.json`), `.github/copilot-instructions.md`, plus source strings `doctor.py:920` and `install.py:138`.

- **TROUBLESHOOTING.md:** map every pre-consolidation tool name to the 11-tool surface (`llm_health` → `llm_router_status(view=...)` / `llm-router verify`; `llm_setup` → `llm_router_admin` / `llm-router install`; etc. — derive from `tools/consolidated.py` docstrings). Delete `LLM_ROUTER_SQL_DEBUG`/`LLM_ROUTER_HOOK_DEBUG` (427/430). Fix the `model=` FAQ per §4b. Fix hook filenames at 202/306/312 to `llm_router-*.py`; replace line 451's impossible import with a runnable equivalent.
- **`rules/llm_router.md:11,13`:** `llm_router set-enforce off` → `llm-router set-enforce off`; `llm_router doctor` → `llm-router doctor`. Grep the whole rules/skills corpus for `llm_router ` used as a command.
- **`skills/release/SKILL.md:49-54`:** hook destinations to `HOOK_SPECS` names; ~82 `claude-code-llm-router` → `llm-routing`. Same package rename in `docs/BENCHMARKS.md:12`.
- **`skills/route/SKILL.md`:** rewrite the tool table (37-41) and examples (60-69) around `llm(task=..., tier=...)`; `llm_usage` (54) → the real equivalent.
- **Fork resolution:** delete `src/llm_router/rules/llm-router.md` (v5) — `install_hooks.py:57-66` already removes its installed counterpart as a conflicting pre-rebrand artifact; keeping the source fork is how it diverged. Diff v5 vs v9 first and port anything missing. Fix `.github/copilot-instructions.md:1`'s version tag to the underscore form so `install_hooks.py:116` sees it.
- **plugin.json:** commit a real `.mcp.json` or drop the `mcpServers` key — §4e. The one functional change in this PR.

**Repro/test:** a docs-lint test — no file in `guide/`, `skills/`, `src/llm_router/rules/` may match `llm_health|llm_setup|llm_cache_|llm_quality_report|llm_classify\b|llm_policy\b`, `claude-code-llm-router`, `llm-router-[a-z-]+\.py`, or `llm_router ` followed by a known subcommand. Plus: `plugin.json`'s `mcpServers` value, if present, must exist on disk.

**Sequencing:** PR 1 → PR 2 → PR 3 (rebase). PRs 4, 5, 6 in parallel anytime. PR 7 next. PR 8 last (needs §4b/4d/4e and PR 7's final env name). Every PR ships its fresh controlled repro as a test — no closing on DB inspection.

## 4. Design decisions needing a call

**a) #64 — what stops the pin-override, and how far on scoring?**
- (i) *Never skip pinned/overridden models* — recommend unconditional; a pin is an explicit instruction.
- (ii) *Thresholds configurable + kill switch* — recommend yes; currently untunable and undiscoverable.
- (iii) *Fix the heuristic bias itself* — terse-correct QUERY answers score ~0.3 because they can't earn length/structure bonuses, yet `TaskType.QUERY` has `min_output_length=1`. Options: exempt QUERY/simple from skipping, or normalize so the reachable max per task type is 1.0. This changes learned-quality behavior globally. Recommendation: exempt `query`+`simple` from *skipping* only, leave scoring untouched for escalation.

**b) #64/#66 — re-add `model=` to consolidated `llm()`?** `llm_query` supports it internally; North Star 1.0 dropped it (`consolidated.py:59`). For: restores the documented escape hatch, makes the override exemption reachable. Against: re-opens a routing bypass the consolidation deliberately removed. The #66 FAQ text depends on this. Lean: re-add (power-user escape hatch that already exists one layer down) — product call.

**c) #62 — warn only, or auto-adapt?** Plan is warn-only. Alternative: at classify time, auto-select an installed instruct model when the primary is absent, log once. More magic, less silent degradation. Recommendation: warn-only now, auto-adapt as a follow-up issue.

**d) #65 — which side renames, and the shim.** Recommendation: routing side takes the new name (`LLM_ROUTER_COST_PROFILE`) — the identity side already completed its rename, and re-renaming it burns users who just followed the deprecation warning. Shim: value-domain-filtered legacy fallback + one-shot warnings; remove legacy reads in 14.0. Alternative: deprecate and delete `effective_profile()` (only one caller) — smallest change, but leaves cost profile unconfigurable without routing.yaml.

**e) #66 — `.mcp.json` vs dropping `mcpServers`.** Committing a real `.mcp.json` makes `/plugin install` work (likely the original intent); dropping the key makes plugin.json honest but the plugin skill-only. Needs a call on whether the Claude Code plugin is a supported install path. If yes, `.mcp.json` should launch the same stdio entry `commands/install.py` configures elsewhere.

**f) #61 — should no-args-in-a-TTY still start the server?** The strict fix only errors on unrecognized subcommands. Optional extra: when `args` is empty **and** stdin is a TTY, print a hint to stderr before starting. Low cost, prevents adjacent confusion.

## 5. Regression-test gaps

| Issue | Test that would have caught it | Keep permanently? |
|---|---|---|
| #59 | Session-scoping test setting **only** `CLAUDE_CODE_SESSION_ID` — the gh49 test evidently set `CLAUDE_SESSION_ID` and passed against the wrong tier. | **Yes** — parametrize gh49 over both vars + a "neither set → global + honest message" case. Guards the #49→#59 regression class. |
| #60 | End-to-end "one routed call ⇒ one `routing_decisions` row" through the **`llm()`/`llm_query`** surface, not `llm_route`. The 13.0.4 fix was validated only on the path that passes `classification_data`. | **Yes** — the single most valuable test here. Keep the `accuracy=None` renderer tests too. |
| #61 | Subprocess smoke test: unknown subcommand exits non-zero fast, no MCP startup output. | **Yes** — trivial, and pins the no-args contract. |
| #62 | Doctor test asserting a warning when `_primary_model()` ∉ mocked `/api/tags`. General gap: doctor asserts health of a component doctor never checks. | **Yes**, with PR 5. Optional broader meta-test: every hardcoded `ollama/*` default in `src/` appears in doctor's checked set. |
| #63 | Table-driven test over the health snippet: (env, log age, probe) → expected state. The gh50 test checks "doesn't crash", not "says something true". | **Yes** — two issues deep (#50, #63); it has earned a truth-table test. |
| #64 | (1) "Pinned model is always attempted" with a poisoned `_quality_store`; (2) "any excluded candidate appears in the trace". | **Yes** to both. (2) is load-bearing: *every candidate exclusion must leave a visible trace* — its absence is what made this a multi-day hunt. |
| #65 | Collision test: `LLM_ROUTER_PROFILE=enterprise` doesn't break routing; deployment var alone still resolves the routing profile. `server.py:265`'s comment shows the collision was known — a test would have forced a fix instead of a workaround. | **Yes**, small; delete with the legacy reads in 14.0. |
| #66 | Docs-vs-surface consistency test: extract tool names from the registered surface, grep docs/skills for retired names; assert documented env vars exist in `env_registry.py`. | **Yes** — the consolidation was a mass rename and docs silently lagged. |
| #67 | Same lint extended with naming patterns + "plugin.json referenced files exist" + "all rules version tags match `install_hooks.py:116`". | **Yes** for the lint (mechanical, zero-flake). The `llmr` CLI rename: out of scope, park as a discussion issue. |
