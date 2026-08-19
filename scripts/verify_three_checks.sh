#!/usr/bin/env bash
set -u -o pipefail

# Derive the repo root from this script's location (scripts/..) so it works
# on any clone, not just the author's machine.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || {
  echo "FAIL  unable to cd to $REPO_ROOT"
  exit 1
}

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { printf "${GREEN}PASS${NC}  %s\n" "$1"; }
fail() { printf "${RED}FAIL${NC}  %s\n" "$1"; }
info() { printf "${YELLOW}INFO${NC}  %s\n" "$1"; }

now() {
  python3 - <<'PY'
import time
print(time.perf_counter())
PY
}

elapsed() {
  python3 - "$1" "$2" <<'PY'
import sys
start = float(sys.argv[1])
end = float(sys.argv[2])
print(f"{end - start:.3f}s")
PY
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

if have_cmd uv; then
  PYTHON_CMD=(uv run python)
  PYTEST_CMD=(uv run pytest)
  LLM_ROUTER_CMD=(uv run llm_router)
else
  PYTHON_CMD=(python3)
  PYTEST_CMD=(pytest)
  LLM_ROUTER_CMD=(llm_router)
fi

FAILURES=0

run_startup_timing() {
  info "Test 1/3: Startup timing (unreachable Ollama)"

  local log_file
  log_file="$(mktemp)"
  local start end total rc
  start="$(now)"

  OLLAMA_BASE_URL="http://10.255.255.1:11434" PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "${PYTHON_CMD[@]}" - <<'PY' >"$log_file" 2>&1
import sys
import time

from llm_router.dynamic_routing import initialize_dynamic_routing

start = time.perf_counter()
initialize_dynamic_routing()
elapsed = time.perf_counter() - start

print(f"initialize_dynamic_routing elapsed={elapsed:.3f}s")
sys.exit(0 if elapsed < 3.0 else 2)
PY
  rc=$?

  end="$(now)"
  total="$(elapsed "$start" "$end")"

  if [[ $rc -eq 0 ]]; then
    pass "Startup timing completed in $total (<3s)"
    cat "$log_file"
  else
    fail "Startup timing failed in $total"
    cat "$log_file"
    FAILURES=$((FAILURES + 1))
  fi

  rm -f "$log_file"
}

run_dashboard_tests() {
  info "Test 2/3: Dashboard tests"

  local log_file
  log_file="$(mktemp)"
  local start end total rc
  start="$(now)"

  "${PYTEST_CMD[@]}" tests/test_dashboard_data.py tests/test_session_report.py -q >"$log_file" 2>&1
  rc=$?

  end="$(now)"
  total="$(elapsed "$start" "$end")"

  if [[ $rc -eq 0 ]]; then
    pass "Dashboard tests completed in $total"
    cat "$log_file"
  else
    fail "Dashboard tests failed in $total"
    cat "$log_file"
    FAILURES=$((FAILURES + 1))
  fi

  rm -f "$log_file"
}

run_smoke_test() {
  info "Test 3/3: Smoke test"

  local doctor_log status_log
  doctor_log="$(mktemp)"
  status_log="$(mktemp)"

  local start end total doctor_rc status_rc status_start status_end status_total
  start="$(now)"

  "${LLM_ROUTER_CMD[@]}" doctor >"$doctor_log" 2>&1
  doctor_rc=$?

  if [[ $doctor_rc -eq 0 ]]; then
    status_start="$(now)"
    { time -p "${LLM_ROUTER_CMD[@]}" status; } >"$status_log" 2>&1
    status_rc=$?
    status_end="$(now)"
    status_total="$(elapsed "$status_start" "$status_end")"
  else
    status_rc=1
    status_total="n/a"
  fi

  end="$(now)"
  total="$(elapsed "$start" "$end")"

  if [[ $doctor_rc -eq 0 && $status_rc -eq 0 ]]; then
    pass "Smoke test completed in $total (status step: $status_total)"
    printf "\n[llm_router doctor]\n"
    cat "$doctor_log"
    printf "\n[llm_router status]\n"
    cat "$status_log"
  else
    fail "Smoke test failed in $total"
    printf "\n[llm_router doctor]\n"
    cat "$doctor_log"
    printf "\n[llm_router status]\n"
    cat "$status_log"
    FAILURES=$((FAILURES + 1))
  fi

  rm -f "$doctor_log" "$status_log"
}

run_startup_timing
echo
run_dashboard_tests
echo
run_smoke_test
echo

if [[ $FAILURES -eq 0 ]]; then
  pass "All 3 verification tests passed"
  exit 0
else
  fail "$FAILURES verification test(s) failed"
  exit 1
fi
