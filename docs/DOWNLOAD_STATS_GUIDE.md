# Download Statistics & Deprecation Guide

This guide covers three components for managing package adoption metrics and gracefully deprecating the legacy `claude-code-llm-router` package.

---

## 1️⃣ Automated Download Statistics (GitHub Actions)

### What It Does

The `.github/workflows/update-stats.yml` workflow:
- ✅ Runs weekly (every Sunday at 00:00 UTC)
- ✅ Fetches combined download stats from PyPI
- ✅ Updates `docs/analytics/downloads.json` and `docs/analytics/downloads.md`
- ✅ Automatically commits changes to the repo
- ✅ Can be triggered manually via GitHub Actions UI

### Setup

The workflow is already configured. To enable/customize:

```bash
# View the workflow
cat .github/workflows/update-stats.yml

# Manually trigger
# Go to: https://github.com/ypollak2/llm-router/actions/workflows/update-stats.yml
# Click "Run workflow" → "Run workflow"
```

### Schedule Options

Edit `.github/workflows/update-stats.yml` to change frequency:

```yaml
on:
  schedule:
    # Daily at midnight UTC
    - cron: '0 0 * * *'

    # Weekly on Sunday
    - cron: '0 0 * * 0'

    # Every Monday at 6 AM UTC
    - cron: '0 6 * * 1'
```

### Output Files

After running, the workflow creates:

- **`docs/analytics/downloads.json`** — Raw data (machine-readable)
  ```json
  {
    "total_downloads": 2450,
    "period": "recent",
    "packages": {
      "llm-routing": { "downloads": 1500, "description": "..." },
      "claude-code-llm-router": { "downloads": 950, "description": "..." }
    }
  }
  ```

- **`docs/analytics/downloads.md`** — Formatted summary (human-readable)
  ```markdown
  # Download Statistics
  **Total**: 2,450 downloads
  ```

### Manual Execution

```bash
# From project root, run stats command
uv run llm-router stats --format json > docs/analytics/downloads.json
uv run llm-router stats --format markdown > docs/analytics/downloads.md
```

### Displaying Stats in README

Update your README with dynamic links:

```markdown
## Downloads

[![Combined Downloads](https://img.shields.io/badge/downloads-2.45k-blue)](docs/analytics/downloads.md)
[View full statistics →](docs/analytics/dashboard.html)
```

Or embed the dashboard widget directly:

```html
<iframe src="https://htmlpreview.github.io/?https://raw.githubusercontent.com/ypollak2/llm-router/main/docs/analytics/dashboard.html"
        width="100%" height="500" frameborder="0"></iframe>
```

---

## 2️⃣ Deprecation Package for Legacy Users

### What It Does

The deprecation package (`deprecation/` directory) provides:
- ✅ A redirect package on PyPI for `claude-code-llm-router`
- ✅ Clear migration instructions to users
- ✅ Automatic dependency on `llm-routing>=7.6.2`
- ✅ Deprecation warnings when imported

### Why Both Packages?

When users upgrade `pip install claude-code-llm-router`, they get:
1. A deprecation warning
2. An automatic dependency on the new package
3. Clear migration instructions

This ensures smooth transitions without breaking existing workflows.

### File Structure

```
deprecation/
├── pyproject.toml                          # Package metadata
├── README.md                               # Migration guide
└── claude_code_llm_router/
    └── __init__.py                         # Re-exports + deprecation warning
```

### Publishing the Deprecation Package

#### One-Command Publish

```bash
bash scripts/publish-deprecation.sh
```

This script:
1. Builds the distribution
2. Publishes to PyPI
3. Shows verification links

#### Manual Steps

```bash
# 1. Build
cd deprecation
uv build

# 2. Publish (using your PyPI token from ~/.pypirc)
# Option A: With twine
twine upload dist/*

# Option B: Via environment variable
PYPI_TOKEN="pypi-..." twine upload dist/* --password "$PYPI_TOKEN"

# 3. Verify on PyPI
# https://pypi.org/project/claude-code-llm-router/
```

### What Users See

When they run:
```bash
pip install claude-code-llm-router
```

They get:
```
Installing collected packages: llm-routing, claude-code-llm-router
```

Then when they import:
```python
from claude_code_llm_router import ...
```

They see:
```
DeprecationWarning: claude-code-llm-router is DEPRECATED.
Please upgrade to 'llm-routing' instead: pip install --upgrade llm-routing
```

### Migration Path

Old users can:
```bash
# Option 1: Just upgrade (recommended)
pip install --upgrade llm-routing

# Option 2: Install the deprecation package (auto-upgrades)
pip install claude-code-llm-router
```

Both lead to the same result: `llm-routing` is installed and working.

---

## 3️⃣ Download Statistics Dashboard Widget

### What It Does

