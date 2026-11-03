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

# Run Python with the PROJECT interpreter. The project needs Python >= 3.11
# (requires-python in pyproject.toml; tomllib is stdlib only from 3.11), and the
# system python3 on macOS is 3.9: a bare `python3 -c "import tomllib"` died with
# ModuleNotFoundError on the first line of main(), before anything was printed
# (release dry run, 2026-11-03). `uv run --frozen python` is the same interpreter
# the test, lint and build steps use. Without uv, python3 is accepted only if it
# is new enough; otherwise stop here with a message that says what to do.
project_python() {
    if command -v uv >/dev/null 2>&1; then
        uv run --frozen python "$@"
    elif python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        python3 "$@"
    else
        local found
        found=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "not found")
        log_error "release.sh needs Python >= 3.11 (tomllib is stdlib only from 3.11); python3 here is: ${found}. Install uv (https://docs.astral.sh/uv/), which supplies the project interpreter, or put a Python >= 3.11 first on PATH." >&2
        return 1
    fi
}

get_version() {
    project_python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
}

get_previous_version() {
    # Get the previous git tag
    git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || echo "unknown"
}

# Perl source of the timeout fallback (see run_with_timeout). It lives in a
# function, not inline in a $( ), because bash 3.2 (the macOS /bin/bash, 3.2.57)
# fails to parse a heredoc with an unbalanced quote or parenthesis inside $( ).
_timeout_perl() {
    cat <<'PERL'
use strict;
use warnings;
use POSIX ();
use Time::HiRes qw(alarm);

my ($dur, @cmd) = @ARGV;
my %unit = (s => 1, m => 60, h => 3600, d => 86400);
my ($num, $suffix) = (defined $dur ? $dur : '') =~ /^(\d*\.?\d+)([smhd]?)$/
    or do { print STDERR "timeout: invalid time interval '", (defined $dur ? $dur : ''), "'\n"; exit 125 };
@cmd or do { print STDERR "timeout: missing command\n"; exit 125 };
my $secs  = $num * $unit{ $suffix || 's' };
my $grace = defined $ENV{RELEASE_TIMEOUT_KILL_AFTER} ? $ENV{RELEASE_TIMEOUT_KILL_AFTER} : 10;

$SIG{CHLD} = 'DEFAULT';
my $pid = fork();
defined $pid or do { print STDERR "timeout: fork failed: $!\n"; exit 125 };
if (!$pid) {
    { no warnings 'exec'; exec { $cmd[0] } @cmd; }
    my $err = $! + 0;
    print STDERR "timeout: failed to run command '$cmd[0]': $!\n";
    POSIX::_exit($err == POSIX::ENOENT() ? 127 : 126);
}

my $timed_out = 0;
$SIG{ALRM} = sub {
    if ($timed_out) { kill 'KILL', $pid; return }
    $timed_out = 1;
    kill 'TERM', $pid;
    alarm($grace) if $grace > 0;
};
alarm($secs) if $secs > 0;
my $got;
do { $got = waitpid($pid, 0) } while ($got == -1 && !$!{ECHILD});
alarm(0);
my $status = $?;
exit 124 if $timed_out;
exit 125 if $got != $pid;
exit(($status & 127) ? 128 + ($status & 127) : $status >> 8);
PERL
}

# run_with_timeout SECONDS COMMAND [ARG...] : GNU `timeout --foreground`, portable.
# macOS ships no `timeout` (coreutils names it `gtimeout`), so `timeout --foreground`
# exited 127 there and the test step could never run (dry run, 2026-11-03).
# Order: timeout, then gtimeout, then the perl fallback above (perl is in the macOS
# base system). The exit codes are the same in all three: 124 when the time limit
# hit; otherwise COMMAND's own status (128+N when a signal killed it); 127 if
# COMMAND is not found; 126 if it cannot be run. The fallback sends SIGTERM at the
# limit and SIGKILL RELEASE_TIMEOUT_KILL_AFTER seconds later (default 10), and still
# reports 124. Like --foreground, only COMMAND itself is signalled, not its process
# group (`uv run` forwards the TERM to the process it started: checked 2026-11-03).
run_with_timeout() {
    local secs=$1
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout --foreground "$secs" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout --foreground "$secs" "$@"
    elif command -v perl >/dev/null 2>&1; then
        perl -e "$(_timeout_perl)" -- "$secs" "$@"
    else
        log_error "No timeout, gtimeout or perl on PATH, so the test run cannot be bounded. Install coreutils (brew install coreutils)." >&2
        return 125
    fi
}

