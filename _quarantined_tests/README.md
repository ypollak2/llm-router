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

---

## Second batch — added after the server/redactor upstream fixes

Five more, all **downstream-only** (no upstream counterpart) and all testing
implementations the sync replaced:

| file | tested |
|---|---|
| `test_hook_equivalence.py` | the pre-sync `classify` module's byte-equivalence with the hook |
| `test_budget_envelope.py` | the pre-sync `budget_envelope` API |
| `test_subscription_local.py` | the pre-sync subscription-local routing |
| `test_summary.py`, `test_surface_status.py` | the `observability/` package before upstream's modules landed beside it |

The same caution applies as above: **upstream having a similarly-named module
is not the same claim as upstream asserting the same behaviour.** Diff before
deleting.

`test_hook_equivalence.py` deserves particular attention. It asserted that the
importable classifier stays byte-identical to the routing hook's own scorer —
a drift guard between two copies of one algorithm. If upstream has no
equivalent, deleting this loses a real invariant rather than a stale test.

### Two files deliberately NOT quarantined

`test_identity_gate.py` and `test_fs_tools_opt_in.py` are downstream's own and
still belong here. Both failed after the sync and both were **fixed rather than
quarantined**:

- the identity gate was flagging this quarantine directory, whose files contain
  `"chuzom"` precisely because they assert its absence — the allowlist gained a
  directory entry for that documented category;
- `test_fs_tools_opt_in.py` carried a deliberate tripwire asserting SEC-002
  layer 2 was *missing*. The sync brought path confinement, so the tripwire
  fired exactly as designed. It is now flipped to assert the confinement exists,
  plus a behavioural test that an escaping path actually raises — `hasattr` is
  not enforcement.

---

## Third batch — tests of the upstream benchmark harness

`test_deep_reasoning_classifier.py` (45 errors) and `test_routerarena_submit.py`
(6) both load `bench/routerarena/submission/router/…` — a repo-root tree the
sync does not carry, because it is upstream's benchmark-submission harness
rather than anything this package ships.

They load it by PATH, not by import, which is why the sync's import-parsing skip
could not see the dependency: nothing in the file names a module. That is worth
remembering — an availability check built on the import graph is blind to
`importlib.util.spec_from_file_location`, and the only signal was the runtime
`AttributeError` on a module the loader had cheerfully created.

Delete these if llm-routing is not going to carry a RouterArena submission.
Keep and re-point them if it is.
