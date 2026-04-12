# /release — llm-router Release Skill

Automates the full release pipeline for llm-router. Run this skill whenever
you are ready to ship a new version.

## Steps

### 1. Confirm version
Ask the user: "What version are we releasing?" (e.g. `4.0.1`)

### 2. Sync all version strings
Update ALL of the following to the new version — grep first to catch any extras:
```bash
grep -rn "version" pyproject.toml .claude-plugin/plugin.json .claude-plugin/marketplace.json --include="*.toml" --include="*.json" | grep -v ".venv"
```
Files to update:
- `pyproject.toml` → `version = "X.Y.Z"`
- `.claude-plugin/plugin.json` → `"version": "X.Y.Z"`
- `.claude-plugin/marketplace.json` → `"version": "X.Y.Z"`

### 3. Update CHANGELOG.md
Add entry at the top:
```
## vX.Y.Z — YYYY-MM-DD
### What's new
- <bullet points>
### Technical notes
- <any migration notes>
```

### 4. Run tests — abort if any fail
```bash
uv run pytest tests/ -q --ignore=tests/test_integration.py
uv run ruff check src/ tests/
```

### 5. Verify version sync
```bash
python3 -c "
import tomllib, json
v1 = tomllib.load(open('pyproject.toml','rb'))['project']['version']
v2 = json.load(open('.claude-plugin/plugin.json'))['version']
v3 = json.load(open('.claude-plugin/marketplace.json'))['version']
assert v1==v2==v3, f'MISMATCH: pyproject={v1} plugin={v2} marketplace={v3}'
print(f'✅ All versions in sync: {v1}')
"
```

### 6. Deploy hooks (if any hook changed)
```bash
install -m 755 src/llm_router/hooks/auto-route.py ~/.claude/hooks/llm-router-auto-route.py
install -m 755 src/llm_router/hooks/session-end.py ~/.claude/hooks/llm-router-session-end.py
install -m 755 src/llm_router/hooks/session-start.py ~/.claude/hooks/llm-router-session-start.py
install -m 755 src/llm_router/hooks/enforce-route.py ~/.claude/hooks/llm-router-enforce-route.py
```

### 7. Commit and tag
```bash
git add -p   # stage deliberately — never git add .
git commit -m "feat(vX.Y.Z): <headline>"
git tag vX.Y.Z
git push && git push origin --tags
```

### 8. Publish to PyPI
```bash
rm -rf dist/ && uv build
PYPI_TOKEN=$(python3 -c "import configparser; c=configparser.ConfigParser(); c.read('/Users/yali.pollak/.pypirc'); print(c['pypi']['password'])")
uv publish --token "$PYPI_TOKEN"
```

### 9. Create GitHub Release
```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z — <headline>" \
  --latest \
  --notes "## What's new
- <bullets from CHANGELOG>

## Upgrade
\`\`\`bash
pip install --upgrade claude-code-llm-router && llm-router install
\`\`\`"
```

### 10. Reinstall plugin locally
```bash
claude plugin reinstall llm-router
claude plugin list | grep llm-router
```

### 11. Log the decision
Append to `docs/decisions.md`:
```
## YYYY-MM-DD — Release vX.Y.Z
**Decision**: shipped version X.Y.Z
**What changed**: <summary>
**Outcome**: published to PyPI, plugin reinstalled
```
