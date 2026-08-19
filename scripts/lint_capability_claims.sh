#!/usr/bin/env bash
# lint_capability_claims.sh — CI ratchet guard against NEW unbacked absolute
# capability language in marketing/user-facing copy (README.md + docs/ + Docs/).
#
# Design: a "ratchet". The current tree carries a backlog of pre-existing
# absolute-claim lines (mostly internal planning docs). Those are recorded once
# in a baseline file; the guard fails ONLY when a claim appears that is not in
# the baseline — i.e. a NEWLY introduced unbacked claim. That matches task #19's
# intent ("flag NEW unbacked capability language"), not "rewrite the backlog".
#
# A line is a candidate claim when it contains an absolute capability term and
# is neither hedged nor suppressed:
#   * hedged     — line also matches a hedge phrase, or
#   * suppressed — line, or the line directly above it, contains `claim-ok`.
#
# Usage:
#   scripts/lint_capability_claims.sh                 # check; exit 1 on new claims
#   scripts/lint_capability_claims.sh --update-baseline  # re-record the backlog
#
# Dependency-free (grep/sed/sort). macOS bash 3.2 + Linux.
set -u

BASELINE="scripts/capability_claims_baseline.txt"
MODE="check"
[ "${1:-}" = "--update-baseline" ] && MODE="update"

CLAIM_RE='guarantee|guaranteed|prevents|never spends|never exceeds|100%[[:space:]]|fully enforced|proven|bank-grade|military-grade|tamper-proof|unbreakable|always blocks'
HEDGE_RE='not a( hard)? guarantee|not guaranteed|best[ -]effort|~|opt-in|may |can '

# Resolve target files, deduped by physical path (docs/ and Docs/ collide on a
# case-insensitive filesystem; sort -u on the resolved path removes the dupe).
resolve() { cd "$(dirname "$1")" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")"; }
raw=""
[ -f README.md ] && raw="$(resolve README.md)"
for d in docs Docs; do
  [ -d "$d" ] || continue
  # Exclude internal AUDIT / analysis docs: they quote and analyze claims (using
  # words like "proven"/"guaranteed") as evidence, and are not user-facing
  # marketing copy — the exact scope this guard targets. Scanning them would make
  # honest audit writing trip the marketing-claim ratchet.
  while IFS= read -r f; do
    raw="$raw
$(resolve "$f")"
  done <<EOF
$(find "$d" -type f -name '*.md' \
    -not -path '*/correctness-reset/*' \
    -not -path '*/self-audit-loop/*' \
    -not -path '*/routing-audit-agent/*' \
    -not -path '*/audit/*' \
    -not -path '*/archive/*' \
    -not -name 'AUDIT_PROMPT*')
EOF
done
files=$(printf '%s\n' "$raw" | grep -v '^$' | sort -u)

# Emit "relpath\tterm\ttext" for every unhedged, unsuppressed claim line.
emit_hits() {
  printf '%s\n' "$files" | while IFS= read -r file; do
    [ -z "$file" ] && continue
    rel=${file#"$(pwd -P)/"}
    prev=""
    while IFS= read -r line || [ -n "$line" ]; do
      if printf '%s' "$line" | grep -qiE "$CLAIM_RE"; then
        if printf '%s' "$line" | grep -qi 'claim-ok' || printf '%s' "$prev" | grep -qi 'claim-ok'; then
          prev="$line"; continue
        fi
        if printf '%s' "$line" | grep -qiE "$HEDGE_RE"; then
          prev="$line"; continue
        fi
        term=$(printf '%s' "$line" | grep -oiE "$CLAIM_RE" | head -1)
        # Signature is path + term + trimmed text — line-number-independent so
        # inserting lines elsewhere doesn't churn the baseline.
        txt=$(printf '%s' "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        printf '%s\t%s\t%s\n' "$rel" "$term" "$txt"
      fi
      prev="$line"
    done <"$file"
  done | sort -u
}

hits=$(emit_hits)

if [ "$MODE" = "update" ]; then
  printf '%s\n' "$hits" | grep -v '^$' >"$BASELINE"
  n=$(printf '%s\n' "$hits" | grep -cv '^$')
  echo "capability-claim lint: baseline updated — $n known claim(s) recorded in $BASELINE"
  exit 0
fi

# Check mode: fail on any hit not present in the baseline.
new=""
if [ -f "$BASELINE" ]; then
  new=$(printf '%s\n' "$hits" | grep -v '^$' | grep -Fxvf "$BASELINE" || true)
else
  new=$(printf '%s\n' "$hits" | grep -v '^$')
fi

if [ -z "$new" ]; then
  echo "capability-claim lint: OK — no NEW unbacked absolute claims."
  exit 0
fi

echo "capability-claim lint: FAILED — NEW unbacked claim(s) below."
echo "Hedge them, add a 'claim-ok' comment with justification, or (if vetted) run --update-baseline."
printf '%s\n' "$new" | while IFS="$(printf '\t')" read -r rel term txt; do
  printf '  %s  [%s]  %s\n' "$rel" "$term" "$txt"
done
count=$(printf '%s\n' "$new" | grep -cv '^$')
echo "($count new)"
exit 1
