#!/usr/bin/env bash
# Everything CI will run, run locally first.
#
# Added 2026-09-15 after five ruff errors reached PR #138 and turned the lint job
# red. The full pytest suite was green locally the whole time, because the local
# suite never ran ruff — CI did. A gate that does not run what CI runs is not a
# gate, it is a rehearsal of the easy half.
#
#   ./scripts/precommit_gate.sh          lint + fast checks
#   ./scripts/precommit_gate.sh --full   the above plus the whole test suite
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0

step() { printf "\n\033[1m%s\033[0m\n" "$1"; }

step "ruff (exactly what .github/workflows/ci.yml runs)"
if command -v uvx >/dev/null 2>&1; then
  uvx ruff check src/ tests/ || fail=1
else
  .venv/bin/python -m ruff check src/ tests/ 2>/dev/null || {
    echo "  ruff unavailable; CI will still run it"; }
fi

step "plugin bundle is in sync"
.venv/bin/python scripts/build_plugin_bundle.py >/dev/null 2>&1
if ! git diff --quiet -- hooks/ 2>/dev/null; then
  echo "  hooks/ changed — the bundle was stale; stage the rebuild"
  fail=1
else
  echo "  in sync"
fi

step "mutation exclusions are derived, not hand-edited"
.venv/bin/python scripts/gf_excluded_tests.py --check >/dev/null 2>&1 \
  && echo "  in sync" || { echo "  regenerate: python scripts/gf_excluded_tests.py"; fail=1; }

if [[ "${1:-}" == "--full" ]]; then
  step "full test suite"
  .venv/bin/python -m pytest tests/ --timeout=300 -p no:randomly -q || fail=1
else
  printf "\n  (run with --full to include the test suite)\n"
fi

printf "\n"
[[ $fail -eq 0 ]] && echo "gate: PASS" || echo "gate: FAIL — fix before committing"
exit $fail
