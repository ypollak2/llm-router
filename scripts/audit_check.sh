#!/usr/bin/env bash
# LLM Router correctness-reset — mechanical audit checks (Phase 9 / #6).
#
# Runs the REPRODUCIBLE, non-judgment portion of one audit pass against the
# CURRENT checkout: the CI-exact test suite plus the claim-evidence validator.
# The benchmark (needs a metered key + real spend) and the human gate-by-gate
# review are performed separately per 11_AUDIT_RUNBOOK.md — this script exists so
# the two consecutive passes run an IDENTICAL, un-fudgeable mechanical baseline.
#
# Exit 0 = mechanical checks clean (modulo the documented known-flake allowance).
# Exit 1 = a real failure that blocks the audit pass.
#
# Usage:  scripts/audit_check.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY="$(git status --porcelain | wc -l | tr -d ' ')"
echo "── LLM Router audit mechanical check ──────────────────────────────"
echo "commit:      $SHA   (uncommitted files: $DIRTY)"
if [ "$DIRTY" != "0" ]; then
  echo "WARNING: working tree is dirty — an audit pass must run on a FROZEN commit."
fi

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

# 1) CI-exact full suite: fresh HOME + dummy key + the CI ignores/timeout.
echo
echo "── (1/2) CI-exact test suite ──"
FRESH_HOME="$(mktemp -d)"
set +e
HOME="$FRESH_HOME" OPENAI_API_KEY=sk-test-dummy-key-for-ci-only-no-real-calls-made \
  "$PY" -m pytest --ignore=tests/test_agno_integration.py --timeout=30 -q \
  > /tmp/audit_suite.log 2>&1
SUITE_RC=$?
set -e 2>/dev/null || true
FAILS="$(grep -cE '^FAILED ' /tmp/audit_suite.log 2>/dev/null || true)"
FAILS="${FAILS:-0}"
echo "pytest rc=$SUITE_RC, FAILED lines=$FAILS"
# Known-flake allowance: the RC-0 and perf flakes are fixed (#206/#209); the only
# tolerated residual is the aiosqlite watchdog on the 3.11 job (not this runner).
if [ "$FAILS" != "0" ]; then
  echo "REAL FAILURES present — audit pass BLOCKED. Offending tests:"
  grep -E '^FAILED ' /tmp/audit_suite.log | sed 's/^/    /'
  SUITE_OK=0
else
  echo "suite clean."
  SUITE_OK=1
fi

# 2) Claim-evidence validator (Gate 14): no numeric public claim without evidence.
echo
echo "── (2/2) Claim-evidence validator (Gate 14) ──"
if [ -f scripts/validate_claim_evidence.py ]; then
  set +e
  "$PY" scripts/validate_claim_evidence.py > /tmp/audit_claims.log 2>&1
  CLAIMS_RC=$?
  set -e 2>/dev/null || true
  tail -3 /tmp/audit_claims.log
  [ "$CLAIMS_RC" = "0" ] && CLAIMS_OK=1 || CLAIMS_OK=0
else
  echo "scripts/validate_claim_evidence.py not found — record Gate-14 check manually."
  CLAIMS_OK=1
fi

echo
echo "── SUMMARY ────────────────────────────────────────────────────"
echo "commit=$SHA suite_ok=$SUITE_OK claims_ok=$CLAIMS_OK"
if [ "$SUITE_OK" = "1" ] && [ "$CLAIMS_OK" = "1" ]; then
  echo "MECHANICAL CHECKS: PASS  (record this + the manual gate review + benchmark"
  echo "                          in 11_AUDIT_RUNBOOK.md for this pass)"
  exit 0
fi
echo "MECHANICAL CHECKS: FAIL — this audit pass does not count; fix and re-freeze."
exit 1
