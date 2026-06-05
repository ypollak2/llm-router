#!/bin/bash
# Test that router savings update in real-time when routing decisions are made.
# This will catch the bug where savings amount stays stale.

set -euo pipefail

echo "🧪 Real-Time Savings Update Test"
echo "────────────────────────────────────────────────────"

# Get baseline savings
echo "Baseline status:"
BASELINE=$(llm-router status 2>&1)
echo "$BASELINE" | grep -A 5 "Routing savings" || echo "  (no baseline savings data)"

SAVINGS_BEFORE=$(echo "$BASELINE" | grep "7 days" | grep -o '\$[0-9.]*' | head -1 || echo "unknown")
DECISIONS_BEFORE=$(echo "$BASELINE" | grep "decisions" | grep -o '[0-9]*\s*decisions' | head -1 || echo "unknown")

echo
echo "Before:"
echo "  Decisions: $DECISIONS_BEFORE"
echo "  7-day savings: $SAVINGS_BEFORE"
echo

# Simulate routing by running a few router commands
echo "Triggering routing decisions..."
for i in {1..3}; do
    llm-router last --count 1 >/dev/null 2>&1 || true
    sleep 0.5
done

echo

# Get new status
echo "Status after routing:"
AFTER=$(llm-router status 2>&1)
echo "$AFTER" | grep -A 5 "Routing savings" || echo "  (no savings data)"

SAVINGS_AFTER=$(echo "$AFTER" | grep "7 days" | grep -o '\$[0-9.]*' | head -1 || echo "unknown")
DECISIONS_AFTER=$(echo "$AFTER" | grep "decisions" | grep -o '[0-9]*\s*decisions' | head -1 || echo "unknown")

echo
echo "After:"
echo "  Decisions: $DECISIONS_AFTER"
echo "  7-day savings: $SAVINGS_AFTER"
echo

# Check if savings changed
if [ "$SAVINGS_BEFORE" = "$SAVINGS_AFTER" ] && [ "$SAVINGS_BEFORE" != "unknown" ]; then
    echo "⚠️  WARNING: Savings amount didn't update!"
    echo "   This may indicate:"
    echo "   1. Stale cache (clear with: rm ~/.llm-router/cache)"
    echo "   2. Batch updates (savings calculated periodically, not real-time)"
    echo "   3. Database transaction issue (routing not being recorded)"
    exit 1
else
    echo "✅ Savings updated correctly"
    exit 0
fi
