---
id: LINEAGE-PERF-1
status: fixed in `fix/lineage-perf-mechanism` (test-only; production unaffected)
---
## LINEAGE-PERF-1. The LineageStore.record p95 budget measured the runner's fsync latency and flaked in the timing lane

- **Symptom.** The first day the timing lane ran (#383, 2026-10-09),
  `tests/qa/test_performance.py::test_perf_lineage_record_single_row_under_5ms` failed twice with the product unchanged:
  `p95 64.77ms exceeds budget 11ms` (PR #384, run 37952157913 attempt 1, timing 3.13) and `p95 34.61ms` (PR #381, run
  37955223121 attempt 1, timing 3.11). Both reruns passed. The test name said 5 ms; the assertion said 11 ms.
- **Cause.** `record()` opens a fresh sqlite connection per call and commits one transaction in WAL with the default
  `synchronous=FULL`; closing the last connection checkpoints and deletes the WAL. On Linux (strace, 203 records) that is
  about 5 fdatasync and 2 unlink per record, so the cost is the disk: p50 0.11 ms on tmpfs, about 1.2 ms on a virtualised
  Linux disk, 0.5 ms on macOS (weak fsync). Heavy local I/O contention (4 dd+sync loops) only reached p95 6 ms, so the CI
  failures were rare stalls of tens of ms on shared runners. The test takes n=50 and `p95 = sorted[47]`, the third-worst
  sample, so three stalls (6%) fail it. The 5 ms in the name came from the 13.0.0 upstream sync (#40); no PRD, PLAN-v16
  or QA-strategy number backs it (the strategy doc the file cites is not in the repo). 11 ms is the last CI-spike loosening.
- **Fix (test only; owner chose "mechanism + p50").** (a) `tests/qa/test_lineage_record_mechanism.py`, not marked `timing`,
  pins what one `record()` does: 1 `sqlite3.connect`, 1 `close`, 1 BEGIN and 1 COMMIT, statements exactly
  `PRAGMA busy_timeout`, `PRAGMA journal_mode = WAL`, one INSERT, 0 `os.fsync`/`os.fdatasync`, and a fresh `_connect` connection
  is `synchronous=FULL` + WAL. (b) The wall-clock test is renamed `test_perf_lineage_record_p50_budget` and asserts
  `p50 <= 11 ms` over the same 50 samples, still `timing`. (c) Provenance is in its docstring. No threshold other than the
  statistic changed; `src/` is unchanged.
- **Test.** The new file. Mutations run against `lineage_store.py` and not committed: an extra `_connect(...).close()` in
  `record()` fails 2 tests (`record() opens a different number of connections`); an extra `os.fsync` after the JSONL write
  fails 2; `PRAGMA synchronous=NORMAL` in `_connect` fails 2 (`unexpected statements`, synchronous != 2). Unmutated: 3 passed.
  Limit: SQLite's own fsyncs run in C and cannot be counted portably, so they are pinned by their determinants
  (connections, transactions, sync level, journal mode), not counted directly. A deliberate change to those numbers
  (persistent connection, `synchronous=NORMAL`) is a durability/concurrency decision and must update the constants.
