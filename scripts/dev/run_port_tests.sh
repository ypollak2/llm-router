#!/usr/bin/env bash
# Sandbox runner for the Chuzom->llm-router port. Runs every new slice's tests
# in one place. Usage: ./scripts/dev/run_port_tests.sh [--full]
# (--full also runs the whole suite)
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ ! -d .venv ]; then uv venv --python 3.12 .venv >/dev/null; uv pip install --python .venv/bin/python -e ".[dev]" >/dev/null; fi
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH="$PWD/src"
PORT_TESTS="tests/observability tests/classify tests/subscription_local tests/signals"
echo "=== Chuzom->llm-router port tests ==="
.venv/bin/python -m pytest $PORT_TESTS -q -p no:cacheprovider "$@"
if [ "${1:-}" = "--full" ]; then
  echo; echo "=== full suite (regression check) ==="
  .venv/bin/python -m pytest -q -p no:cacheprovider
fi
