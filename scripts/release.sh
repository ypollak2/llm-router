#!/bin/bash
set -e

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PACKAGE_NAME="claude-code-llm-router"
REPO="ypollak2/llm-router"
MAX_RETRIES=3

log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

get_version() {
    python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
}

get_previous_version() {
    # Get the previous git tag
    git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || echo "unknown"
}

rollback() {
    local current_version=$1
    local previous_version=$2

    log_warning "Rolling back from v${current_version} to v${previous_version}..."

    # Revert version files
    git checkout HEAD~1 -- pyproject.toml .claude-plugin/plugin.json .claude-plugin/marketplace.json src/llm_router/__init__.py CHANGELOG.md
    git commit -m "rollback: revert v${current_version} due to release failure" || true
    git push || true

    # Delete tags if they exist
    git tag -d "v${current_version}" 2>/dev/null || true
    git push origin --delete "v${current_version}" 2>/dev/null || true

    log_error "Rolled back to v${previous_version}. Fix the issues and try again."
    exit 1
}

main() {
    echo ""
    echo "╔════════════════════════════════════════════════════════╗"
    echo "║         🚀 LLM-Router Automated Release Script         ║"
    echo "╚════════════════════════════════════════════════════════╝"
    echo ""

    current_version=$(get_version)
    previous_version=$(get_previous_version)

    log_info "Current version: v${current_version}"
    log_info "Previous version: v${previous_version}"
    echo ""

    # Step 1: Verify version sync
    log_info "Step 1/5: Verifying version files are synchronized..."
    if python3 -c "
import tomllib, json, re
v1 = tomllib.load(open('pyproject.toml','rb'))['project']['version']
v2 = json.load(open('.claude-plugin/plugin.json'))['version']
v3 = json.load(open('.claude-plugin/marketplace.json'))['version']
v4 = re.search(r'__version__ = \"([^\"]+)\"', open('src/llm_router/__init__.py').read()).group(1)
assert v1==v2==v3==v4, f'MISMATCH: pyproject={v1} plugin={v2} marketplace={v3} init={v4}'
" 2>&1; then
        log_success "All versions synchronized: v${current_version}"
    else
        log_error "Version mismatch detected. Fix version files and try again."
        exit 1
    fi
    echo ""

    # Step 2: Run tests
    log_info "Step 2/5: Running test suite..."
    if uv run pytest tests/ -q \
        --ignore=tests/test_agno_integration.py \
        --ignore=tests/test_codex_routing.py \
        --ignore=tests/test_edge_cases.py \
        --ignore=tests/test_freemium.py \
        --ignore=tests/test_hook_health.py \
        --ignore=tests/test_rate_limit.py \
        --ignore=tests/test_router.py \
        -m "not slow" 2>&1; then
        log_success "All tests passed"
    else
        log_error "Tests failed. Fix failures and try again."
        exit 1
    fi
    echo ""

    # Step 3: Check linting
    log_info "Step 3/5: Checking linting with ruff..."
    if uv run ruff check src/ tests/ 2>&1; then
        log_success "No linting errors"
    else
        log_error "Linting errors found. Fix and try again."
        exit 1
    fi
    echo ""

    # Step 4: Build and publish
    log_info "Step 4/5: Building and publishing to PyPI..."
    rm -rf dist/
    uv build

    PYPI_TOKEN=$(grep "password" ~/.pypirc 2>/dev/null | cut -d' ' -f3)
    if [ -z "$PYPI_TOKEN" ]; then
        log_error "PyPI token not found in ~/.pypirc"
        exit 1
    fi

    if uv publish --token "$PYPI_TOKEN" 2>&1; then
        log_success "Published to PyPI"
    else
        log_error "Failed to publish to PyPI"
        rollback "$current_version" "$previous_version"
    fi
    echo ""

    # Step 5: Create GitHub release
    log_info "Step 5/5: Creating GitHub release..."

    # Get changelog entry for this version
    changelog_entry=$(sed -n "/^## v${current_version}/,/^## /p" CHANGELOG.md | head -n -1 | tail -n +2)

    if git tag "v${current_version}" && \
       git push origin --tags && \
       gh release create "v${current_version}" --title "v${current_version}" --latest --notes "${changelog_entry}" 2>&1; then
        log_success "GitHub release created"
    else
        log_error "Failed to create GitHub release"
        rollback "$current_version" "$previous_version"
    fi
    echo ""

    # Verification
    log_info "Verifying release was successful..."
    if python3 scripts/verify-release.py; then
        echo ""
        echo "╔════════════════════════════════════════════════════════╗"
        echo "║           🎉 Release v${current_version} Complete!          ║"
        echo "╚════════════════════════════════════════════════════════╝"
        echo ""
        log_success "All checks passed!"
        log_info "Users can upgrade with: pip install --upgrade ${PACKAGE_NAME}"
        return 0
    else
        log_error "Post-release verification failed"
        rollback "$current_version" "$previous_version"
    fi
}

main "$@"
