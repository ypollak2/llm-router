# Lessons Learned — Recurring Issues & Prevention

Analysis of last 15+ commits (v6.0 → v6.4). Identifies patterns to prevent future regressions.

---

## Implementation Status

✅ **Issue #1 (Header/Styling Rabbit Holes)** — IMPLEMENTED
- Created `docs/STYLE.md` to lock visual style decisions
- Documents styling iteration prevention strategy
- Includes approval workflow for style changes

✅ **Issue #2 (Linting Violations)** — IMPLEMENTED
- Added `.git/hooks/pre-commit` to enforce `ruff check` before commits
- Created `scripts/pre-release-verify.sh` for automated pre-release verification
- Hook prevents commits with linting violations

✅ **Issue #3 (Tool Registration Drift)** — IMPLEMENTED
- Refactored `tests/test_server.py::test_all_tools_registered()` with dynamic tool discovery
- Test no longer requires manual updates when new tools are added
- Scans modules and validates against known tools list

✅ **Issue #4 (Test State Isolation)** — ALREADY IN PLACE
- Autouse fixtures reset config singleton between tests
- Prevents test pollution and cross-test state bleeding

✅ **Issue #5 (Version Sync Mismatches)** — PARTIALLY IMPLEMENTED
- Created `scripts/pre-release-verify.sh` (checks version sync)
- Added `.git/hooks/commit-msg` to detect version mismatches in release commits
- Prevents commits with version inconsistencies

⚠️  **Issue #6 (Documentation-First Requirement)** — MANUAL ENFORCEMENT
- No automated enforcement (requires code review discipline)
- Recommendation: Include "docs updated" checklist in PR templates
- Future improvement: Pre-commit hook to detect code changes without docs

---

## Issue #1: Header/Styling Rabbit Holes (11 commits wasted)

**Pattern:** Commits 57a307a through a0bd266 (11 total commits) iterating on README header styling.

**Root Causes:**
- Testing markdown rendering requires actual GitHub push
- ANSI codes don't render consistently in GitHub markdown
- HTML/CSS also fails in GitHub markdown
- SVG approach seemed right but took multiple attempts
- PNG generation required Playwright automation
- Each iteration required full commit + push cycle

**Impact:**
- 11 commits with zero functional value
- Polluted git history
- Created version churn (6.3.0 → 6.3.1 due to header fixes)

**Prevention for Future Releases:**

1. **Never iterate on formatting in git history:**
   - Create a `.styling-branch` if you must iterate on looks
   - Merge final version in a single commit
   - Use `git rebase` to squash styling commits before merging to main

2. **Test rendering locally first:**
   - Use local markdown preview tools (VSCode, mdx)
   - Create temporary test branch to verify before main push
   - If GitHub-specific rendering matters, test on dev branch first

3. **Lock visual style early:**
   - Agree on header/formatting once
   - Document in STYLE.md
   - Don't iterate after code freeze

**Action:** Created `docs/STYLE.md` template. Future style changes must be approved before implementation.

---

## Issue #2: Linting Violations Discovered Post-Commit (6 incidents)

**Pattern:** Commits d4aae10, 55e480c, dc957ee, be97c81, 9297fec, 00e319e all fixing ruff violations.

**Root Causes:**
- Forgot to run `uv run ruff check src/ tests/` before committing
- Pre-commit hooks not enforced
- Files added/modified without linting verification
- Unused imports, f-string violations discovered after push

**Impact:**
- 6 separate fix commits
- Breaks CI/CD confidence
- Forces unnecessary releases (6.2.1 was linting cleanup)

**Prevention:**

1. **Add pre-commit hook (enforce, non-optional):**
   ```bash
   # .git/hooks/pre-commit
   uv run ruff check src/ tests/ || exit 1
   ```

2. **Linting as CI gate:**
   - Run ruff in GitHub Actions before merge
   - Block merge if violations exist

3. **IDE integration:**
   - Configure VSCode/PyCharm to auto-fix on save
   - Use format-on-save settings

4. **Checklist before push:**
   ```
   - [ ] uv run ruff check src/ tests/ (no errors)
   - [ ] uv run pytest tests/ -q (all pass)
   - [ ] git diff shows expected changes only
   ```

