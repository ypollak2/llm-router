---
id: LC-1
status: fixed in this change (test-only)
---
## LC-1. The "no slow callback" classifier test failed on a loaded CI runner

- **Symptom.** `tests/test_local_classifier.py::test_no_slow_callback_on_the_async_path_and_the_detector_works`
  failed on CI py3.11 (run 37811026623 attempt 2: `Executing <...> took 0.137 seconds`), on #319
  (68 ms) and earlier on #328 (docs only). None of those changes touched the classifier.
- **Cause.** No blocking call exists on the `classify_async` path (aiohttp only; read in full).
  The test asserted on asyncio debug `slow_callback_duration` = 50 ms, which is wall clock: a
  callback preempted by a busy runner counts. Reproduced locally (macOS, py3.11.15) with 40
  CPU burners and the threshold at 3 ms: the reported slow step was the test's own task
  resuming, 1 of 6 runs; with no load, 0 of 1.
- **Fix (test only).** `_LoopGuard` patches the blocking primitives (`time.sleep`,
  `Thread.join`, `urlopen`, blocking-mode socket calls, `subprocess.Popen`) to record and raise
  when called on the loop thread. The test proves the guard fires on the real sync
  `classify_local` (it joins a worker) and on an injected sleep, then that `classify_async`
  makes zero such calls. No clock, no threshold.
- **Test.** `test_no_blocking_call_on_the_async_path_and_the_guard_works`. Mutants, each red:
  `time.sleep(0.01)` in `_is_loaded`, a sync `urlopen` in `_classify`, a blocking
  `socket.create_connection` in `_is_loaded`. The old test passed a 10 ms sleep.
