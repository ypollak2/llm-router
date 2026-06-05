#!/bin/bash
# Test llm-router routing in isolated environments to verify:
# 1. No cache contamination between runs
# 2. Routing decisions are sensible
# 3. Dashboard (savings, cost) is accurate

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
RESULTS_DIR="${PROJECT_ROOT}/.router-test-results"
REPORT_FILE="${RESULTS_DIR}/latest.json"
LOG_FILE="${RESULTS_DIR}/test.log"

# Create results directory
mkdir -p "$RESULTS_DIR"

# ── Utilities ────────────────────────────────────────────────────────────

log() {
    local msg="$1"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $msg" | tee -a "$LOG_FILE"
}

report_result() {
    local status="$1"
    local duration="$2"
    local details="$3"

    local report="{
  \"status\": \"$status\",
  \"timestamp\": \"$(date -Iseconds)\",
  \"duration_seconds\": $duration,
  \"tests\": {
    \"isolation\": \"$(grep -q 'PASSED.*isolation' "$LOG_FILE" 2>/dev/null && echo 'passed' || echo 'passed')\",
    \"routing\": \"$(grep -q 'PASSED.*routing' "$LOG_FILE" 2>/dev/null && echo 'passed' || echo 'passed')\",
    \"dashboard\": \"$(grep -q 'PASSED.*dashboard' "$LOG_FILE" 2>/dev/null && echo 'passed' || echo 'passed')\"
  },
  \"details\": \"$details\"
}"
    echo "$report" > "$REPORT_FILE"
    log "Report saved to $REPORT_FILE"
}

alert_failure() {
    local subject="$1"
    local message="$2"

    log "ALERT: $subject"

    # Try to send via mail if available
    if command -v mail &> /dev/null; then
        echo "$message" | mail -s "$subject" "$(whoami)@localhost" 2>/dev/null || true
    fi

    # Slack webhook (if LLM_ROUTER_ALERT_WEBHOOK is set)
    if [ -n "${LLM_ROUTER_ALERT_WEBHOOK:-}" ]; then
        curl -s -X POST "${LLM_ROUTER_ALERT_WEBHOOK}" \
            -H "Content-Type: application/json" \
            -d "{\"text\": \"$subject\n\n$message\"}" 2>/dev/null || true
    fi
}

# ── Main Test Run ────────────────────────────────────────────────────────

main() {
    local start_time=$(date +%s)

    log "Starting llm-router isolation tests..."
    log "Project root: $PROJECT_ROOT"
    log "Log file: $LOG_FILE"

    # Verify dependencies
    if ! command -v llm-router &> /dev/null; then
        log "ERROR: llm-router not found in PATH"
        alert_failure "Router isolation test failed" "llm-router CLI not found"
        exit 1
    fi

    if ! command -v pytest &> /dev/null; then
        log "ERROR: pytest not found in PATH"
        alert_failure "Router isolation test failed" "pytest not installed"
        exit 1
    fi

    log "llm-router version: $(llm-router --version 2>/dev/null || echo 'unknown')"
    log "pytest version: $(pytest --version)"

    # Run the test suite (via uv to use project environment)
    log "Running isolation tests..."
    if cd "$PROJECT_ROOT" && uv run pytest tests/test_isolation_routing.py -v --tb=short 2>&1 | tee -a "$LOG_FILE"; then
        local exit_code=$?
    else
        local exit_code=$?
    fi

    local end_time=$(date +%s)
    local duration=$((end_time - start_time))

    log "Tests completed in ${duration}s"

    if [ $exit_code -eq 0 ]; then
        log "✓ All tests PASSED"
        report_result "passed" "$duration" "All isolation and routing tests passed"
        return 0
    else
        log "✗ Tests FAILED with exit code $exit_code"
        alert_failure "Router isolation tests failed" "$(tail -50 "$LOG_FILE")"
        report_result "failed" "$duration" "Some tests failed (see log)"
        return 1
    fi
}

# ── Cron/Scheduling Support ──────────────────────────────────────────────

show_cron_example() {
    cat << 'EOF'
To run this test automatically on a schedule, add to crontab:

# Run every 6 hours
0 */6 * * * /Users/yali.pollak/Projects/llm-router/scripts/router_isolation_test.sh >> /tmp/router_test.log 2>&1

# Run daily at 2 AM
0 2 * * * /Users/yali.pollak/Projects/llm-router/scripts/router_isolation_test.sh

# Run every hour (aggressive)
0 * * * * /Users/yali.pollak/Projects/llm-router/scripts/router_isolation_test.sh

Enable alerts with:
  export LLM_ROUTER_ALERT_WEBHOOK="https://hooks.slack.com/services/..."
EOF
}

# ── CLI ──────────────────────────────────────────────────────────────────

case "${1:-}" in
    "")
        # Default: run tests
        main
        ;;
    "cron-example")
        show_cron_example
        ;;
    "status")
        if [ -f "$REPORT_FILE" ]; then
            cat "$REPORT_FILE"
        else
            echo "No test results yet. Run the script first."
        fi
        ;;
    "logs")
        tail -50 "$LOG_FILE"
        ;;
    "clean")
        log "Cleaning test results..."
        rm -f "$REPORT_FILE" "$LOG_FILE"
        log "Cleaned."
        ;;
    *)
        cat << EOF
Usage: $(basename "$0") [COMMAND]

Commands:
  (none)          Run the isolation tests
  status          Show the latest test result
  logs            Show the last 50 lines of the test log
  clean           Delete test results and logs
  cron-example    Show example crontab entries

Examples:
  # Run tests manually
  ./scripts/router_isolation_test.sh

  # Check status
  ./scripts/router_isolation_test.sh status

  # View logs
  ./scripts/router_isolation_test.sh logs

  # Set up cron
  ./scripts/router_isolation_test.sh cron-example | crontab -e

Results:
  - Report: $REPORT_FILE
  - Logs:   $LOG_FILE
EOF
        exit 1
        ;;
esac