**Action:** Add pre-commit hook enforcement. Update CLAUDE.md checklist.

---

## Issue #3: Tool Registration Out of Sync with Tests (2 incidents)

**Pattern:** 
- New tools added to `tools/admin.py` (e95980d added `llm_quality_guard`)
- Test file `test_server.py` still had old tool list
- Required separate commit d16ff52 to update expected tools

**Root Cause:**
- `test_server.py` has hardcoded list of expected tools
- Adding a tool requires 2 changes: implementation + test fixture
- Easy to forget second change

**Impact:**
- Test failure discovered post-push
- Required separate commit to fix
- Version mismatch scenarios possible

**Prevention:**

1. **Generate tool list dynamically:**
   ```python
   # Instead of hardcoded set:
   def get_registered_tools():
       tools = mcp._tool_manager.list_tools()
       return {t.name for t in tools}
   
   # Test becomes:
   def test_tools_registered():
       actual = get_registered_tools()
       # Assert against a minimal baseline, not a full list
       assert "llm_route" in actual
       assert "llm_classify" in actual
   ```

2. **Tool count check instead of exact match:**
   ```python
   def test_minimum_tools_registered():
       tools = mcp._tool_manager.list_tools()
       assert len(tools) >= 45  # Minimum, not exact
   ```

3. **Checklist:**
   ```
   - [ ] Add tool to tools/*.py
   - [ ] Register in register(mcp)
   - [ ] Run tests locally: uv run pytest tests/test_server.py -xvs
   - [ ] No test_all_tools_registered failures
   ```

**Action:** Refactor `test_server.py` to use dynamic tool discovery instead of hardcoded lists.

---

## Issue #4: Test State Bleeding & Isolation (5+ incidents)

**Pattern:** 
- be7d421: Reset config singleton between tests
- 5d3cd0a: Improve mock fixtures
- 18533ed: Add proper mock fixtures, disable Ollama
- 6b8d45f: Fix config reset
- 7b0af96: Prevent aiosqlite thread hang on pytest exit

**Root Cause:**
- Config singleton shared across tests
- Ollama availability varies (CI vs local)
- aiosqlite worker threads not cleaned up properly
- Tests depend on each other's state

**Impact:**
- Flaky tests (pass locally, fail in CI)
- Hard to debug race conditions
- CI pipeline unreliable

**Prevention:**

1. **Enforce test isolation with fixtures:**
   ```python
   @pytest.fixture(autouse=True)
   def reset_config_state():
       # Backup original state
       original = get_config()
       yield
       # Restore after test
       reset_config(original)
   ```

2. **Disable external dependencies in tests:**
   ```python
   # Use monkeypatch, not environment variables
   @pytest.fixture
   def mock_ollama(monkeypatch):
       monkeypatch.setenv("OLLAMA_BASE_URL", "")
       # Prevents test from hitting local Ollama
   ```

3. **Mark environment-sensitive tests:**
   ```python
   @pytest.mark.skipif(not OLLAMA_AVAILABLE, reason="Requires Ollama")
   def test_ollama_compression():
       pass
   ```

4. **Cleanup hooks for long-running resources:**
   ```python
   @pytest.fixture
   def db_session():
       session = get_db()
       yield session
       session.close()  # Always cleanup
       # Wait for background threads
       wait_for_background_tasks(timeout=1)
   ```

**Action:** 
- Create `conftest.py` with mandatory autouse fixtures for state cleanup
- Add `@pytest.mark.requires_ollama` marker
- Add final cleanup hook in pytest.ini

---

## Issue #5: Version Sync Mismatches (1+ incident)

**Pattern:** v6.4.0 released with tag pointing to wrong commit (74f1b46 instead of e95980d).

**Root Cause:**
- Release script created tag on old commit
- pyproject.toml updated to 6.4.0 but not committed first
- Tag created before commit pushed
- Manual fix required (retagging, force-push)

**Impact:**
- CI/CD pipeline complained about version mismatch
- Manual intervention needed to fix GitHub release
- Confusion about actual release state

**Prevention:**

