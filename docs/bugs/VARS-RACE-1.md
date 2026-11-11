---
id: VARS-RACE-1
status: fixed in `fix/conftest-vars-race` (test-only; no production change)
---
## VARS-RACE-1. The module-leak fixture crashed with "dictionary changed size during iteration"

- **Symptom.** PR #904, CI run 42980583688, job test (3.13), 2026-11-11 08:04Z.
  `tests/test_n_suite_ignores_the_operators_settings.py` failed. The nested pytest it starts errored in
  `tests/test_audit_v5_fixes.py::TestBudgetEnforcement::test_budget_exceeded_cleanup_releases_reservation` with
  `RuntimeError: dictionary changed size during iteration`, raised at `tests/conftest.py:1936`. #904 changes only a
  docstring and a comment.
- **Cause.** The autouse fixture `_no_module_state_leak` calls `_module_state_fingerprint`, which iterated
  `vars(llm_router)` with a Python-level generator. A thread still running from an earlier test that imports an
  `llm_router` submodule adds an attribute to the package between two steps of that loop.
- **Fix.** Iterate `vars(pkg).copy()`. The copy runs in C and holds the GIL, so no thread can change the dict
  during it.
- **Test.** `tests/test_conftest_vars_race.py` adds and removes 64 module attributes on `llm_router` from a thread,
  with the switch interval at 1 µs, while the fingerprint runs 3000 times. It removes every attribute before it returns.
  On 2026-11-11 (CPython 3.11.15, sandboxed): the old loop failed 2 of 2 runs, and the fix passed 10 of 10.
