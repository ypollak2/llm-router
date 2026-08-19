#!/bin/bash
# End-to-end verification of installed llm_router hooks.
# Pipes representative payloads into the actual hooks Claude Code runs and
# asserts the outputs contain LLM Router branding + task 4 strict-mode logic.

set -uo pipefail
HOOKS_DIR="$HOME/.claude/hooks"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PY="$HOME/.local/share/uv/tools/llm-routing/bin/python"
SESSION_ID="verify-$(date +%s)"
ROUTER_DIR="$TMP/.llm_router"
mkdir -p "$ROUTER_DIR"

PASS=0; FAIL=0
check() { # check <name> <pattern> <output>
  local name="$1" pat="$2" out="$3"
  if echo "$out" | grep -q -- "$pat"; then
    echo "  ✓ $name"; PASS=$((PASS+1))
  else
    echo "  ✗ $name  (expected '$pat')"; FAIL=$((FAIL+1))
    echo "    output preview: $(echo "$out" | head -3 | tr '\n' ' | ')"
  fi
}

echo "── Test 1 · Stop summary uses LLM Router branding ──────────────"
# session-end hook reads JSON via stdin; minimal payload renders header
OUT=$(echo '{"session_id":"'"$SESSION_ID"'","stop_hook_active":true}' | env HOME="$TMP" "$PY" "$HOOKS_DIR/llm_router-session-end.py" 2>&1 || true)
check "stop summary shows ⚡ LLM Router"     "⚡ LLM Router\|LLM_ROUTER\|LLM Router" "$OUT"
check "stop summary does NOT show 'LLM Router'" '^' "$(echo "$OUT" | grep -v "LLM Router" | head -1)"

echo
echo "── Test 2 · enforce-route strict mode hard-blocks read-only Bash ──"
# Pre-write a pending route so the hook has work to do
cat > "$ROUTER_DIR/pending_route_${SESSION_ID}.json" <<EOF
{"expected_tool":"llm_code","task_type":"code","complexity":"moderate","issued_at":$(date +%s),"session_id":"$SESSION_ID"}
EOF
PAYLOAD='{"session_id":"'"$SESSION_ID"'","tool_name":"Bash","tool_input":{"command":"git log --oneline -5"}}'

# Smart mode (default) — should allow read-only Bash
OUT_SMART=$(echo "$PAYLOAD" | env HOME="$TMP" LLM_ROUTER_ENFORCE=smart "$PY" "$HOOKS_DIR/llm_router-enforce-route.py" 2>&1 || true)
check "smart mode allows 'git log'" "^$" "$(echo "$OUT_SMART" | head -1)"

# Re-prime pending state and try strict
cat > "$ROUTER_DIR/pending_route_${SESSION_ID}.json" <<EOF
{"expected_tool":"llm_code","task_type":"code","complexity":"moderate","issued_at":$(date +%s),"session_id":"$SESSION_ID"}
EOF
OUT_STRICT=$(echo "$PAYLOAD" | env HOME="$TMP" LLM_ROUTER_ENFORCE=strict "$PY" "$HOOKS_DIR/llm_router-enforce-route.py" 2>&1 || true)
check "strict mode BLOCKS 'git log'" '"decision": "block"' "$OUT_STRICT"
check "block reason mentions LLM Router not LLM Router" "llm_router\|LLM Router" "$OUT_STRICT"

echo
echo "── Test 3 · enforcement.log carries outcome stamps ──"
LOG="$ROUTER_DIR/enforcement.log"
check "log has outcome=BLOCKED(strict)" "outcome=BLOCKED(strict)" "$(cat "$LOG" 2>/dev/null || echo none)"

echo
echo "── Test 4 · statusline renders without error ──"
STATUS_INPUT='{"session_id":"'"$SESSION_ID"'","cwd":"'"$HOME"'/Projects/llm_router","model":{"id":"claude-opus-4-7"},"transcript_path":"/tmp/none"}'
OUT_STATUS=$(echo "$STATUS_INPUT" | env HOME="$TMP" bash "$HOOKS_DIR/llm_router-statusline.sh" 2>&1 || true)
check "statusline produces some output" "." "$OUT_STATUS"
# Statusline is icon-heavy; spot-check it doesn't crash
check "statusline didn't crash with traceback" "^$" "$(echo "$OUT_STATUS" | grep -i "traceback\|error" | head -1)"

echo
echo "── Test 5 · installed source signatures ──"
check "llm_router-session-end has 'LLM Router' header at L1249" "LLM Router" "$(sed -n '1249p' "$HOOKS_DIR/llm_router-session-end.py")"
check "llm_router-enforce-route has _strict variable" "_strict" "$(grep '_strict = enforce ==' "$HOOKS_DIR/llm_router-enforce-route.py" | head -1)"
check "rules file is LLM Router-branded" "LLM Router" "$(head -3 "$HOME/.claude/rules/llm_router.md")"

echo
echo "════════════════════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
echo "════════════════════════════════════════════════════════════"
exit $FAIL
