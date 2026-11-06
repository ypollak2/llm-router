---
id: RELSH-2
status: fixed in `bind/release-sh-portable` (PR #815; release tooling only, production unaffected)
---
## RELSH-2. `release.sh` exited 127 on macOS because GNU `timeout` does not exist there

- **Symptom (2026-11-03).** The test step (step 2) of the dry run ran `timeout --foreground "$_test_timeout" uv run pytest ...`.
  On the maintainer's Mac neither `timeout` nor `gtimeout` is on PATH (`which -a timeout gtimeout` prints nothing; Darwin 25.5.0,
  `/bin/bash` 3.2.57), so the step exited 127 before a single test ran. Step 2 reports every exit other than 0, or 124 with
  `[100%]` in the output, as `Tests failed` and stops the release.
- **Cause.** The script assumed GNU coreutils. macOS does not ship `timeout`; Homebrew's `coreutils` installs it as `gtimeout`.
  Only Linux hosts had it, and `release.sh` is not run in CI. A second defect sat in the same step: the limit defaulted to 300 s,
  while the local suite takes about 70 minutes single-threaded on the maintainer's Mac (the maintainer's 2026-11-03 dry run;
  not re-measured by this change). On a host that had `timeout`, a healthy run would have been killed at 300 s with exit 124 and no
  `[100%]` marker (it prints only on completion) and would have read as a failed release.
- **Fix.** `run_with_timeout` in `scripts/release/release.sh`; the test step calls it. It uses `timeout`, else `gtimeout`, else a
  perl fallback (perl is in the macOS base system) with the same exit codes: 124 when the limit hit (SIGTERM, then SIGKILL after
  `RELEASE_TIMEOUT_KILL_AFTER` seconds, default 10, and still reported as 124), the command's own status otherwise (128+N when a
  signal killed it), 127 when the command is not found, 126 when it cannot be run. Like `--foreground`, only the command itself is
  signalled; `uv run` forwards the TERM to the process it started (probe on 2026-11-03: `uv run --frozen python -c 'time.sleep(60)'`
  under the fallback with a 3 s limit exited 124 and left no process behind). Step 2's rule "exit 124 with `[100%]` in the output
  is a pass" is unchanged. The default `RELEASE_TEST_TIMEOUT` is now 7200 s.
- **Test.** `tests/test_release_sh_portable.py`: `::test_fallback_passes_the_commands_own_exit_status_through`,
  `::test_fallback_exits_124_when_the_limit_is_hit`, `::test_fallback_escalates_to_sigkill_when_term_is_ignored`,
  `::test_fallback_reports_a_missing_command_as_127`, `::test_fallback_reports_128_plus_n_when_a_signal_killed_the_command`,
  `::test_a_hung_run_that_printed_100_percent_is_still_exit_124_with_the_marker_captured`,
  `::test_timeout_is_the_first_choice_and_gets_the_foreground_flag`, `::test_gtimeout_is_the_second_choice`,
  `::test_the_test_step_uses_the_wrapper_and_keeps_its_124_rule`, `::test_the_default_test_limit_covers_a_local_run_of_the_suite`,
  and `::test_fallback_exit_codes_match_gnu_timeout_where_both_exist` (skips on a host without GNU `timeout`, such as a stock Mac;
  its 5 cases passed in a Debian 13 container with GNU `timeout` 9.7 on 2026-11-03). Mutation check: replacing the perl branch with
  the old direct `timeout --foreground` call fails 17 tests on a stock Mac (run on 1f91ba4e).
