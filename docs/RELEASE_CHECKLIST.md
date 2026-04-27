# LLM Router Release Checklist

Use this checklist before every release. The automated release script (`bash scripts/release.sh`) handles most steps, but verify manually first.

---

## Pre-Release (1 hour)

### Code Quality
- [ ] All tests pass: `uv run pytest tests/ -q`
- [ ] No linting issues: `uvx ruff check src/ tests/`
- [ ] No type errors: (project uses runtime type checking via Pydantic)
- [ ] All git changes are staged: `git status`
- [ ] No uncommitted files: `git diff`

### Documentation
- [ ] CHANGELOG.md updated with new features/fixes
- [ ] README.md reflects current state
- [ ] docs/GETTING_STARTED.md is up-to-date
- [ ] docs/HOST_SUPPORT_MATRIX.md reflects supported hosts
- [ ] docs/QUICKSTART_2MIN.md is current

### Version Numbers
- [ ] `pyproject.toml` version updated (X.Y.Z)
- [ ] Version follows semver (major.minor.patch)
- [ ] Plugin versions will auto-sync during release

### Git Branch
- [ ] On the `main` branch: `git branch`
- [ ] Branch is up-to-date: `git pull origin main`
- [ ] No merge conflicts

---

## Release (3 minutes)

### Automated Release
```bash
bash scripts/release.sh
```

This script automatically:
1. ✅ Verifies versions are in sync
2. ✅ Runs full test suite
3. ✅ Checks linting
4. ✅ Builds and publishes to PyPI
5. ✅ Creates GitHub release with changelog
6. ✅ Verifies PyPI availability

### If Script Fails
- Check error message from script
- Run `python3 scripts/verify-release.py` to diagnose
- Fix the issue
- Re-run `bash scripts/release.sh`

---

## Post-Release (15 minutes)

### Verify PyPI
- [ ] Package available: `pip index versions llm-router` (check latest)
- [ ] Can install fresh: `pip install --upgrade llm-router`
- [ ] Version matches: `python -c "import llm_router; print(llm_router.__version__)"`

### Verify GitHub
- [ ] Release created: https://github.com/ypollak2/llm-router/releases
- [ ] Release notes include changelog
- [ ] Git tag matches version: `git tag | grep vX.Y.Z`

### Verify Plugin Distribution
- [ ] All plugin versions synced to new version
- [ ] Plugins available in marketplace (if applicable)
- [ ] Claude Code MCP registration updated (if needed)

### Verify Documentation
- [ ] docs/GETTING_STARTED.md still reflects current setup
- [ ] Installation instructions work end-to-end
- [ ] Links in docs are not broken
- [ ] GitHub repository README points to latest version

### User Communications (if major release)
- [ ] Update Twitter/social media (if public)
- [ ] Post release notes in Discord/community (if applicable)
- [ ] Send email to users (if applicable)

---

## Version Bump Guide

### Patch Release (7.6.0 → 7.6.1)
Use for: bug fixes, minor improvements, documentation updates

```bash
# Edit pyproject.toml
version = "7.6.1"

# Add CHANGELOG entry
## v7.6.1 — 2026-04-27
### Fixed
- Bug fix description
- Another bug fix
```

### Minor Release (7.6.0 → 7.7.0)
Use for: new features, significant improvements

```bash
# Edit pyproject.toml
version = "7.7.0"

# Add CHANGELOG entry
## v7.7.0 — 2026-04-27
### Added
- New feature description
- Another feature
### Fixed
- Any bug fixes included
```

### Major Release (7.6.0 → 8.0.0)
Use for: breaking changes, API changes, major refactoring

```bash
# Edit pyproject.toml
version = "8.0.0"

# Add CHANGELOG entry with migration guide
## v8.0.0 — 2026-04-27
### ⚠️ BREAKING CHANGES
- List breaking changes
- Include migration guide

### Added
- New features in this version
```

---

## Release Process Diagram

```
┌─────────────────────────────────────┐
│  1. Update version in pyproject.toml │
│  2. Update CHANGELOG.md             │
│  3. Commit & push to main           │
└─────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────┐
│  Run: bash scripts/release.sh        │
│                                     │
│  • Verify versions sync             │
│  • Run tests                        │
│  • Build & publish to PyPI          │
│  • Create GitHub release            │
│  • Verify success                   │
└─────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────┐
│  POST-RELEASE VERIFICATION          │
│                                     │
│  • Confirm PyPI availability        │
│  • Check GitHub release             │
│  • Verify docs are current          │
│  • Notify users (if major)          │
└─────────────────────────────────────┘
```

---

## Troubleshooting Releases

### "Version mismatch" error
```bash
# Check what versions exist
python3 -c "
import json, tomllib
print('pyproject.toml:', tomllib.load(open('pyproject.toml','rb'))['project']['version'])
print('plugin.json:', json.load(open('.claude-plugin/plugin.json'))['version'])
print('codex plugin.json:', json.load(open('.codex-plugin/plugin.json'))['version'])
"

# Update mismatched files manually, then retry
bash scripts/release.sh
```

### "Tests failed" error
```bash
# Run tests locally to see failures
uv run pytest tests/ -q

# Fix failures
# Re-run: bash scripts/release.sh
```

### "PyPI upload failed" error
```bash
# Check PyPI status: https://status.python.org/

# If temporary outage, wait and retry:
bash scripts/release.sh

# If authentication issue, check ~/.pypirc
cat ~/.pypirc  # Should have [pypi] section with token
```

### "GitHub release creation failed" error
```bash
# Check gh CLI is authenticated
gh auth status

# Authenticate if needed
gh auth login

# Re-run release script
bash scripts/release.sh
```

---

## After Failed Release

The script automatically rolls back on failure:
- Version files reverted to previous state
- Git tag deleted
- PyPI package deleted (if uploaded)
- Local main branch restored

**You can safely re-run** after fixing the issue:
```bash
bash scripts/release.sh
```

---

## Release Cadence

| Situation | Timeline |
|-----------|----------|
| Bug fixes | As needed (patch) |
| Features | Weekly (minor) |
| Major changes | Monthly (major) |
| Security patches | ASAP (hotfix) |

---

## Questions?

- **Release automation issues?** → Check `scripts/release.sh` and `scripts/verify-release.py`
- **Version mismatch?** → Run `python scripts/verify-version-sync.py`
- **Plugin distribution?** → Run `python scripts/verify-plugin-sync.py`
- **Documentation out of date?** → Update docs/ and re-run release

