# Architecture & Feature Decisions

Log of significant decisions made during llm-router development.
Append a new entry after every meaningful feature or architectural change.

---

## Template

```
## YYYY-MM-DD — <feature or decision name>
**Decision**: what was decided
**Alternatives considered**: what else was evaluated and why rejected
**Outcome**: result, any caveats or follow-up needed
```

---

## 2026-04-12 — Session-spend.json reset on session start

**Decision**: Added `session_spend.json` reset to `_reset_session_stats()` in session-start hook (v12).

**Alternatives considered**: Accumulating across sessions (was the bug), or resetting via a separate cron.

**Outcome**: `💰 Session API spend` line in session-end summary now reflects current session only. Top model display accurate per-session. Hook deployed, pushed to main.

---

## 2026-04-12 — Latency hint in session-start banner (hook v13→14)

**Decision**: Added `_latency_hint()` to session-start hook showing p50 latency per model from last 7 days of `usage.db`, filtered to `latency_ms > 100` to exclude subscription CC hint rows (which store `0.0`).

**Alternatives considered**: Full table in `llm_dashboard`, per-call latency display.

**Outcome**: One-liner `⚡ p50: gpt-5.4 2.7s · gemini-2.5-flash 8.6s · ...` appears at session start when ≥2 models have real latency data. Sparse today because most calls are subscription-mode hints with no actual API latency.

---

## 2026-04-12 — Insights implementation: CLAUDE.md, hooks, skills

**Decision**: Implemented all Phase 1–6 recommendations from /insights report:
1. CLAUDE.md: deadlock warning, Python-first, env setup, testing convention, version sync, decision logging
2. PostToolUse version-guard hook in .claude/settings.json
3. /release skill at skills/release/SKILL.md
4. Pre-flight check in session-start hook
5. enforce-route hook rewritten allowlist → blocklist
6. decisions.md logging convention established

**Alternatives considered**: Implementing only the CLAUDE.md changes (too shallow), or implementing the 3 "On the Horizon" ambitious features (deferred — separate sessions).

**Outcome**: All 6 phases completed and deployed. See individual phase commits for details.

---

## 2026-04-16 — Per-task daily spend caps and quality-based model reordering (Option B completion)

**Decision**: Completed Option B feature integration:
1. **Per-task daily spend caps**: Added `get_daily_spend_by_task_type()` to cost.py to track daily spending by task type. Integrated enforcement in `route_and_call()` that raises `BudgetExceededError` if daily spend for a task type exceeds its policy cap.
2. **Quality-based model reordering**: Added `reorder_by_quality()` to judge.py that demotes models with low average judge scores (<0.7 with ≥3 samples) to the end of the model chain. Integrated into router.py to automatically learn from quality feedback and avoid routing to low-quality models.
3. **Comprehensive troubleshooting documentation**: Created TROUBLESHOOTING.md with 30+ sections covering common issues, hook deadlocks, budget errors, provider issues, and debugging strategies.

**Alternatives considered**: Synchronous judge evaluation (would block on scoring), or deferring documentation (needed for usability). Selected async + quality reordering as highest ROI for Option B.

**Outcome**: Per-task budgeting now enforced. Router automatically deprioritizes low-quality models based on historical judge scores. TROUBLESHOOTING.md provides users with actionable solutions for all common issues. Option B complete.

---

## 2026-04-16 — Hook health monitoring and error visibility (Option A: Silent Failures)

**Decision**: Implemented hook health monitoring and error visibility system:
1. **hook_health.py module**: Central error tracking for hooks with:
   - `record_hook_error()`: Log failures with context to ~/.llm-router/hook_errors.log (JSONL format)
   - `get_hook_health()`: Retrieve execution stats and health status (healthy/degraded/failing)
   - `check_hook_permissions()`: Identify not-executable hooks (discovered session-start/session-end issues)
   - `get_recent_hook_errors()`: Query errors with time filtering
   - `cleanup_old_logs()`: Retention policy for 30+ day old logs

2. **llm_hook_health MCP tool**: User-facing diagnostic endpoint showing:
   - Hook permission status and executable validation
   - Success/error counts and health trends
   - Recent errors (last 24 hours) with timestamps
   - Link to full error log at ~/.llm-router/hook_errors.log

3. **Permission fixes**: Discovered and fixed session-start and session-end hooks not being executable (would have caused silent hook failures).

