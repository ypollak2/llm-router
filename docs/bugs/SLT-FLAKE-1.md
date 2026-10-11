---
id: SLT-FLAKE-1
status: fixed in this change (test-only; production unaffected)
---
## SLT-FLAKE-1. test_both_prints_full_line_then_fast_line raced the detached segment build

- **Symptom.** PR #399 CI run 38055298338, job test (3.13), head 4594f720 (2026-10-10, no statusline
  change in the PR): `tests/test_statusline_tick.py::test_both_prints_full_line_then_fast_line` failed with
  `assert '🛡  smart · segments pending' == '🛡  smart · ✗ no provider'`. It also failed once in a local
  full-suite run on PR #395 and passed in isolation.
- **Cause.** Test bug, not wall-clock. The test rendered `both` in a fresh `tmp_path` home, then rendered the
  full line a second time and compared the two first lines. The first render of a home only starts the
  detached segment build and prints `segments pending` (STATUSLINE-COLD-1). If the build wrote
  `statusline_seg_<sid>.kv` before the second render, that render showed the health segment instead, and
  the lines differed. Every failure seen has that one direction (`segments pending` first, a built segment
  second). On the owner's machine (15 cores, isolated HOME), with the test parametrized 60 times plus
  `test_statusline_tick.py`, `test_p09c_statusline_budget.py` and `test_statusline_default_full.py` under
  `pytest -n auto`: 37 failures in 183 runs (3 rounds: 6, 21, 10), all
  `'🛡  smart · segments pending' == '🛡  smart · ○ idle'`; 1 in 20 runs in isolation.
- **Fix (test only).** The test renders once, waits for the cache file with
  `tests/statusline_prime.wait_for_cache` (a condition, not a sleep; the builder writes via `os.replace`),
  then renders `both` and the full line from the same cache. It also asserts the `both` line is not
  `segments pending`, so the comparison is between two built lines. No threshold or timing mark changes:
  the race is against a detached process, not against the clock, so the test stays in the parallel lane.
- **Test.** Same test. After the fix the same harness passed 183 of 183 runs (3 rounds, 253 passed each).
  A mutant that appends a segment to line 1 in `both` mode still fails it
  (`'🛡  smart · ○ idle · MUTANT' == '🛡  smart · ○ idle'`).
