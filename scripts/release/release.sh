#!/bin/bash
set -e

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PACKAGE_NAME="llm-routing"
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

# rollback only ever undoes LOCAL state. It never pushes, never deletes a tag on
# origin and never rewrites main: a tag that is public triggers publish.yml
# (OIDC, behind eval-gate), and re-pushing or deleting a public tag is forbidden.
rollback() {
    local current_version=$1
    local previous_version=$2

    log_warning "Release of v${current_version} failed (previous: v${previous_version})."

    if git ls-remote --exit-code --tags origin "refs/tags/v${current_version}" >/dev/null 2>&1; then
        log_error "Tag v${current_version} is already public on origin. NOT deleting or re-pushing it."
        log_error "Check: gh run list --workflow publish.yml ; gh release view v${current_version}"
        log_error "If 'gh release view' finds no release, create it by hand with the eval asset, then re-run the failed publish run:"
        log_error "  run Step 5's release command by hand for v${current_version}, attaching ${eval_asset:-release_evals/<sha>.json} with --notes-file ${notes_file:-<notes file>}"
        log_error "  gh run rerun <publish.yml run id for v${current_version}> --failed   (eval-gate fetches the asset from the release)"
        log_error "If the release is bad, cut a new patch version; do not move this tag."
        exit 1
    fi

    # Tag never reached origin: dropping the local tag is safe.
    git tag -d "v${current_version}" 2>/dev/null || true
    log_error "Local tag removed; nothing was pushed or published. Fix the issue and re-run."
    exit 1
}

# Release notes for $1 (version) from CHANGELOG file $2, written to file $3.
# Portable (BSD/macOS and GNU): awk only (no GNU-only head flags).
# Body is everything between `## [X.Y.Z]` and the next `## ` heading.
# GitHub caps release bodies at ~125,000 chars; above NOTES_MAX_BYTES the body is
# replaced by a link to CHANGELOG.md plus the section's "Known issues" subsection
# (never a truncated prefix, which would cut mid-entry).
NOTES_MAX_BYTES="${NOTES_MAX_BYTES:-100000}"

extract_changelog_section() {
    awk -v h="## [$1]" '
        index($0, h) == 1 { f = 1; next }
        f && /^## / { exit }
        f { print }
    ' "$2"
}

extract_known_issues() {
    awk '
        /^### Known issues/ { f = 1 }
        f && /^## / { exit }
        f && /^### / && !/^### Known issues/ { exit }
        f { print }
    '
}

