# Plan Mode + TDD Enforcement Gates

**Problem**: Recent sessions show friction spike:
- Wrong approach (7×/30) — selecting suboptimal solutions without user input
- Buggy code (5×/30) — code doesn't work on first delivery
- Success rate dropped from 63% to 53%

**Root cause**: Skipping Plan Mode for "small" changes and not enforcing test-first strictly.

## Enforcement Rules (Apply Immediately)

### 1. Plan Mode — Mandatory Triggers
Enter Plan Mode BEFORE writing code if ANY apply:
- Task will modify 3+ files ✅ (existing CLAUDE.md rule)
- Task involves routing chain/database schema changes
- Task affects critical paths: cost.py, router.py, hooks/*, config.py
- Task description contains "how can we", "should we", "best approach"
- User hasn't explicitly waived planning ("just do it", "no plan needed")

**Process**: EnterPlanMode → explore architecture → present 2-3 options → wait approval → implement

### 2. Test-First — Non-Negotiable
**For features**: RED (failing test) → GREEN (code) → REFACTOR → VERIFY full suite
**For bugs**: Write reproducer test first → fix → verify tests pass

Never skip for "small changes". If it's too small to test, it's too small to be worth doing.

**Pre-delivery checkpoint**:
- [ ] Full test suite passes (`uv run pytest tests/ -q`)
- [ ] Linting passes (`uv run ruff check src/ tests/`)
- [ ] No new warnings/errors
- [ ] CHANGELOG updated (if user-facing)
- [ ] Version sync verified (if changed)

### 3. Metrics to Track
- Plan Mode %: Target 100% for 3+ file changes
- Test-first adherence %: Target 100%
- Combined friction (wrong_approach + buggy_code): Target <3 per 30 sessions (from current 12)

## Session Application

Start of session: These rules are ACTIVE. When user gives you a task:
1. Count files that will be modified
2. If ≥3 files OR other triggers apply → EnterPlanMode immediately
3. For implementation → write tests first
4. Before marking complete → run verification checklist

## Related Context
- Insights baseline: 53% success rate (trending ▼), need improvement
- v7.3.0 just released — verify these patterns hold for next sprint
- Global CLAUDE.md already documents Plan Mode + TDD, but enforcement was lax
