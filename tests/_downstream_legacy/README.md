# Quarantined tests — 13.0.0 sync

These eight modules could not be collected after the upstream sync. They are
**moved, not deleted**: each one still describes behaviour somebody cared
enough to test, and losing that silently is exactly the failure mode the sync
tooling exists to prevent.

Three distinct reasons, and they need different decisions:

## 1. Tests for symbols the sync replaced (5 files)

`test_classify.py`, `test_audit.py`, `test_signals.py`, `test_audit_routing.py`,
`test_team.py`.

Each imports a symbol that existed only in the pre-sync downstream tree —
`CONFIDENCE_THRESHOLD`, `cmd_audit`, `detect_pii`, `AuditedDecision`,
`_add_ws23_context`. The modules they test now come from upstream, which
brings its own tests for the same modules (11 audit, 3 team, 2 signals, 1
classify).

**Before deleting any of these, diff their assertions against the upstream
tests that replaced them.** "Upstream has a test file with a similar name" is
not the same claim as "upstream asserts the same behaviour", and only the
second one justifies dropping coverage.

`test_audit_routing.py` is the one to look at first: it tests the misroute
scorer, which upstream has **zero** files named for. That coverage is now
carried by upstream's `test_misroute_audit.py` under the module's new name —
verify that before assuming it is covered.

## 2. Tests for excluded capabilities (2 files)

`test_cp_audit.py`, `test_cp_sse_policy_events.py` reach `llm_router.enterprise`
*transitively*, through the control-plane modules, so the sync's
import-parsing skip did not catch them — it only sees direct imports.

Enterprise is deliberately not shipped downstream. These should be deleted
once someone confirms nothing else in them is worth keeping.

## 3. Missing test-only dependency (1 file)

`test_claim_evidence.py` reads a fixture that lives outside both synced trees.
`test_execution_ledger.py` needs `hypothesis`, which is not in the downstream
dev dependencies — that one is a one-line fix, not a quarantine.
