#!/usr/bin/env bash
# Local CI validation — matches GitHub Actions workflow checks
# Run before pushing: bash scripts/ci-validate.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

echo "╔════════════════════════════════════════════════════════════╗"
echo "║  LLM Router Local CI Validation                               ║"
echo "║  Matches: GitHub Actions workflow checks                  ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

FAILED=0
PASSED=0

check_step() { echo -e "${BLUE}▶${NC} $1"; }
pass_step() { echo -e "${GREEN}✓${NC} $1"; ((PASSED++)); }
fail_step() { echo -e "${RED}✗${NC} $1"; [ -n "$2" ] && echo "  $2"; ((FAILED++)); }

# ============================================================================
# 1. Version & Manifest Checks
# ============================================================================
echo -e "${BLUE}════ 1. Version & Manifest Checks ════${NC}"
echo ""

check_step "Checking pyproject.toml version"
VERSION=$(grep '^version = ' pyproject.toml | cut -d'"' -f2)
if [ -z "$VERSION" ]; then
    fail_step "pyproject.toml version" "Version not found"
else
    pass_step "Version: $VERSION"
fi

check_step "Checking plugin manifests"
for plugin in .claude-plugin .codex-plugin .factory-plugin; do
    if [ -f "$plugin/plugin.json" ] && [ -f "$plugin/marketplace.json" ]; then
        pass_step "$plugin (plugin.json + marketplace.json)"
    else
        fail_step "$plugin" "Missing required files"
    fi
done

# ============================================================================
# 2. Git & Branch Status
# ============================================================================
echo ""
echo -e "${BLUE}════ 2. Git Status ════${NC}"
echo ""

check_step "Checking working tree"
if [ -z "$(git status --porcelain)" ]; then
    pass_step "Clean working tree"
else
    git status --short | head -10
    fail_step "Uncommitted changes" "Commit or stash changes before pushing"
fi

check_step "Checking current branch"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
pass_step "Branch: $BRANCH"

# ============================================================================
# 3. Package Build & Verification
# ============================================================================
echo ""
echo -e "${BLUE}════ 3. Package Build ════${NC}"
echo ""

check_step "Cleaning dist/"
rm -rf dist/ build/ *.egg-info/ 2>/dev/null

check_step "Building sdist and wheel"
if uv build --quiet 2>/dev/null; then
    pass_step "Build succeeded"
else
    fail_step "Build failed" "Run: uv build (verbose)"
fi

check_step "Verifying sdist contains required files"
if tar tzf dist/*.tar.gz | grep -q "pyproject.toml" && \
   tar tzf dist/*.tar.gz | grep -q "README.md" && \
   tar tzf dist/*.tar.gz | grep -q "LICENSE"; then
    pass_step "Required files present"
else
    fail_step "Required files missing" "Check tarball contents"
fi

check_step "Verifying excluded files NOT in sdist"
EXCLUDED_FOUND=0
if tar tzf dist/*.tar.gz | grep -iE "(Dockerfile|docker-compose|CLAUDE\.md|SECURITY\.md)" > /dev/null 2>&1; then
    EXCLUDED_FOUND=1
fi
if [ $EXCLUDED_FOUND -eq 0 ]; then
    pass_step "No excluded files in sdist"
else
    fail_step "Excluded files found in sdist" "Review pyproject.toml [tool.hatch.build.targets.sdist]"
fi

# ============================================================================
# 4. Quick Syntax Check (Python 3 compile check)
# ============================================================================
echo ""
echo -e "${BLUE}════ 4. Python Syntax ════${NC}"
echo ""

check_step "Checking Python syntax"
SYNTAX_ERRORS=0
while IFS= read -r file; do
    if ! python3 -m py_compile "$file" 2>/dev/null; then
        echo "  Syntax error: $file"
        ((SYNTAX_ERRORS++))
    fi
done < <(find src/llm_router -name "*.py" -type f)

if [ $SYNTAX_ERRORS -eq 0 ]; then
    pass_step "All Python files valid"
else
    fail_step "Syntax errors found" "$SYNTAX_ERRORS file(s) have syntax errors"
fi

# ============================================================================
# 5. Plugin Directory Structure
# ============================================================================
echo ""
echo -e "${BLUE}════ 5. Plugin Structure ════${NC}"
echo ""

check_step "Checking .claude-plugin/ structure"
CLAUDE_FILES=0
[ -f ".claude-plugin/plugin.json" ] && ((CLAUDE_FILES++))
[ -f ".claude-plugin/marketplace.json" ] && ((CLAUDE_FILES++))
[ -f ".claude-plugin/.mcp.json" ] && ((CLAUDE_FILES++))
if [ $CLAUDE_FILES -eq 3 ]; then
    pass_step ".claude-plugin (3 files)"
else
    fail_step ".claude-plugin" "Expected 3 files, found $CLAUDE_FILES"
fi

check_step "Checking .codex-plugin/ structure"
CODEX_FILES=0
[ -f ".codex-plugin/plugin.json" ] && ((CODEX_FILES++))
[ -f ".codex-plugin/marketplace.json" ] && ((CODEX_FILES++))
[ -f ".codex-plugin/.mcp.json" ] && ((CODEX_FILES++))
if [ $CODEX_FILES -eq 3 ]; then
    pass_step ".codex-plugin (3 files)"
else
    fail_step ".codex-plugin" "Expected 3 files, found $CODEX_FILES"
fi

check_step "Checking .factory-plugin/ structure"
FACTORY_FILES=0
[ -f ".factory-plugin/plugin.json" ] && ((FACTORY_FILES++))
[ -f ".factory-plugin/marketplace.json" ] && ((FACTORY_FILES++))
if [ $FACTORY_FILES -eq 2 ]; then
    pass_step ".factory-plugin (2 files)"
else
    fail_step ".factory-plugin" "Expected 2 files, found $FACTORY_FILES"
fi

# ============================================================================
# 6. Summary
# ============================================================================
echo ""
echo "╔════════════════════════════════════════════════════════════╗"
TOTAL=$((PASSED + FAILED))
echo -e "║  Result: ${GREEN}$PASSED passed${NC}  ${RED}$FAILED failed${NC}  (of $TOTAL)               ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}✓ All checks passed! Ready to push.${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Review changes: git diff --stat origin/main"
    echo "  2. Push to GitHub: git push origin"
    echo "  3. Watch CI: https://github.com/LLM Router/llm_router/actions"
    echo ""
    exit 0
else
    echo -e "${RED}✗ $FAILED check(s) failed. Fix before pushing.${NC}"
    exit 1
fi
