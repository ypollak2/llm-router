# Style Guide — Visual & Documentation Standards

This document locks down visual style decisions to prevent iteration waste during releases.

---

## Header & Branding

**Status:** LOCKED (v6.4.0)

- **Format:** PNG image (generated from Playwright screenshot automation)
- **File:** `docs/llm-router-header.png`
- **Dimensions:** 1024×256 pixels
- **Background:** Black (#000000)
- **Text Color:** Orange (Claude Code brand)
- **Generation Tool:** Playwright → screenshot of HTML render
- **Deployment:** Referenced in README.md top section

**Why:** ANSI codes don't render in GitHub markdown. HTML/CSS fails in GitHub markdown. SVG approach has rendering issues across different viewers. PNG snapshot is most reliable cross-platform.

**Change Approval:** ANY header changes require explicit approval in PR comments before implementation. Do not iterate in main branch commits.

---

## README Structure

**Status:** LOCKED (v6.4.0)

Follows this order:
1. Header image (PNG)
2. One-line value prop ("Route every AI call...")
3. Badges (PyPI, Tests, Downloads, Python, MCP, License, Stars)
4. Savings metric ("Average savings: 60–80%")
5. Installation section
6. What It Does (concise narrative)
7. Mental Model (newcomer-friendly explanation)
8. Features (bulleted, benefits-focused)
9. Configuration (links to detailed docs)
10. Tools section (table of all MCP tools)
11. Architecture (module overview)

**Content Philosophy:**
- Concise (target: <200 lines)
- Link to detailed docs instead of inlining
- Focus on value, not feature lists
- One real-world example in Mental Model

**Change Approval:** Structural changes (reordering sections, adding new top-level sections) require discussion before PRs.

---

## CHANGELOG Format

**Status:** LOCKED (v6.4.0)

Sections per release:
- Version heading (## vX.Y.Z — Title (YYYY-MM-DD))
- Added (new features)
- Fixed (bug fixes)
- Changed (breaking changes, refactors)
- Removed (deprecated features)

**Archiving:** When CHANGELOG.md exceeds 50 lines, archive releases older than 2 versions to `CHANGELOG_ARCHIVE.md`.

---

## Documentation Standards

**Technical Docs:**
- Code examples must be runnable (tested syntax)
- Explain "why", not just "what"
- Use consistent terminology across all docs

**File Naming:**
- Lowercase with hyphens: `lessons-learned.md`, `style-guide.md`
- No spaces or underscores in documentation filenames

---

## Approval Workflow

Before making visual/style changes:

1. **Small changes** (typos, link updates, minor rewording) — Direct commit
2. **Content changes** (new sections, restructuring) — Discuss in PR before implementation
3. **Visual changes** (header format, color, styling) — Explicit approval required
4. **Archiving decisions** (moving content to separate files) — Align with maintainers first

---

## Issue Prevention

**Never iterate styling in git history:**
- Create a `.styling-branch` if exploring options
- Squash all styling iterations into one commit before merging
- Use `git rebase` to clean up before pushing to main

**Test rendering before main push:**
- Use local markdown preview tools
- Create temporary dev branch to test on GitHub if needed
- Lock visual style once approved

This prevents the 11-commit header iteration waste documented in [LESSONS_LEARNED.md](./LESSONS_LEARNED.md).
