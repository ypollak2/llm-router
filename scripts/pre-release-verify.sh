#!/bin/bash
# Pre-release verification checklist
# Prevents common issues before releasing new versions

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "🚀 Pre-Release Verification Checklist"
echo "===================================="
echo ""

# 1. Check working tree is clean
echo "1️⃣  Checking git working tree..."
if [[ -n $(git status -s) ]]; then
    echo -e "${RED}❌ Working tree has uncommitted changes${NC}"
    git status
    exit 1
fi
echo -e "${GREEN}✅ Working tree clean${NC}"
echo ""

# 2. Verify version sync across all files
echo "2️⃣  Verifying version synchronization..."
V_PYPROJECT=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
V_PLUGIN=$(python3 -c "import json; print(json.load(open('.claude-plugin/plugin.json'))['version'])")
V_MARKETPLACE=$(python3 -c "import json; print(json.load(open('.claude-plugin/marketplace.json'))['version'])")

if [[ "$V_PYPROJECT" != "$V_PLUGIN" ]] || [[ "$V_PLUGIN" != "$V_MARKETPLACE" ]]; then
    echo -e "${RED}❌ VERSION MISMATCH${NC}"
    echo "  pyproject.toml: $V_PYPROJECT"
    echo "  plugin.json: $V_PLUGIN"
    echo "  marketplace.json: $V_MARKETPLACE"
    exit 1
fi
echo -e "${GREEN}✅ All versions in sync: $V_PYPROJECT${NC}"
echo ""

# 3. Check CHANGELOG updated
echo "3️⃣  Checking CHANGELOG.md updated..."
if ! grep -q "## v$V_PYPROJECT" CHANGELOG.md; then
    echo -e "${YELLOW}⚠️  CHANGELOG.md may not include v$V_PYPROJECT entry${NC}"
    echo "   (Optional: add manually or use release.sh to extract from git)"
else
    echo -e "${GREEN}✅ CHANGELOG.md updated${NC}"
fi
echo ""

# 4. Run linting
echo "4️⃣  Running linting (ruff)..."
if ! uv run ruff check src/ tests/ > /dev/null 2>&1; then
    echo -e "${RED}❌ Linting violations found${NC}"
    uv run ruff check src/ tests/
    exit 1
fi
echo -e "${GREEN}✅ No linting violations${NC}"
echo ""

# 5. Run tests
echo "5️⃣  Running test suite..."
if ! uv run pytest tests/ -q --tb=short > /dev/null 2>&1; then
    echo -e "${RED}❌ Tests failed${NC}"
    uv run pytest tests/ -q --tb=short
    exit 1
fi
echo -e "${GREEN}✅ All tests pass${NC}"
echo ""

# 6. Verify current branch is main
echo "6️⃣  Verifying current branch..."
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
    echo -e "${YELLOW}⚠️  Currently on branch: $CURRENT_BRANCH (not main)${NC}"
    echo "   Switch to main before release: git checkout main"
    exit 1
fi
echo -e "${GREEN}✅ On main branch${NC}"
echo ""

# 7. Check remote is up-to-date
echo "7️⃣  Checking remote sync..."
git fetch origin main > /dev/null 2>&1
if [[ $(git rev-list --count main..origin/main) -gt 0 ]]; then
    echo -e "${YELLOW}⚠️  Local main is behind origin/main${NC}"
    echo "   Pull latest: git pull origin main"
    exit 1
fi
echo -e "${GREEN}✅ Local main is up-to-date${NC}"
echo ""

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ All pre-release checks passed!${NC}"
echo -e "${GREEN}Ready to run: bash scripts/release.sh${NC}"
echo -e "${GREEN}========================================${NC}"