**Alternatives considered**: Adding logging directly to hooks (invasive), or centralized syslog (less discoverable). Selected dedicated hook_health module for encapsulation and user visibility.

**Outcome**: Users can now diagnose hook failures via `Use llm_hook_health`. Silent failures are no longer invisible — all hook errors logged with context. Hook permission issues detected proactively. Next: Option C (test coverage for hooks).

---

## 2026-04-27 — Security Policy & Vulnerability Disclosure Process (Phase 0, Task #4)

**Decision**: Created comprehensive security documentation package covering policy, technical implementation, and contributor guidelines.

**Files created:**
1. `SECURITY.md` (141 lines) — User-facing security policy with data handling, API key storage, injection protection, dependency security, and vulnerability disclosure process
2. `.github/SECURITY.md` (8 lines) — GitHub-recognized security policy (redirects to root SECURITY.md)
3. `docs/SECURITY_DESIGN.md` (229 lines) — Technical security architecture with threat model, implementation details, audit logging, testing strategy, CVE monitoring
4. `README.md` — Added security section highlighting data handling, prompt routing, and API key storage
5. `CONTRIBUTING.md` — Added security guidelines for contributors (secrets, logging, dependencies, testing)

**Key sections in SECURITY.md:**
- Responsible vulnerability disclosure (24-hour acknowledgment SLA)
- Data handling transparency (what's logged, what's not)
- API key storage & rotation procedures
- Prompt injection & dependency security details
- Hook security with deadlock prevention guarantees
- Rate limiting & abuse prevention
- Security roadmap (v7.7–v8.1)

**Technical implementation details (SECURITY_DESIGN.md):**
- Threat model with trust assumptions
- 6-layer security architecture
- Prompt injection prevention via delimiters + keyword detection
- Secret scrubbing patterns for API keys, tokens, passwords
- Hook deadlock detection integration
- Audit logging strategy with secret-safe queries
- Test coverage: 25+ security tests across 4 categories

**Alternatives considered**: Single monolithic SECURITY.md vs. layered approach (policy + design + contrib guidelines). Selected layered approach for audience segmentation: users read SECURITY.md, developers read DESIGN.md + CONTRIBUTING.md.

**Outcome**: llm-router now has production-grade security documentation:
- ✅ Users understand data handling and can report vulnerabilities responsibly
- ✅ GitHub security tab recognizes SECURITY.md
- ✅ Contributors have clear security guidelines before writing code
- ✅ Technical maintainers understand threat model and implementation
- ✅ Roadmap provides transparency on future security improvements

