# LLM Router Router Audit — Telemetry / Installer / Concurrency (Sections 6, 8, 9)

Scope: Section 6 (cost & telemetry accuracy), Section 8 (installer/doctor
self-heal extended to other hosts), Section 9 (concurrency / determinism).

Test files (all under `tests/audit/`, created fresh — no existing files
modified):

- `test_cost_telemetry.py` — Section 6
- `test_installer_other_hosts.py` — Section 8
- `test_concurrency_determinism.py` — Section 9

`tests/commands/test_doctor.py` and `tests/test_codex_gateway_install.py`
were **not** modified — no gap was found in either that needed an
additive case for these sections (see "gaps / assumptions").

Other files present in `tests/audit/` (`test_chain_correctness.py`,
`test_config_edge_cases.py`, `test_execution_variety.py`,
`test_failure_fallback.py`, `test_policy_switching.py`,
`test_provider_matrix.py`) belong to a different, parallel audit agent
covering different sections — not reported on here.

## Results — one line per check

### Section 6 — Cost & telemetry accuracy (`test_cost_telemetry.py`, 8 tests, 7 pass / 1 fail)

- [PASS] `test_log_usage_reflects_actually_executed_model_not_failed_candidate` — verified `cost.log_usage` is called exactly once per turn, with the model/provider/cost of the model that actually succeeded, never the earlier failed candidate.
- [PASS] `test_usage_db_row_matches_successful_model_after_chain_fallback` — verified the persisted `usage` table row (real SQLite read-back) names the successful model after a fallback chain walk.
- [PASS] `test_tok_axis_unit_locks_whole_axis_to_one_scale` — verified `_tok_axis_unit` picks one (divisor, suffix) pair from the axis max (3.2M→M, 900→raw, 450.7k→k).
- [PASS] `test_fmt_tok_axis_all_ticks_share_one_suffix_across_wide_range` — verified a tick set spanning 0 to 3.2M renders with a single shared suffix, no "M next to k" mixing.
- [PASS] `test_fmt_tok_axis_small_range_never_introduces_k_or_m` — verified an all-under-1000 axis renders raw digits only.
- [PASS] `test_quality_report_by_model_excludes_never_recorded_models` — verified `cost.get_quality_report`'s `by_model` breakdown contains exactly the 2 logged models and no phantom third model, with total cost matching exactly.
- [PASS] `test_usage_summary_total_cost_matches_only_recorded_rows` — verified `cost.get_usage_summary` lifetime total equals the sum of only the recorded rows.
- [FAIL — intentional, documents a real bug] `test_savings_analytics_reports_real_decisions_not_zero` — encodes the CORRECT expectation (one recorded `routing_decisions` row → `SavingsAnalytics.compute_savings()` reports `total_decisions == 1`); fails against current code because it reports `0`. See "bugs found" below.

### Section 8 — Installer/doctor self-heal, other hosts (`test_installer_other_hosts.py`, 16 tests, 16 pass)

- [PASS] OpenCode (`_install_opencode_files`) — writes only `{"mcpServers": {"llm_router": {...}}}`, no forced-default keys; idempotent on re-run.
- [PASS] Gemini CLI (`_install_gemini_cli_files`) — `settings.json` has only `mcpServers`; extension manifest has only `name/version/description/mcpServers`; `hooks.json` carries lifecycle-event registrations, no model/provider scalar.
- [PASS] Copilot CLI (`_install_copilot_cli_files`) — `mcp.json` has only `mcpServers`.
- [PASS] OpenClaw (`_install_openclaw_files`) — `mcp.json` has only `mcpServers`.
- [PASS] Trae IDE (`_install_trae_files`) — platform-specific `mcp.json` has only `mcpServers`.
- [PASS] Factory Droid (`_install_factory_files`) — writes **no files at all** under HOME; nothing to force a default with.
- [PASS] VS Code (`_install_vscode_files`) — `mcp.json` has only `servers` (not `mcpServers`), no forced-default keys.
- [PASS] Cursor IDE (`_install_cursor_files`) — `mcp.json` has only `mcpServers`; rules file is plain markdown prose, not machine-parsed config.
- [PASS] Cross-host generic sweep (parametrized, 4 hosts) — no top-level `model`/`model_provider`/`default*`/`active*` scalar key in any written JSON.
- [PASS] `test_no_other_host_writes_any_toml_config` — confirmed no non-Codex installer writes any `.toml` file (TOML top-level scalars were the exact shape of the Codex bug).

### Section 9 — Concurrency / determinism (`test_concurrency_determinism.py`, 2 tests, 2 pass)

- [PASS] `test_concurrent_calls_cannot_exceed_monthly_budget_cap` — fired 10 concurrent `route_and_call` turns (each provider call artificially slowed 150ms to force overlap during the check-then-reserve window) against a monthly budget sized to admit exactly 3; confirmed exactly 3 succeeded, 7 got `BudgetExceededError`, and `_pending_spend` returned to exactly 0.0 after all settled (no reservation leak).
- [PASS] `test_build_and_filter_chain_deterministic_across_repeated_calls` — called `_build_and_filter_chain` 5x with identical (task_type=CODE, profile=BALANCED, complexity=MODERATE, config) inputs; all 5 returned byte-identical ordered chains.

## Bugs found

