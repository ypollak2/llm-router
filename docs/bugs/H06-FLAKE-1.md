---
id: H06-FLAKE-1
status: fixed in `fix/h06-torn-read-flake`
---
## H06-FLAKE-1. test_the_old_pattern_really_does_tear flaked: the demonstrated race did not occur

- **Symptom (CI run 37914093633 attempt 1, job test (3.13), PR #366).** `tests/reliability/test_h06_quota_write_is_atomic.py::test_the_old_pattern_really_does_tear` failed with "the non-atomic writer produced zero torn reads ... assert 0.0 > 0.0".
- **Cause.** Test bug, not product. The test asserted that a free-running `write_text` loop tears at least one of 2000 concurrent reads. That is a scheduling race; on some runs the reader never lands inside the microsecond truncate-to-write window.
- **Fix.** The old pattern (open "w", then write) is now driven with a forced interleaving: the writer truncates, sets an Event, and parks (5 s bound) until the reader has read; the reader waits (5 s bound) for the Event, reads, then releases the writer. No loop, no timing. `test_atomic_rename_eliminates_torn_reads` is unchanged. No product change.
- **Test.** The same test file. Mutation: making the old-pattern write atomic (tmp + `os.replace` before the mid-write hook) makes `test_the_old_pattern_really_does_tear` fail; with the fix the file passes in 50 of 50 repeated runs.