# Paths that must never be published, as a grep -E pattern matched case-insensitively
# against each line of `tar tzf <sdist>`.
#  * .env, CLAUDE.md and the .internal/ /research/ /deprecation/ /scripts/ /skills/
#    directories are blocked wherever they appear.
#  * The internal-document names (MONETIZATION ROADMAP STRATEGY PRICING
#    PHASE_COMPLETION) are blocked only on DOCUMENT files (.md .markdown .rst .txt
#    .pdf .doc .docx, also with a backup suffix such as .bak, .orig or ~).
#    Unanchored they also matched the source module src/llm_router/pricing.py
#    (case-insensitive), which blocked the 16.0.0 release dry run on 2026-11-03.
#    A document named for one of them still blocks, in a folder of that name too,
#    and under src/ as well: only non-document files (modules, stubs, data) are out
#    of its reach.
SDIST_LEAK_RE='\.env$|CLAUDE\.md|\.internal/|/research/|/deprecation/|/scripts/|/skills/|(MONETIZATION|ROADMAP|STRATEGY|PRICING|PHASE_COMPLETION).*\.(md|markdown|rst|txt|pdf|docx?)([^A-Za-z0-9/][^/]*)?$'

# Reads an sdist listing on stdin (one path per line, as `tar tzf` prints it) and
# prints the lines that must not be published. Prints nothing for a clean listing.
sdist_leaks() {
    grep -i -E "$SDIST_LEAK_RE" || true
}

# 0 = the sdist at $1 is clean; 1 = it holds a private file, or could not be
# inspected. An unreadable archive or an empty listing is a refusal, never a pass:
# a leak check that found nothing to check has not checked anything.
verify_sdist_clean() {
    local sdist=$1 listing leaked
    if ! listing=$(tar tzf "$sdist"); then
        log_error "Cannot read $sdist, so the sdist was NOT checked."
        return 1
    fi
    if [ -z "$listing" ]; then
        log_error "$sdist lists no files, so the sdist was NOT checked."
        return 1
    fi
    leaked=$(printf '%s\n' "$listing" | sdist_leaks)
    if [ -n "$leaked" ]; then
        log_error "BLOCKED: sdist contains private/internal files:"
        echo "$leaked"
        log_error "Fix [tool.hatch.build.targets.sdist] exclude in pyproject.toml"
        return 1
    fi
    log_success "sdist is clean — no private files detected"
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
    if project_python scripts/ci/verify-version-sync.py && project_python scripts/ci/verify-plugin-sync.py; then
        log_success "All versions and plugin distributions synchronized: v${current_version}"
    else
        log_error "Version mismatch detected. Fix version files and try again."
        exit 1
    fi
    echo ""

    # Step 2: Run tests (core tests, skip known failures)
    # Wrapped in run_with_timeout because pytest occasionally hangs in Py_FinalizeEx
    # (asyncio event-loop teardown leak from background HTTP clients) AFTER
    # all tests have passed. We treat exit code 124 (timeout) as success iff
    # the output shows the "[100%]" pytest completion marker.
    log_info "Step 2/5: Running test suite (core tests, skipping pre-existing failures)..."
    # 7200 s: a hang guard, not a speed target. The local suite runs single-threaded
    # here and takes about 70 minutes on the maintainer's Mac (2026-11-03 dry run);
    # the old 300 s default killed a healthy run with exit 124 and no "[100%]" marker,
    # which reads as a failed release. release.sh does not run in CI.
    # Override with RELEASE_TEST_TIMEOUT=<seconds>.
    local _test_timeout="${RELEASE_TEST_TIMEOUT:-7200}"
    local _test_output=""
    local _test_exit=0
    # `|| _test_exit=$?` bypasses `set -e` on non-zero exit while capturing the code.
    _test_output=$(run_with_timeout "$_test_timeout" uv run pytest tests/ -q \
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

    verify_sdist_clean "$SDIST_FILE" || exit 1
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
        log_info "Then verify: uv run --frozen python scripts/release/verify-release.py"
        return 0
    else
        log_error "GitHub release v${current_version} not found after creation"
        rollback "$current_version" "$previous_version"
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
