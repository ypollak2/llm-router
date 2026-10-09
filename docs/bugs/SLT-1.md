---
id: SLT-1
status: fixed in this change (test-only; production unaffected)
---
## SLT-1. Statusline tick test read the refresh marker between create and write

- **Symptom.** CI py3.11 run 37910363552 attempt 1 (PR #363, unrelated change):
  `tests/test_statusline_tick.py::test_hung_refresh_never_delays_a_tick_and_is_started_once`
  failed with `AssertionError: exactly one refresh started across 5 ticks` /
  `assert '' == 'x'`. Zero characters, not two: no extra refresh was started. The
  `BrokenPipeError` earlier in that log is unrelated: `test_classify_local_is_bounded_by_its_budget`
  (tests/test_decision_classifier.py) answers after 2 s on purpose, the client has
  already timed out, and the fake server's write hits a closed socket; that test passed.
- **Cause.** The fake refresher runs `open(marker,'a').write('x')`; `open` creates the file
  empty and the `x` lands only when the file object is flushed. The test polled
  `marker.exists()` and then read, so a slow runner read `''` in that window. The product
  is correct: ticks run one after another, and `maybe_refresh` writes the stamp before it
  forks, so the next tick sees the stamp and starts nothing.
- **Fix (test only).** The test polls (15 s deadline) until the marker has content, then
  asserts it is exactly `x`. `src/llm_router/statusline_tick.py` is unchanged.
- **Test.** Same test. With a 1 s sleep injected in the fake refresher between `open` and
  `write`, the old test fails with the exact CI error (`'' == 'x'`) and the new one passes.
  With the product's stamp check disabled, the new test still fails (`'xxxx' == 'x'`, 5 of 5).
  The file passes 20 of 20 runs (126 tests each) in an isolated HOME.
