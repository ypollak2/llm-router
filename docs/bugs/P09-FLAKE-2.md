---
id: P09-FLAKE-2
status: fixed in this change (test-only; production unaffected)
---
## P09-FLAKE-2. test_the_timing_wrapper_adds_under_5ms_per_call bounded the wrapper by wall-clock

- **Symptom.** main CI run 37926623089 (commit 03f274a5), job test (3.11): `AssertionError: (2.04, 68.3, 10.57)` (added unsampled ms, added sampled ms, median bare-bash ms) against a < 5 ms per-call bound; the sampled arm picked up 68 ms of scheduler noise on a loaded xdist runner.
- **Cause.** Test bug, not product. Min-of-40 interleaved wall-clock samples still cannot bound a few-millisecond cost when a whole runner is oversubscribed; the sampled arm has only 14 samples and includes two perl starts.
- **Fix.** The test now counts the commands the wrapper executes in the foreground (`bash -x`, fixed `PS4`), per setting (unset 5, `0` 6, `all` 17). That is load-independent; process starts stay pinned by `test_the_unsampled_path_starts_no_process_and_a_sampled_call_two_clock_reads`. The threshold was not raised; the wall-clock check is gone.
- **Test.** `tests/test_p09_hook_budgets.py`. With `sleep 0.01` or an `echo >> file` added to the wrapper block in `statusline-command.sh`, all three parametrized cases fail; restored, they pass.
