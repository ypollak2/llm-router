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
