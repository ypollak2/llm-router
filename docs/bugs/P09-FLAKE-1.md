---
id: P09-FLAKE-1
status: fixed in this change (test-only; production unaffected)
---
## P09-FLAKE-1. Session-end "moved steps" tests bounded main() by wall-clock

- **Symptom.** main CI run 37918831984 (commit e9a62a00), job test (3.11):
  `tests/test_p09_session_end_bg.py::test_stop_runs_none_of_the_moved_steps_inline`
  failed with `AssertionError: main() took 2.1 s` under `pytest -n auto`.
- **Cause.** Test bug. The test already proves the property directly (`called == []`,
  `spawned == [1]`); the extra `elapsed < 2.0` used wall-clock as a proxy for "nothing slow
  ran inline". main() still does its own synchronous rendering, spend/trend/quota work, so on
  a loaded runner it can exceed 2 s with every moved step correctly absent. The sibling
  `test_stop_returns_while_a_5s_child_still_runs` had the same bound plus a fixed 5 s child
  that a slow main() could outlast.
- **Fix (test only).** Dropped both `elapsed < 2.0` assertions. The child in the second
  test now blocks on a release file (60 s cap) that the test writes after main() returns, so
  "main() did not wait for the child" no longer depends on speed. Hook files unchanged.
- **Test.** Same two tests. With a 2.5 s sleep injected into the inline quota sample, the
  old tests fail (`main() took 2.7 s`) and the new ones pass; with a moved step called inline
  from main(), the first test fails on `called == []` and the second fails when main()
  waits on the child (child killed by a 3 s subprocess timeout: "the detached child never ran").