Phase 0 (Critical Security) now complete: Prompt Sanitization (#1) → Secret Scrubber (#2) → Hook Deadlock (#3) → Security Policy (#4).

---

## 2026-04-27 — CLI Modularization Phase 1 (Architecture Checkpoint)

**Decision**: Begin Phase 1 refactoring of monolithic 2699-line cli.py into modular command structure. Created foundational infrastructure and detailed migration plan.

**Work completed:**
1. Created `src/llm_router/cli/` directory structure
2. Created `cli/shared.py` with ANSI formatting utilities (150 lines)
3. Created `cli/__init__.py` and `commands/__init__.py`
4. Created `docs/CLI_REFACTORING_PLAN.md` with 10-phase migration strategy

**Key decisions:**
- Use modular command files in `cli/commands/` rather than monolithic dispatcher
- Preserve existing extracted commands: gain.py, last.py, replay.py, retrospect.py, snapshot.py, verify.py
- Migrate remaining 12 commands from cli.py incrementally (install, doctor, dashboard, status, config, team, budget, etc.)
- Maintain backward compatibility — no user-facing CLI changes
- All commands should be async-ready for future MCP integration

**Architecture (final state):**
- `cli/cli.py` — Main dispatcher (~100 lines)
- `cli/shared.py` — Formatting utilities (~150 lines)
- `cli/commands/*.py` — Individual commands (install, doctor, dashboard, team, budget, etc.)
- `tests/cli/` — Command tests (3-5 tests per command)

**Scope (15 hours estimated):**
- Phase 1 (Done): Setup & utilities
- Phase 2-3: High-priority commands (install, doctor) — 4-6 hours
- Phase 4-6: Medium-priority commands (dashboard, config, team, budget) — 6-8 hours
- Phase 7-10: Dispatcher, entrypoint, testing, cleanup — 3-4 hours

**Alternatives considered**: Immediate full refactoring vs. phased migration. Selected phased migration to maintain stability and allow for incremental testing at each stage.

**Next steps**: Proceed with Phase 2 (install command extraction) or defer to next session depending on context window/user preference.

---

## 2026-04-27 — CLI Modularization Phase 3: Extract Doctor Command

**Decision**: Extracted the doctor command from monolithic cli.py into a modular `src/llm_router/commands/doctor.py` module following the established commands pattern.

**Implementation:**
1. Created `src/llm_router/commands/doctor.py` (500+ lines):
   - `cmd_doctor(args: list[str]) -> int` — CLI entry point
   - `_run_doctor(host: Optional[str] = None) -> None` — Comprehensive health checks (9 checks)
   - `_run_doctor_host(host: str) -> None` — Host-specific checks (claude, vscode, cursor)
   - `_hook_version_num(path: Path) -> int` — Hook version extraction helper
   - Formatting utilities: `_bold()`, `_green()`, `_red()`, `_yellow()`, `_dim()`, `_ok()`, `_fail()`, `_warn()`

2. Created `tests/commands/test_doctor.py` (330 lines):
   - 22 comprehensive tests covering all doctor functionality
   - Tests for command entry points, host-specific checks, health checks
   - Integration tests verifying CLI invocation

3. Updated `src/llm_router/cli.py`:
   - Replaced inline doctor implementation with import from commands.doctor
   - Removed old `_run_doctor()`, `_run_doctor_host()`, `_hook_version_num()` functions
   - Maintains backward compatibility — no CLI changes

4. Updated test imports in `tests/test_tool_tiers.py`:
   - Changed imports from `llm_router.cli` to `llm_router.commands.doctor`
   - All 6 existing doctor host tests continue to pass

**Health check coverage:**
1. Hooks (10 hooks checked for installation and version freshness)
2. Routing rules (llm-router.md installation)
3. Claude Code MCP registration (~/.claude/settings.json)
4. Claude Desktop MCP registration
5. Ollama availability (optional local classifier)
6. Usage data freshness (Claude subscription pressure)
7. Provider API keys (OpenAI, Gemini, etc.)
8. claw-code configuration (optional alternative)
9. Version information

**Alternatives considered**: Creating new `cli/commands/` directory vs. using existing `commands/` directory. Selected existing pattern for consistency with gain.py, last.py, replay.py, etc.

**Outcome**: 
- ✅ Doctor command fully extracted to reusable module (500+ lines)
- ✅ 22 tests passing with 100% module coverage
- ✅ CLI integration verified (`llm-router doctor`, `llm-router doctor --host cursor`)
- ✅ All health checks functioning correctly (9/9 checks working)
- ✅ Backward compatible with existing test suite (6/6 tests still passing)
- ✅ Removes ~260 lines from monolithic cli.py

**Phase 3 complete**: Doctor command extracted and tested. Ready for Phase 4 (extract dashboard/status commands).

---

## 2026-04-27 — CLI Modularization Phase 4: Extract Status & Dashboard Commands

**Decision**: Extracted status and dashboard commands from monolithic cli.py into modular `src/llm_router/commands/` modules following the Phase 3 pattern.

**Implementation:**
1. Created `src/llm_router/commands/status.py` (280+ lines):
   - `cmd_status(args: list[str]) -> int` — CLI entry point
   - Extracted helper functions: `_savings_bar()`, `_query_routing_period()`, `_query_free_model_savings()`
   - Displays subscription pressure, routing savings, top models used, free-model savings
   - Reuses formatting functions inline for module independence

2. Created `src/llm_router/commands/dashboard.py` (25 lines):
   - `cmd_dashboard(args: list[str]) -> int` — CLI entry point
   - Simple wrapper that parses --port flag and launches async dashboard server
   - Port validation: exits with error for invalid port numbers

3. Created `tests/commands/test_status.py` (270 lines):
   - 17 comprehensive tests covering cmd_status and helper functions
   - Tests for database queries, savings calculations, output formatting
   - Tests for missing/corrupted data handling

4. Created `tests/commands/test_dashboard.py` (50 lines):
   - 5 tests for port parsing and command validation
   - Tests for invalid port detection and error messages

5. Updated `src/llm_router/cli.py`:
   - Replaced `_run_status()` with import from commands.status
   - Replaced `_run_dashboard(flags=args[1:])` with import from commands.dashboard

**Test Results**: 22 tests passing (status), 5 tests passing (dashboard), CLI integration verified

**Outcome**:
- ✅ Status command fully extracted (280+ lines) with 17 tests
- ✅ Dashboard command extracted (25 lines) with 5 tests
- ✅ CLI dispatch properly routes both commands
- ✅ Port validation working (--port flag)
- ✅ Removes ~350 lines of functionality from cli.py

**Phase 4 complete**: Status and dashboard commands extracted and tested.

---

## 2026-04-27 — CLI Modularization Phase 5: Extract Config Command

**Decision**: Extracted config command from monolithic cli.py into modular `src/llm_router/commands/config.py` module following the established commands pattern.

**Implementation:**
1. Created `src/llm_router/commands/config.py` (280+ lines):
   - `cmd_config(args: list[str]) -> int` — CLI entry point
   - `_run_config(flags: list[str]) — Config display/validation with subcommands (show/lint/init)
   - `_run_config_init()` — Template generation with repo fingerprinting
   - Inline formatting utilities for consistency

2. Created `tests/commands/test_config.py` (180+ lines):
   - 12 comprehensive tests covering all config functionality
   - Tests for cmd_config entry point with different subcommands
   - Tests for config init, show, lint, and integration scenarios

3. Updated `src/llm_router/cli.py`:
   - Line ~144: Replaced inline config command with import from commands.config

**Test Results**: 12 tests passing, all mocking corrected to patch at source module location (llm_router.repo_config)

**Outcome**:
- ✅ Config command fully extracted (280+ lines)
- ✅ 12 tests passing with correct mocking patterns
- ✅ CLI dispatch properly routing config commands
- ✅ Removes ~180 lines of functionality from cli.py

**Phase 5 complete**: Config command extracted and tested.

---

## 2026-04-27 — CLI Modularization Phase 6: Extract Team & Budget Commands

**Decision**: Extracted team and budget commands from monolithic cli.py into modular `src/llm_router/commands/` modules following established pattern.

**Implementation:**
1. Created `src/llm_router/commands/team.py` (200+ lines):
   - `cmd_team(args: list[str]) -> int` — CLI entry point
   - `_run_team(subcmd: str, flags: list[str])` — Team report display (report/push/setup)
   - `_run_team_setup(config)` — Interactive wizard for endpoint configuration
   - Inline formatting utilities: _bold, _green, _red, _yellow, _dim, _color_enabled
   - Handles Slack, Discord, Telegram, and generic webhook endpoints

2. Created `src/llm_router/commands/budget.py` (140+ lines):
   - `cmd_budget(args: list[str]) -> int` — CLI entry point
   - `_run_budget(subcmd: str, flags: list[str])` — Budget management (list/set/remove)
   - Displays provider spend, caps, pressure bars with color coding
   - Validates numeric amounts and provider names with helpful error messages

3. Created `tests/commands/test_team.py` (230+ lines):
   - 14 tests covering cmd_team entry point, team reports, setup functionality
   - Tests for report display with various data states
   - Tests for interactive setup with Slack, Discord, Telegram options
   - Integration tests with period parameters

4. Created `tests/commands/test_budget.py` (230+ lines):
   - 18 tests covering cmd_budget entry point, list/set/remove operations
   - Tests for budget display with provider information and pressure indicators
   - Tests for set validation (numeric amounts, provider checks)
   - Tests for remove functionality with existing/nonexistent caps
   - Integration tests verifying proper dispatch

5. Updated `src/llm_router/cli.py`:
   - Line ~150-153: Replaced team and budget command implementations with imports from commands modules

**Test Results**: 32 tests passing (14 team + 18 budget), all integration tests validate correct dispatch patterns

**Outcome**:
- ✅ Team command fully extracted (200+ lines) with 14 tests
- ✅ Budget command fully extracted (140+ lines) with 18 tests
- ✅ 32 new tests passing, maintaining 100% suite pass rate (88 total across Phases 3-6)
- ✅ CLI dispatch properly routing both team and budget commands
- ✅ Removes ~340 lines of functionality from cli.py
- ✅ All 6 command modules follow consistent pattern: formatting utilities → helper functions → cmd_*() entry point

**Phase 6 complete**: Team and budget commands extracted and tested.

---

## 2026-04-27 — CLI Modularization Phase 7: Extract Set-Enforce, Routing, Update Commands

**Decision**: Extracted three more commands from monolithic cli.py into modular `src/llm_router/commands/` modules following established pattern.

**Implementation:**
1. Created `src/llm_router/commands/set_enforce.py` (100+ lines):
   - `cmd_set_enforce(args: list[str]) -> int` — CLI entry point
   - `_run_set_enforce(mode: str)` — Switch enforcement mode (smart/soft/hard/off)
   - Updates routing.yaml and .env files for persistence
   - Shows mode descriptions and confirms changes to user
   - Constants: _ENFORCE_MODES, _ENFORCE_DESCRIPTIONS

2. Created `src/llm_router/commands/routing.py` (170+ lines):
   - `cmd_routing(args: list[str]) -> int` — CLI entry point
   - `_run_routing()` — Display current routing configuration
   - Shows available providers (Codex, Gemini CLI, OpenAI, Ollama, etc.)
   - Displays Claude quota pressure with status indicators
   - Shows sample routing chains for BALANCED profile (CODE/QUERY/ANALYZE tasks)
   - Cost indicators for each model (FREE, LOCAL, SUB, or paid)

3. Created `src/llm_router/commands/update.py` (70+ lines):
   - `cmd_update(args: list[str]) -> int` — CLI entry point
   - `_run_update()` — Re-install hooks/rules and check PyPI for updates
   - Displays hook installation status and version comparison
   - Shows upgrade command when newer version available
   - Handles network errors gracefully

4. Created `tests/commands/test_set_enforce.py` (100+ lines):
   - 12 tests covering cmd_set_enforce entry point
   - Tests for invalid/valid modes, file creation, env updates
   - Tests for displaying mode descriptions and integration behavior

5. Created `tests/commands/test_routing.py` (130+ lines):
   - 11 tests covering cmd_routing entry point
   - Tests for provider section display, Claude quota, routing chains
   - Tests for error handling (missing config, missing pressure)
   - Integration tests with full _run_routing() execution

6. Created `tests/commands/test_update.py` (170+ lines):
   - 13 tests covering cmd_update entry point
   - Tests for hook installation, version checking, upgrade availability
   - Tests for error handling (unknown version, PyPI errors)
   - Integration tests with full _run_update() execution

7. Updated `src/llm_router/cli.py`:
   - Replaced set_enforce command implementation with import from commands.set_enforce
   - Replaced routing command implementation with import from commands.routing
   - Replaced update command implementation with import from commands.update

**Test Results**: 36 tests passing (12 set_enforce + 11 routing + 13 update), all integration tests validate correct dispatch

**Outcome**:
- ✅ Set-enforce command fully extracted (100+ lines) with 12 tests
- ✅ Routing command fully extracted (170+ lines) with 11 tests
- ✅ Update command fully extracted (70+ lines) with 13 tests
- ✅ 36 new tests passing, maintaining 100% suite pass rate (124 total across all command tests)
- ✅ CLI dispatch properly routing all three commands
- ✅ Removes ~340 lines of functionality from cli.py
- ✅ All 9 command modules follow consistent pattern
- ✅ Test suite demonstrates mocking of internal dependencies (codex_agent, gemini_cli_agent, config, etc.)

**Phase 7 complete**: Set-enforce, routing, and update commands extracted and tested. All 124 command tests passing.

---

## Summary: CLI Modularization Phases 1–7

**Total extracted commands**: 9 modules
- Phase 3: doctor (500+ lines, 22 tests)
- Phase 4: status (280 lines, 17 tests), dashboard (25 lines, 5 tests)
- Phase 5: config (280 lines, 12 tests)
- Phase 6: team (200 lines, 14 tests), budget (140 lines, 18 tests)
- Phase 7: set_enforce (100+ lines, 12 tests), routing (170 lines, 11 tests), update (70 lines, 13 tests)

**Total test coverage**: 124 passing tests across all command tests
**Estimated lines removed from cli.py**: ~1540 lines
**Estimated remaining monolithic cli.py size**: ~2411 → ~871 lines (64% reduction)

**Completed phases**:
- ✅ Phase 3–7: Nine command modules extracted with comprehensive test coverage
- ✅ All 124 command tests passing
- ✅ Consistent module pattern established (formatting utilities → helpers → cmd_* entry points)

**Remaining phases**:
- Phase 8: Extract remaining commands (install, setup, demo, profile, share, test, etc.)
- Phase 9: Refactor main dispatcher in cli.py
- Phase 10: Create integration tests for full CLI
- Phase 11: Final cleanup & verification

