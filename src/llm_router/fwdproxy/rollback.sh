#!/bin/sh
# rollback.sh: undo the forward-proxy deployment. Renames (never rm) the plist,
# claude-rc, and fwd-ca to *.discard-<UTC stamp>. Unloads the launchd agent if loaded.
# Leaves ~/.llm-router/fwd-ca-key (holds ca.key) in place and prints a note if it exists.
# Prints what it did.
set -eu
HOME_DIR=${LLM_ROUTER_WRAPPER_HOME:-$HOME}
TS=$(date -u +%Y%m%dT%H%M%SZ)
DID=0

# 1. unload the agent if loaded
if launchctl list com.llm_router.fwdproxy >/dev/null 2>&1; then
  if launchctl bootout "gui/$(id -u)/com.llm_router.fwdproxy" 2>/dev/null; then
    echo "rollback: unloaded com.llm_router.fwdproxy"
    DID=1
  else
    echo "rollback: bootout failed for com.llm_router.fwdproxy (agent may not be loaded)"
  fi
fi

# 2. rename the plist
PLIST="$HOME_DIR/Library/LaunchAgents/com.llm_router.fwdproxy.plist"
if [ -e "$PLIST" ]; then
  mv "$PLIST" "${PLIST}.discard-${TS}"
  echo "rollback: renamed $PLIST -> ${PLIST}.discard-${TS}"
  DID=1
fi

# 3. rename claude-rc
RC="$HOME_DIR/.llm-router/bin/claude-rc"
if [ -e "$RC" ]; then
  mv "$RC" "${RC}.discard-${TS}"
  echo "rollback: renamed $RC -> ${RC}.discard-${TS}"
  DID=1
fi

# 4. rename fwd-ca
FWD_CA="$HOME_DIR/.llm-router/fwd-ca"
if [ -e "$FWD_CA" ]; then
  mv "$FWD_CA" "${FWD_CA}.discard-${TS}"
  echo "rollback: renamed $FWD_CA -> ${FWD_CA}.discard-${TS}"
  DID=1
fi

# 5. check offline CA key dir (leave in place)
FWD_CA_KEY="$HOME_DIR/.llm-router/fwd-ca-key"
if [ -e "$FWD_CA_KEY" ]; then
  echo "note: CA key dir $FWD_CA_KEY left in place (contains ca.key); remove it by hand if no longer needed"
fi

if [ "$DID" -eq 0 ]; then
  echo "rollback: nothing to undo"
fi