`docs/analytics/dashboard.html` is a self-contained HTML widget that:
- ✅ Displays combined download stats visually
- ✅ Shows package breakdown with charts
- ✅ Auto-refreshes every 5 minutes
- ✅ Works offline (reads from `downloads.json`)
- ✅ Responsive design (mobile-friendly)

### Features

- **Statistics Cards** — Total downloads, package count
- **Package Breakdown** — Individual downloads with percentages
- **Progress Bars** — Visual representation of adoption ratio
- **Metadata** — Period, last updated time, data source
- **Auto-Refresh** — Updates from JSON file every 5 minutes

### Displaying the Dashboard

#### Option 1: GitHub Pages (Recommended)

```markdown
# Statistics

[📊 View Download Dashboard →](https://ypollak2.github.io/llm-router/docs/analytics/dashboard.html)
```

#### Option 2: Embed in README

```html
<details>
<summary>📊 Download Statistics</summary>

<iframe src="https://htmlpreview.github.io/?https://raw.githubusercontent.com/ypollak2/llm-router/main/docs/analytics/dashboard.html"
        width="100%" height="600" frameborder="0" style="border-radius: 8px;"></iframe>

</details>
```

#### Option 3: Direct HTML Embed

```markdown
![Download Stats](docs/analytics/dashboard.html)
```

### Customizing the Dashboard

The dashboard reads from `docs/analytics/downloads.json`. To customize:

#### Colors
Edit the CSS `linear-gradient(135deg, #667eea 0%, #764ba2 100%)`:
```css
/* Change primary color */
background: linear-gradient(135deg, YOUR_COLOR1, YOUR_COLOR2);
```

#### Refresh Interval
Edit the JavaScript interval (default: 5 minutes):
```javascript
// Refresh every 10 minutes instead
setInterval(loadStats, 10 * 60 * 1000);
```

#### Font Size
Adjust in the CSS:
```css
.header h1 {
    font-size: 2.5em;  /* Change this */
}
```

---

## 📋 Complete Workflow: Step by Step

### Setup (One-time)

```bash
# 1. Ensure stats module is installed
uv sync

# 2. Test the stats command
uv run llm-router stats

# 3. Generate initial analytics files
uv run llm-router stats --format json > docs/analytics/downloads.json
uv run llm-router stats --format markdown > docs/analytics/downloads.md

# 4. Verify dashboard loads
# Open docs/analytics/dashboard.html in a browser
```

### Publishing Deprecation Release

```bash
# 1. Build and publish deprecation package
bash scripts/publish-deprecation.sh

# 2. Verify on PyPI
# https://pypi.org/project/claude-code-llm-router/

# 3. Test installation
pip install claude-code-llm-router
python -c "import claude_code_llm_router; print('Success')"
```

### Automating Stats Updates

```bash
# The GitHub Action runs automatically every Sunday
# Or manually trigger it:

# Go to: https://github.com/ypollak2/llm-router/actions
# Select: "Update Download Statistics"
# Click: "Run workflow"
```

### Embedding Dashboard in Docs

```bash
# 1. Update README.md
# Add: [View Dashboard →](docs/analytics/dashboard.html)

# 2. Update GitHub Pages config (if applicable)
# Ensure docs/ is published to GitHub Pages

# 3. Verify accessibility
# https://your-repo.github.io/docs/analytics/dashboard.html
```

---

## 🔍 Monitoring & Troubleshooting

### GitHub Action Fails

Check the action logs:
1. Go to: GitHub → Actions → "Update Download Statistics"
2. Click the failed run
3. View step outputs for error messages

Common issues:
- **PyPI API unavailable**: Retry next week
- **Package names wrong**: Update package names in `src/llm_router/stats.py`
- **No write permission**: Check GitHub Action permissions in `.github/workflows/update-stats.yml`

### Dashboard Shows No Data

Ensure `docs/analytics/downloads.json` exists:
```bash
ls -la docs/analytics/downloads.json

# If missing, generate it:
uv run llm-router stats --format json > docs/analytics/downloads.json
```

### Deprecation Package Not Installing

```bash
# Check PyPI
pip index versions claude-code-llm-router

# Reinstall
pip install --force-reinstall claude-code-llm-router

# Verify it pulls in llm-routing
pip show claude-code-llm-router
pip show llm-routing
```

---

## 📚 References

- **CLI Command**: `llm-router stats --help`
- **Stats Module**: `src/llm_router/stats.py`
- **Deprecation Package**: `deprecation/` directory
- **Dashboard**: `docs/analytics/dashboard.html`
- **GitHub Action**: `.github/workflows/update-stats.yml`
- **Publish Script**: `scripts/publish-deprecation.sh`

---

## 🚀 Next Steps

1. ✅ Run GitHub Action manually to generate initial data
2. ✅ Test the deprecation package locally
3. ✅ Embed dashboard in README
4. ✅ Publish deprecation package when ready
5. ✅ Monitor migration metrics weekly

---

**Questions?** [Open an issue →](https://github.com/ypollak2/llm-router/issues)