1. **Pre-release verification script:**
   ```bash
   #!/bin/bash
   # verify-release-readiness.sh
   TAG="${1}"
   VERSION="${TAG#v}"
   
   # Check all versions match
   PYPROJECT=$(grep '^version = ' pyproject.toml | cut -d'"' -f2)
   PLUGIN=$(jq -r '.version' .claude-plugin/plugin.json)
   MARKET=$(jq -r '.version' .claude-plugin/marketplace.json)
   
   [[ "$PYPROJECT" == "$VERSION" ]] || exit 1
   [[ "$PLUGIN" == "$VERSION" ]] || exit 1
   [[ "$MARKET" == "$VERSION" ]] || exit 1
   
   echo "✅ All versions in sync: $VERSION"
   ```

2. **Automate version bumping:**
   ```bash
   # bump-version.sh v6.5.0
   VERSION=$1
   sed -i "s/version = .*/version = \"$VERSION\"/" pyproject.toml
   jq ".version = \"$VERSION\"" .claude-plugin/plugin.json > .tmp && mv .tmp .claude-plugin/plugin.json
   jq ".version = \"$VERSION\"" .claude-plugin/marketplace.json > .tmp && mv .tmp .claude-plugin/marketplace.json
   ```

3. **Release checklist (in CLAUDE.md):**
   ```
   - [ ] Run verify-release-readiness.sh v6.X.0
   - [ ] All versions in sync
   - [ ] Tests pass: uv run pytest tests/ -q
   - [ ] Linting passes: uv run ruff check src/ tests/
   - [ ] Run release.sh (automated)
   - [ ] Verify on PyPI within 2 min
   ```

**Action:** 
- Create `scripts/verify-release-readiness.sh`
- Update CLAUDE.md release section
- Add pre-release checks to release.sh

---

## Issue #6: Documentation Not Updated with Features

**Pattern:** v6.3 released with "Three-Layer Compression" but README was 1061 lines and unclear.

**Root Cause:**
- Feature implementation complete
- Documentation accumulated over time
- No "documentation sweep" step before release
- Old/redundant sections never removed

**Impact:**
- New users confused about what the tool does
- Key features buried in 1000+ lines of text
- High barrier to understanding

**Prevention:**

1. **Document-first for features:**
   - When implementing a feature, write README section first
   - Submit PR with feature + docs together
   - Treat docs as part of definition-of-done

2. **Pre-release documentation audit:**
   ```
   - [ ] README <= 300 lines total
   - [ ] Top 100 lines cover: What is it? Install? How it works?
   - [ ] Latest 2 versions highlighted (rest in CHANGELOG_ARCHIVE.md)
   - [ ] All new features documented
   - [ ] Links to detailed docs, not inline content
   ```

3. **Automatic doc format checks:**
   ```bash
   # Warn if README > 400 lines
   LINES=$(wc -l < README.md)
   if [[ $LINES -gt 400 ]]; then
     echo "⚠️  README is $LINES lines (recommend <300)"
   fi
   ```

**Action:** 
- Add documentation audit to pre-release checklist
- Implement line-count warning in release.sh
- Update feature PR template to require doc changes

---

## Summary: Prevention Checklist

### Before Every Commit
- [ ] `uv run ruff check src/ tests/` passes
- [ ] `uv run pytest tests/ -q` passes
- [ ] No formatting/styling commits (batch into one)
- [ ] Tools added? → Test file updated

### Before Every Release
- [ ] Run `scripts/verify-release-readiness.sh v6.X.0`
- [ ] All versions synced (pyproject, plugins, marketplace)
- [ ] README <= 300 lines
- [ ] CHANGELOG reflects new features
- [ ] Tests pass on CI

### After Every Release
- [ ] Verify on PyPI within 2 minutes
- [ ] Verify on GitHub within 5 minutes
- [ ] Check for regressions via automated health check

---

## Implementation Timeline

**Week 1:**
- [ ] Create pre-commit hook for ruff
- [ ] Create `verify-release-readiness.sh`
- [ ] Add checklist to CLAUDE.md

**Week 2:**
- [ ] Refactor `test_server.py` for dynamic tool discovery
- [ ] Add conftest.py fixtures for state isolation
- [ ] Add environment markers for Ollama-dependent tests

**Ongoing:**
- [ ] Enforce checklist on every PR
- [ ] Review prevention in sprint retros
- [ ] Update LESSONS_LEARNED.md with new patterns
