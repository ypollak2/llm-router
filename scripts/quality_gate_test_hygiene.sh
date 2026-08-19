#!/usr/bin/env bash
# G4 (quality gate) — test hygiene ratchet.
#
# The audit (pattern D, "tests that cannot fail") found 9 doctor tests wrapped
# in `try/except Exception: pass` — an assertion that can never fail. This gate
# counts except-handlers whose entire body is `pass` / `...` / `continue` inside
# tests/ and fails if the count RISES above a frozen baseline. It is a ratchet:
# it does not force fixing existing debt, but it blocks adding NEW can't-fail
# tests. Lower BASELINE as debt is paid down.
set -euo pipefail

# Frozen from the AST count at the time this gate was introduced (v1.0.1). This
# is existing debt the ratchet holds the line on; lower it as tests are fixed.
# Raised 33→34 deliberately: the +1 is a legitimate `except OSError: continue`
# file-read guard inside the src-scanning loop of
# test_claims_no_fabricated_magnitudes.py (RED2-05) — a defensive I/O skip in a
# data-gathering loop that still asserts on the collected offenders, NOT a
# can't-fail test whose assertion was swallowed.
BASELINE="${TEST_HYGIENE_BASELINE:-34}"

COUNT=$(python3 - <<'PY'
import ast, pathlib
swallow = {"pass"}
n = 0
for p in pathlib.Path("tests").rglob("*.py"):
    try:
        tree = ast.parse(p.read_text())
    except Exception:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body = node.body
            # handler body is exactly one no-op statement (pass / ... / bare continue)
            if len(body) == 1 and (
                isinstance(body[0], ast.Pass)
                or (isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and body[0].value.value is Ellipsis)
                or isinstance(body[0], ast.Continue)
            ):
                n += 1
print(n)
PY
)

echo "G4 test-hygiene: found ${COUNT} no-op except handlers in tests/ (baseline ${BASELINE})"
if [ "$COUNT" -gt "$BASELINE" ]; then
    echo "❌ G4 FAIL: new can't-fail test(s) added ($COUNT > $BASELINE). Assert on the"
    echo "   outcome instead of swallowing exceptions. If intentional, justify and"
    echo "   raise TEST_HYGIENE_BASELINE deliberately."
    exit 1
fi
echo "✓ G4 PASS: no new can't-fail tests."