1. **`llm_router/commands/gain.py:100-115`** (`SavingsAnalytics.get_routing_decisions`) — the SQL query selects columns `original_tool`, `selected_model`, `budget_pct_used`, `estimated_cost_usd`, `session_id`, `timestamp` from the `routing_decisions` table. None of `original_tool`, `selected_model`, `estimated_cost_usd` exist in the real schema — see `llm_router/cost.py:63-88` (`CREATE_ROUTING_DECISIONS_TABLE`, whose real columns are `recommended_model`/`final_model`/`final_provider`/`cost_usd`, etc.) and `llm_router/cost.py:1046-1148` (`log_routing_decision`, the only writer). `original_tool`/`selected_model`/`estimated_cost_usd` belong to the unrelated `corrections` table shape (`llm_router/cost.py:294-311`), suggesting a copy-paste-from-the-wrong-table origin. Every call to `get_routing_decisions()` therefore raises `sqlite3.OperationalError: no such column`, silently caught by `except sqlite3.Error: return []` at `gain.py:119-120`. Net effect: **the `llm_savings` MCP tool (`llm_router/tools/admin.py:1389`, backed by `show_gain()`) unconditionally reports zero decisions and zero savings, no matter how much real routing has happened.** This is a severe, silent telemetry-accuracy failure — worse than "phantom model Z's cost leaking in," it's "100% of real recorded cost is invisible to the user-facing savings dashboard." Reproduced by `tests/audit/test_cost_telemetry.py::test_savings_analytics_reports_real_decisions_not_zero` (left failing, not fixed, per instructions).

2. **Section 8 — no bugs found.** Every other-host installer function in `llm_router/commands/install.py` (`_install_opencode_files:658`, `_install_gemini_cli_files:683`, `_install_copilot_cli_files:808`, `_install_openclaw_files:830`, `_install_trae_files:846`, `_install_factory_files:873`, `_install_vscode_files:888`, `_install_cursor_files:915`) was read in full and confirmed to only perform additive MCP-server registration (via `_merge_json_mcp_block`, which no-ops if the entry already exists and never touches any other top-level key) or append prose instructions to a markdown file. None write TOML, and none write a top-level `model`/`model_provider`/`default*`/`active*` scalar. The Codex bug (already fixed) was structurally unique: Codex's `config.toml` has top-level scalars its own CLI client reads unconditionally on every call, with no equivalent "always-read default" surface in any of the other hosts' config formats.

3. **Section 9 — no bugs found.** The `_budget_lock()`/`_pending_spend` guard (`llm_router/router.py:742-758`) correctly serializes the check-then-reserve sequence; a 10-way concurrent burst against a budget sized for 3 admitted exactly 3. `_build_and_filter_chain` (`llm_router/router.py:224-618`) produced identical output across 5 repeated same-process calls with identical inputs.

## Gaps / assumptions

- **Section 9 determinism — hash-seed variant not completed.** The task asks for 5x same-process repetition, which is what's implemented and passes. A stronger cross-`PYTHONHASHSEED` subprocess variant (to catch non-determinism invisible within one process, since CPython randomizes `str`/`set` iteration order per-process) was attempted and removed: it hung for several minutes in this environment. Root cause appears to be that a bare subprocess re-importing `llm_router.config`/`llm_router.codex_agent`/`llm_router.gemini_cli_agent` from scratch re-triggers real environment probing — in particular `effective_ollama_base_url` (`llm_router/config.py:375-388`) only skips the `http://localhost:11434` default probe when `PYTEST_CURRENT_TEST` is set, which a bare subprocess script doesn't have. As a result, **the hash-seed-driven non-determinism risk in `list({*a, *b})`-style dedup patterns (e.g. `llm_router/router.py:357-359`, merging org-policy and repo-config block/allow-model sets into a list) remains unverified** — not confirmed safe, not confirmed broken. By inspection, those particular merged lists are only used for set-style membership filtering downstream (`llm_router/policy.py:484-528`), not for output ordering, so they likely don't affect final chain order even if their own internal order is hash-seed-dependent — but this was verified by code reading, not by the stronger test.
- **Real ambient dev-machine state leaked into early test iterations.** This machine has a real `~/.llm-router/.env` (sets `OLLAMA_BASE_URL`) and a real `~/.llm-router/routing.yaml` (pins `query` → `ollama/qwen2.5-coder:7b`), and a live Ollama daemon. Early versions of the Section 6 `route_and_call`-based tests silently picked these up (per the documented "`mock_env` does not isolate HOME" gap) and had to be hardened with explicit `pathlib.Path.home` patching, explicit (not `delenv`) `OLLAMA_BASE_URL`/`OLLAMA_BUDGET_MODELS` overrides, and a same-process `all_ollama_models` monkeypatch, because `llm_router.config`'s dotenv path and other modules' `Path.home()`-derived constants (`llm_router/session_spend.py:41`, `llm_router/receipt_store.py:25`, `llm_router/budget.py:44-45`) are resolved at **import time**, not call time, so patching `Path.home()` inside a test cannot retarget them — only `llm_router/repo_config.py`'s loader calls `Path.home()` fresh per call and is reliably patchable this way. This is a real, reproducible confirmation of the flagged HOME-isolation gap and a trap for any future test in this repo that exercises `route_and_call` end-to-end.
- **Doctor coverage gap, not a bug.** `llm_router doctor --host` only supports `claude`/`vscode`/`cursor`/`codex` (`llm_router/commands/doctor.py:102-107`); it has no health-check branch at all for opencode/gemini-cli/copilot-cli/openclaw/trae/factory. Since Section 8 found no forced-default bug for any of those hosts to catch, there was nothing to add a regression test for in `tests/commands/test_doctor.py`, so it was left unmodified per the instructions (only edit if a real gap needing a case was found). Flagging the coverage gap itself for awareness only.
- Section 6 bullet 1 was verified via direct mock of the dispatch loop's success path (failed-then-succeeded two-model chain) rather than reaching into `_dispatch_model_loop` internals directly, since it's a private closure-heavy function; `route_and_call` end-to-end was judged the more faithful and maintainable test surface.
