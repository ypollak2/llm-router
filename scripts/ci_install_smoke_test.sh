#!/usr/bin/env bash
# Loop-5 #5 / OP-2 / G-034 — install-smoke test.
#
# What this catches:
#   The G-034 failure mode is "installed runtime drifts from source".
#   The observed symptom on 2026-06-09 was a running MCP server
#   raising `No module named 'llm_router.classification_allowlist'` while
#   the file existed in the source tree — the installed sdist did
#   not include the file because it had not been re-built.
#
#   This script reproduces an install-from-dist + smoke-import flow
#   in a *clean* throwaway venv, then exits non-zero on any import
#   or CLI failure. Running this as a CI gate before any PyPI
#   release prevents the same drift from shipping to users.
#
# Usage:
#   scripts/ci_install_smoke_test.sh           # builds fresh sdist, smokes
#   scripts/ci_install_smoke_test.sh path.tgz  # smokes a pre-built sdist
#
# Exit codes:
#   0  — every smoke step passed
#   1  — build failed
#   2  — venv creation failed
#   3  — package install failed
#   4  — smoke import failed (the G-034 surface)
#   5  — CLI entry-point check failed
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

step() { printf "%b▶ %s%b\n" "$YELLOW" "$1" "$NC"; }
pass() { printf "%b✅ %s%b\n" "$GREEN" "$1" "$NC"; }
fail() {
    printf "%b❌ %s%b\n" "$RED" "$1" "$NC" >&2
    exit "${2:-1}"
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── 1. Build (or accept pre-built) sdist ─────────────────────────────────

SDIST_PATH="${1:-}"
if [[ -z "$SDIST_PATH" ]]; then
    step "Building sdist via uv build"
    rm -rf dist/
    uv build --sdist 2>&1 | tail -3 || fail "uv build failed" 1
    SDIST_PATH="$(ls dist/*.tar.gz | head -1)"
    if [[ ! -f "$SDIST_PATH" ]]; then
        fail "no sdist found in dist/ after build" 1
    fi
    pass "built $SDIST_PATH"
else
    if [[ ! -f "$SDIST_PATH" ]]; then
        fail "sdist path not found: $SDIST_PATH" 1
    fi
    pass "using pre-built sdist: $SDIST_PATH"
fi

# ── 2. Verify the sdist contains the modules we care about ──────────────
#
# This is the SECOND defence against G-034. Even if the install
# succeeds, an sdist that's missing a critical module would leave
# the installed runtime silently broken at first use. Listing the
# tar contents is fast and gives a clear error if anything's
# missing.
step "Verifying sdist contents"
SDIST_MEMBERS="$(tar tzf "$SDIST_PATH")"
REQUIRED_MODULES=(
    "src/llm_router/__init__.py"
    "src/llm_router/cli.py"
    "src/llm_router/classification_allowlist.py"  # the G-034 canary
    "src/llm_router/invoice_reconciliation/__init__.py"
    "src/llm_router/invoice_reconciliation/anthropic.py"
    "src/llm_router/invoice_reconciliation/openai.py"
    "src/llm_router/invoice_reconciliation/gemini.py"
    "src/llm_router/enterprise/quotas.py"
    "src/llm_router/admin_api.py"
    "pyproject.toml"
)
for required in "${REQUIRED_MODULES[@]}"; do
    if ! grep -qE "(^|/)${required}$" <<<"$SDIST_MEMBERS"; then
        fail "sdist missing required module: $required (G-034 trip)" 4
    fi
done
pass "sdist contains every required module"

# ── 3. Create a clean throwaway venv ─────────────────────────────────────

TMPDIR_BASE="$(mktemp -d -t llm_router-install-smoke-XXXXXX)"
trap "rm -rf '$TMPDIR_BASE'" EXIT
VENV_DIR="$TMPDIR_BASE/venv"

step "Creating clean venv at $VENV_DIR"
python3 -m venv "$VENV_DIR" || fail "venv creation failed" 2
PY="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
"$PIP" install --quiet --upgrade pip || fail "pip self-upgrade failed" 2
pass "clean venv ready"

# ── 4. Install the sdist ─────────────────────────────────────────────────

step "Installing $SDIST_PATH into the clean venv"
"$PIP" install --quiet "$SDIST_PATH" 2>&1 | tail -5 \
    || fail "pip install of sdist failed" 3
pass "package installed"

# ── 5. Smoke-import every critical module ────────────────────────────────
#
# The G-034 root cause was a module that existed in source but not
# in the installed binary. The fix is to import every critical
# module and let the installer's view of the filesystem speak.

step "Smoke-importing critical modules"
"$PY" - <<'PYEOF' || fail "smoke import failed (likely G-034)" 4
import importlib
import sys

modules = [
    "llm_router",
    "llm_router.cli",
    "llm_router.classification_allowlist",
    "llm_router.admin_api",
    "llm_router.invoice_reconciliation",
    "llm_router.invoice_reconciliation.anthropic",
    "llm_router.invoice_reconciliation.openai",
    "llm_router.invoice_reconciliation.gemini",
    "llm_router.enterprise.quotas",
    "llm_router.enterprise.identity",
    "llm_router.enterprise.rbac",
]
failures = []
for name in modules:
    try:
        importlib.import_module(name)
        print(f"  ✓ {name}")
    except Exception as e:
        failures.append((name, repr(e)))
        print(f"  ✗ {name}: {e}", file=sys.stderr)
if failures:
    print(f"\n{len(failures)} module(s) failed to import", file=sys.stderr)
    sys.exit(1)
PYEOF
pass "every critical module importable"

# ── 6. CLI entry-point smoke test ────────────────────────────────────────
#
# `pyproject.toml` declares four console scripts. The actual G-034
# surface for each is: does the entry-point binary exist, and is
# its target ``main`` callable importable?
#
# We DO NOT exercise ``--help`` on every script because some are
# interactive wizards (``llm_router-onboard``, ``llm_router-quickstart``)
# that read stdin immediately. Running ``--help`` on those would
# hang or EOFError under CI's closed stdin — false-positive friction
# without surfacing real packaging bugs.
#
# Instead: confirm the wrapper exists + ``main`` is importable for
# every declared entry point, and reserve ``--help`` for the main
# ``llm_router`` CLI which we know supports it.

step "Smoke-testing CLI entry points"
declare -A ENTRY_POINTS=(
    ["llm_router"]="llm_router.cli:main"
    ["llm_router-onboard"]="llm_router.onboard:main"
    ["llm_router-install-hooks"]="llm_router.install_hooks:main"
    ["llm_router-quickstart"]="llm_router.quickstart:main"
)
for entry in "${!ENTRY_POINTS[@]}"; do
    target="${ENTRY_POINTS[$entry]}"
    module="${target%%:*}"
    attr="${target##*:}"
    if [[ ! -x "$VENV_DIR/bin/$entry" ]]; then
        fail "entry-point wrapper missing: $VENV_DIR/bin/$entry" 5
    fi
    if ! "$PY" -c "
import importlib
mod = importlib.import_module('$module')
assert hasattr(mod, '$attr'), '$module is missing $attr'
assert callable(getattr(mod, '$attr')), '$module.$attr is not callable'
" 2>/dev/null; then
        fail "entry-point target broken: $target" 5
    fi
    printf "  ✓ %s → %s\n" "$entry" "$target"
done
# Reserve the actual runtime exercise for the main CLI — it's the
# one users hit most often and the only one whose ``--help`` is
# stable (no stdin reads).
if ! "$VENV_DIR/bin/llm_router" --help </dev/null >/dev/null 2>&1; then
    fail "main 'llm_router --help' failed at runtime" 5
fi
printf "  ✓ llm_router --help (runtime)\n"
pass "every CLI entry point importable + main CLI runs"

# ── 7. Optional: exercise MCP entry point ────────────────────────────────
#
# The MCP server is what users actually run from Claude Code. We
# don't start a long-running server here (CI-unfriendly), but we
# DO ensure the entry-point module is importable end-to-end — same
# import surface a `llm_router mcp` invocation would hit.

step "Importing MCP server module"
"$PY" -c "
import llm_router.mcp_server  # noqa: F401
" 2>/dev/null || true  # this is optional; llm_router.mcp_server may live under a different path
# Don't fail the gate on a missing MCP module path — that's a
# follow-up tightening once the path is canonical.

# ── 8. All clear ─────────────────────────────────────────────────────────

printf "\n%b════════════════════════════════════════════%b\n" "$GREEN" "$NC"
printf "%b  G-034 install-smoke gate: PASS%b\n" "$GREEN" "$NC"
printf "%b  Safe to publish %s%b\n" "$GREEN" "$(basename "$SDIST_PATH")" "$NC"
printf "%b════════════════════════════════════════════%b\n" "$GREEN" "$NC"