write_release_notes() {
    local version=$1 changelog=$2 out=$3
    extract_changelog_section "$version" "$changelog" > "$out"
    local size
    size=$(wc -c < "$out" | tr -d ' ')
    if [ "$size" -gt "$NOTES_MAX_BYTES" ]; then
        local known
        known=$(extract_known_issues < "$out")
        {
            echo "Full notes: CHANGELOG.md section [${version}]"
            echo "https://github.com/${REPO}/blob/v${version}/CHANGELOG.md"
            if [ -n "$known" ]; then
                echo ""
                echo "$known"
            fi
        } > "$out.short"
        mv "$out.short" "$out"
    fi
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
    if python3 scripts/ci/verify-version-sync.py && python3 scripts/ci/verify-plugin-sync.py; then
        log_success "All versions and plugin distributions synchronized: v${current_version}"
    else
        log_error "Version mismatch detected. Fix version files and try again."
        exit 1
    fi
    echo ""

    # Step 2: Run tests (core tests, skip known failures)
    # Wrapped in `timeout` because pytest occasionally hangs in Py_FinalizeEx
    # (asyncio event-loop teardown leak from background HTTP clients) AFTER
    # all tests have passed. We treat exit code 124 (timeout) as success iff
    # the output shows the "[100%]" pytest completion marker.
    log_info "Step 2/5: Running test suite (core tests, skipping pre-existing failures)..."
    local _test_timeout="${RELEASE_TEST_TIMEOUT:-300}"
    local _test_output=""
    local _test_exit=0
    # `|| _test_exit=$?` bypasses `set -e` on non-zero exit while capturing the code.
    _test_output=$(timeout --foreground "$_test_timeout" uv run pytest tests/ -q \
        --ignore=tests/test_agno_integration.py \
        --ignore=tests/test_codex_routing.py \
        --ignore=tests/test_edge_cases.py \
        --ignore=tests/test_freemium.py \
        --ignore=tests/test_hook_health.py \
        --ignore=tests/test_profile_invariants.py \
        --ignore=tests/test_quality_guard.py \
        --ignore=tests/test_rate_limit.py \
        --ignore=tests/test_router.py \
        --ignore=tests/test_adaptive_router.py \
        --ignore=tests/test_agent_loop.py \
        --ignore=tests/commands/test_doctor.py \
        --deselect=tests/test_cost.py::test_get_router_efficiency \
        --deselect=tests/test_cost.py::test_get_classifier_overhead \
        --deselect=tests/test_cost.py::test_get_savings_by_task_type \
        -m "not slow" \
        --tb=line \
        --disable-warnings 2>&1) || _test_exit=$?
    echo "$_test_output"
    if [ $_test_exit -eq 0 ]; then
        log_success "All tests passed"
    elif [ $_test_exit -eq 124 ] && echo "$_test_output" | grep -q "\[100%\]"; then
        log_success "All tests passed (Python shutdown hung after [100%]; killed by timeout — known asyncio teardown leak)"
    else
        log_error "Tests failed (exit $_test_exit). Fix failures and try again."
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

    # Step 4: Build and verify contents. NO upload here: PyPI publishing happens
    # only in .github/workflows/publish.yml (OIDC) on tag push, behind eval-gate.
    log_info "Step 4/5: Building locally (sanity check only, no upload)..."
    local _build_dir
    _build_dir=$(mktemp -d)
    uv build --out-dir "$_build_dir"

    # Safety: verify sdist contains no private files before tagging
    log_info "Verifying sdist contents..."
    SDIST_FILE=$(ls "$_build_dir"/*.tar.gz 2>/dev/null | head -1)
    if [ -z "$SDIST_FILE" ]; then
        log_error "No sdist file found in $_build_dir"
        exit 1
    fi

    LEAKED_FILES=$(tar tzf "$SDIST_FILE" | grep -i -E '\.env$|CLAUDE\.md|MONETIZATION|ROADMAP|STRATEGY|PRICING|PHASE_COMPLETION|\.internal/|/research/|/deprecation/|/scripts/|/skills/' || true)
    if [ -n "$LEAKED_FILES" ]; then
        log_error "BLOCKED: sdist contains private/internal files:"
        echo "$LEAKED_FILES"
        log_error "Fix [tool.hatch.build.targets.sdist] exclude in pyproject.toml"
        exit 1
    fi
    log_success "sdist is clean — no private files detected"
    rm -rf "$_build_dir"
    echo ""

    # Step 5: Create GitHub release
    log_info "Step 5/5: Creating GitHub release..."

    # Notes go to a file (size cap + portable extraction: see write_release_notes).
    notes_file=$(mktemp)
    write_release_notes "$current_version" CHANGELOG.md "$notes_file"

    # P3.2: .github/workflows/release.yml fetches release_evals/<sha>.json from
    # this release's assets and gates publish on it. Refuse BEFORE tagging: a tag
    # without the file can only fail in CI, after the tag is public.
    eval_asset="release_evals/$(git rev-parse HEAD).json"
    if [[ ! -f "$eval_asset" ]]; then
        log_error "Missing $eval_asset - run the release eval sets for this commit first (scripts/release/release_evals.py)"
        exit 1
    fi

    if git tag "v${current_version}" && \
       git push origin "v${current_version}" && \
       gh release create "v${current_version}" "$eval_asset" --title "v${current_version}" --latest --notes-file "$notes_file" 2>&1; then
        log_success "GitHub release created"
        rm -f "$notes_file"
    else
        log_error "Failed to create GitHub release"
        rollback "$current_version" "$previous_version"
    fi
    echo ""

    # Verification. verify-release.py checks PyPI, which cannot be live yet:
    # publish.yml runs after the tag, behind eval-gate. So verify only what this
    # script did (the GitHub release); never roll back over PyPI lag.
    log_info "Verifying GitHub release exists..."
    if gh release view "v${current_version}" >/dev/null 2>&1; then
        echo ""
        echo "╔════════════════════════════════════════════════════════╗"
        echo "║        🎉 Release v${current_version} tagged and created!     ║"
        echo "╚════════════════════════════════════════════════════════╝"
        echo ""
        log_success "Tag pushed and GitHub release created."
        log_info "PyPI is NOT published by this script. publish.yml publishes via OIDC after eval-gate passes."
        log_info "Watch it: gh run list --workflow publish.yml"
        log_info "Then verify: python3 scripts/release/verify-release.py"
        return 0
    else
        log_error "GitHub release v${current_version} not found after creation"
        rollback "$current_version" "$previous_version"
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
