#!/usr/bin/env bash
# Part 3 — gate every change: enforcement lint + tests + version-sync + routing report.
# Run via `make verify` (or scripts/verify.sh). Non-zero exit on any failure.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "──> 1/4  enforcement lint (no direct-provider calls)"
uv run python scripts/lint_no_direct_llm.py src/llm_router

echo "──> 2/4  unit + integration tests"
uv run --extra dev pytest -q \
  tests/test_direct_session_spend.py \
  tests/test_local_task_no_route.py \
  tests/test_routing_report.py \
  tests/test_gateway_presets.py \
  tests/test_sdk.py \
  tests/test_public_import.py

echo "──> 3/4  version sync (pyproject == all manifests)"
uv run --extra dev pytest -q tests/qa/test_plugin_packaging.py -k version

echo "──> 4/4  routing report"
uv run python -m llm_router.routing_report >/dev/null && echo "   report → ~/.llm-router/routing_report.md"

echo ""
echo "✅ verify passed — safe to release (run scripts/release.sh X.Y.Z)"
