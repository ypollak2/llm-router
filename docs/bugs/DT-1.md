---
id: DT-1
status: fixed in this change (deselected-test sweep)
---
## DT-1. The sdist shipped two test directories the "/tests/" exclude never covered

- **Symptom.** `pytest -m ""` on main 26468d34 failed
  `tests/test_sdist_excludes_quarantined_tests.py::test_sdist_does_not_ship_the_active_test_suite`:
  the sdist contained `integrations/pi/tests/*` (3 files) and
  `src/llm_router/mods/llm-router-receipt/tests/band.test.ts`. CI never saw it: the test is
  `slow`-marked and `addopts` deselects `slow`.
- **Cause.** `"/tests/"` in `[tool.hatch.build.targets.sdist] exclude` is anchored to the repo
  root, so it matches only `tests/`. Same anchoring lesson as `/agents/` and
  `/_quarantined_tests/`, third instance.
- **Fix.** Two exact-path excludes in `pyproject.toml`. An unanchored `tests/` was rejected: it
  matches at any depth and could strip a package directory.
- **Test.** `uv build --sdist`, then `tar tzf dist/*.tar.gz | grep -c /tests/`: 4 before,
  0 after; `llm_router/agents/session.py` still present. The slow test above covers it when run
  with `-m ""`.

