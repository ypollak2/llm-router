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

**Outcome**: Per-task budgeting now enforced. Router automatically deprioritizes low-quality models based on historical judge scores. TROUBLESHOOTING.md provides users with actionable solutions for all common issues. Option B complete. Next: Option D (documentation refresh), Option A (silent failure fixes), Option C (test coverage).
