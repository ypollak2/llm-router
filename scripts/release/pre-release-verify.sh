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
if ! uv run python scripts/ci/verify-version-sync.py; then
    exit 1
fi
echo ""

# 3. Verify plugin distributions are aligned
echo "3️⃣  Verifying plugin distribution synchronization..."
if ! uv run python scripts/ci/verify-plugin-sync.py; then
    exit 1
fi
echo ""

# `uv run python`, not bare `python3`: this repo requires >=3.11 and tomllib is
# stdlib only from 3.11. On macOS `python3` is the system 3.9, so the bare form
# died with ModuleNotFoundError *after* steps 1-3 had already printed green --
# a release script that fails halfway through reads like a release problem.
V_PYPROJECT=$(uv run python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")

# 4. Check CHANGELOG updated
echo "4️⃣  Checking CHANGELOG.md updated..."
if ! grep -qE "^## (\[$V_PYPROJECT\]|v$V_PYPROJECT)" CHANGELOG.md; then
    echo -e "${YELLOW}⚠️  CHANGELOG.md may not include v$V_PYPROJECT entry${NC}"
    echo "   (Optional: add manually or use release.sh to extract from git)"
else
    echo -e "${GREEN}✅ CHANGELOG.md updated${NC}"
fi
echo ""

# 5. Run linting
echo "5️⃣  Running linting (ruff)..."
if ! uv run ruff check src/ tests/ > /dev/null 2>&1; then
    echo -e "${RED}❌ Linting violations found${NC}"
    uv run ruff check src/ tests/
    exit 1
fi
echo -e "${GREEN}✅ No linting violations${NC}"
echo ""

# 6. Run tests
echo "6️⃣  Running test suite..."
# SAME ENVIRONMENT AS CI, or this check answers a different question.
#
# ci.yml sets a dummy OPENAI_API_KEY: a routing-audit fix means several test
# modules need SOME candidate in the provider chain (they patch the dispatch
# layer and make no network call), and a bare machine has no keys. Without it
# this script reported four failures that CI does not see — a release gate
# that disagrees with CI is one people learn to override.
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-test-dummy-key-for-ci-only-no-real-calls-made}"
if ! uv run pytest tests/ -q --tb=short > /dev/null 2>&1; then
    echo -e "${RED}❌ Tests failed${NC}"
    uv run pytest tests/ -q --tb=short
    exit 1
fi
echo -e "${GREEN}✅ All tests pass${NC}"
echo ""

# 7. Verify current branch is main
echo "7️⃣  Verifying current branch..."
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
    echo -e "${YELLOW}⚠️  Currently on branch: $CURRENT_BRANCH (not main)${NC}"
    echo "   Switch to main before release: git checkout main"
    exit 1
fi
echo -e "${GREEN}✅ On main branch${NC}"
echo ""

# 8. Check remote is up-to-date
echo "8️⃣  Checking remote sync..."
git fetch origin main > /dev/null 2>&1
if [[ $(git rev-list --count main..origin/main) -gt 0 ]]; then
    echo -e "${YELLOW}⚠️  Local main is behind origin/main${NC}"
    echo "   Pull latest: git pull origin main"
    exit 1
fi
echo -e "${GREEN}✅ Local main is up-to-date${NC}"
echo ""

# 9. CI must have PASSED on the commit being released
#
# This gate exists because 13.0.1 was tagged and published from a commit whose
# CI was red. The local checks above had all passed, which is exactly why they
# were not enough: they verify the working TREE, and CI verifies the COMMIT —
# across four Python versions, three operating systems, and the built wheel.
# Everything above this line can be green while the thing you are about to
# publish is not.
#
# The failure that time was a mutation-scope config file, harmless to the
# artifact. That is luck, not a reason to skip the check.
echo "9️⃣  Checking CI status for HEAD..."
if ! command -v gh > /dev/null 2>&1 || ! gh auth status > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  gh unavailable or unauthenticated — cannot verify CI${NC}"
    echo "   Check manually before tagging: gh run list --branch=main"
else
    HEAD_SHA=$(git rev-parse HEAD)
    CI_CONCLUSION=$(gh run list --branch=main --limit 20 \
        --json headSha,conclusion,status,workflowName \
        --jq "[.[] | select(.headSha==\"$HEAD_SHA\" and .workflowName==\"CI\")] | first | .conclusion // .status // \"none\"" 2>/dev/null)

    case "$CI_CONCLUSION" in
        success)
            echo -e "${GREEN}✅ CI passed for ${HEAD_SHA:0:8}${NC}"
            ;;
        none|"")
            echo -e "${RED}❌ No CI run found for ${HEAD_SHA:0:8}${NC}"
            echo "   Push the commit and let CI finish before tagging."
            exit 1
            ;;
        in_progress|queued)
            echo -e "${YELLOW}⚠️  CI still running for ${HEAD_SHA:0:8} — wait for it${NC}"
            exit 1
            ;;
        *)
            echo -e "${RED}❌ CI concluded '${CI_CONCLUSION}' for ${HEAD_SHA:0:8}${NC}"
            echo "   gh run list --branch=main --limit 5"
            exit 1
            ;;
    esac
fi
echo ""

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ All pre-release checks passed!${NC}"
echo -e "${GREEN}Ready to run: bash scripts/release/release.sh${NC}"
echo -e "${GREEN}========================================${NC}"
