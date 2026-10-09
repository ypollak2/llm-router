---
id: STATUSLINE-COLD-1
status: fixed in `fix/statusline-cold-render`
---
## STATUSLINE-COLD-1. The first render of a session built the segment cache inline (live p95 234 ms)

- **Symptom.** After PR #370 the statusline measured p95 22-38 ms in `scripts/statusline_wall.py` (n=220) but live
  `hook_latency` rows gave p95 234 ms (n=47, 8 rows > 100 ms, 2 sessions). A cold smoke render for a new session measured 195 ms.
- **Cause.** `statusline-command.sh` computed a missing per-session cache synchronously (`_seg_run sync`: interpreter probe
  plus the segments build, 80-200 ms). The bench missed it two ways: the verdict mode (`cold`) renders from a warm cache, and the
  `first` mode reused one session id, so the `.statusline_seg_sync_<sid>` rate-limit marker (not in `CACHE_GLOBS`) survived and every run
  after the first skipped the sync call and printed `segments pending` (measured 17 ms; with a new session per run, p50 79 ms,
  p95 82 ms, and p95 108 ms with the interpreter memo also dropped).
- **Fix.** A missing cache starts the detached builder (`_seg_run`, single-flight: one launch per 5 s per session plus the
  builder's own lock) and the line renders without segments plus the existing `segments pending` marker. No inline build remains;
  the `sync` mode of `_seg_run` is deleted. The harness `first` mode now uses a fresh `session_id` per run, `first_ever`
  also drops the interpreter memo, and the sync marker and locks are in `CACHE_GLOBS`.
- **Test.** `tests/test_p09c_statusline_budget.py::test_the_first_render_returns_while_the_cache_build_is_still_running`
  holds a stand-in builder open and asserts the render returned while its pid is alive and that two more renders start no second
  builder (mechanism, not wall time); `test_the_render_path_has_no_synchronous_builder_call` pins the script text;
  `test_the_first_render_prints_a_degraded_line_and_the_cache_lands_in_the_background`. Reverting the script to main fails all three.
  Tests that assert what the segments say now render twice via `tests/statusline_prime.py` (start the build, wait for the file).
