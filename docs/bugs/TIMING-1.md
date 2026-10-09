---
id: TIMING-1
status: fixed in `ci/timing-lane` (CI + test infra; production unaffected)
---
## TIMING-1. Tests whose verdict is wall-clock speed flaked in the parallel CI job

- **Symptom.** The main job runs `pytest -n auto --dist loadgroup --timeout=30`. Tests that assert on speed failed
  under that load with the product unchanged: `test_agt_a0_async_agent_loops::test_loop_lag_stays_under_100ms_during_a_30s_llm_act`
  (max lag 0.170 s and 0.207 s against 0.100 s), `test_agt_a0_local_task_serial::test_queued_local_task_budget_not_eaten_by_lock_wait`
  (0.7 >= 0.9), `test_codex_stream_and_reason_codes::test_timeout_keeps_partial_output`, several in `test_p09c_statusline_budget`.
  Earlier one-off conversions (P09-FLAKE-1/2, SLT-1, PROXY-OKF-FLAKE-1, A0-FLAKE-1) fixed single tests; the class kept producing new ones.
- **Cause.** A test whose pass/fail is "did it finish in N ms" measures the runner, not the code. `-n auto` stacks one worker per core
  plus subprocess-heavy tests, so any such test fails at a rate set by load. Relaxing thresholds hides real regressions and only moves the flake.
  A second defect hid in the same area: `tests/conftest.py` deleted every `LLM_ROUTER_*` variable at import, then read
  the perf opt-in variable (`..._RUN_PERF`, set in the timing job) in `pytest_collection_modifyitems`, so the documented perf opt-in could never take effect and
  `tests/qa/test_performance.py` (13 p95 budgets) never ran anywhere.
- **Fix (owner decision D-34 = A).** A `timing` marker (pyproject). Every test judged timing-dependent carries it (129 functions in 54
  files; see the PR for the list and for the tests deliberately left alone). CI: the `test` job runs `-m "not timing and ..."`;
  a new `timing (3.11)` / `timing (3.13)` job runs `-m "timing and ..." -p no:xdist` with the perf opt-in variable. A command-line `-m`
  replaces the addopts `-m`, so the addopts exclusions (`not slow and not requires_*`) are repeated in both expressions.
  No threshold, assertion or test body changed. `conftest.py` now reads the perf opt-in variable (`..._RUN_PERF`, set in the timing job) before the scrub. The release scripts
  run `pytest tests/` with no `-m`, so the timing tests still gate a release, serially.
- **Test.** `tests/test_timing_lane.py`: marker registered; ci.yml `-m` expressions equal pyproject's addopts exclusions and the
  timing job is serial; tagged-function floor (`MIN_TAGGED`, an empty lane passes everything); an AST guard fails when a new test
  asserts on a perf_counter/monotonic reading without the mark (allowlist `NOT_TIMING`, each entry with a reason). The guard is narrow:
  it does not see `time.time()` deltas, sleep ordering or short real timeouts, so those still depend on review.
