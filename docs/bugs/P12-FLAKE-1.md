---
id: P12-FLAKE-1
status: fixed in `fix/nested-suite-timeout-flake`
---
## P12-FLAKE-1. test_operator_settings_do_not_reach_the_suite hit the 30 s pytest-timeout under load

- **Symptom (independent full run on main 5b26824c, `pytest -n auto --dist loadgroup --timeout=30`, load ~4-7).** `tests/test_n_suite_ignores_the_operators_settings.py::test_operator_settings_do_not_reach_the_suite` failed at 30.02 s. Alone it passes 5/5 (12.8 s cold, ~1.3 s warm). CI uses the same flags.
- **Cause.** Test bug, not product. The test spawns a nested pytest (interpreter start, conftest import, two real tests). Its cold cost is within a factor of 2.5 of the suite-wide `timeout = 30`, so CPU contention from the other xdist workers pushed it over.
- **Fix.** `@pytest.mark.timeout(180)` (>10x cold) and `@pytest.mark.xdist_group("nested_pytest")`; the inner `subprocess.run` timeout is 170 s so a hang surfaces the child's output. The assertion is unchanged.
- **Test.** The same file. Mutation: deleting the `LLM_ROUTER_*` strip loop in `tests/conftest.py` makes it fail (both victims fail inside the nested run); restored, it passes. Under two concurrent full `-n auto` suites, 20 of 20 runs pass; the nested run's wall time reached 41 s (over the old 30 s limit) and passed. The pre-fix variant also passed 20 of 20 on that load (max 30 s wall), so the original failure was not reproduced locally; the fix rests on measured headroom, not on a reproduced failure.
