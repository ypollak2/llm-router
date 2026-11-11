---
id: MIDNIGHT-1
status: fixed in `fix/midnight-1` (test-only; no production change)
---
## MIDNIGHT-1. test_p34b4b_session_end_scans failed in the first minutes after 00:00

- **Symptom.** PR #899, job test (3.13), 2026-11-11 00:09:43Z: 5 tests in
  `tests/test_p34b4b_session_end_scans.py` failed (`KeyError 'total'`, `KeyError 'count'`,
  `assert 2 == 1`, `session_cost_ratio` missing, `0 == 3`). The file passed 19/19 on main
  at 00:10-00:11Z, so a re-run hid it.
- **Cause.** The test helpers stamped rows `now - 1 minute` (and up to 7 minutes for the
  cache-trend rows). The readers under test (`ledger_view.resolve("today")` and the hook's
  `date(timestamp, 'localtime') = date('now', 'localtime')`) open "today" at local midnight.
  In the first minute after midnight a 1-minute-old row is yesterday, so the "today" counts
  were 0 (and the S row counted as an extra earlier day). Reproduced with a POSIX `TZ` whose
  local clock reads 00:00:19: the same 5 tests fail. Production is consistent (stored UTC,
  local-day window, one definition); no production change.
- **Fix.** `_ts(..., same_day=True)` in the test file clamps a row under a day old to no
  earlier than local midnight. Used by `_decisions_db` and by the cache-trend test. The
  session-start tests keep exact offsets.
- **Test.** `tests/test_p34b4b_session_end_scans.py`, run on 2026-11-11 with a POSIX `TZ`
  chosen so the local clock read a given minute after midnight (n=1 run per clock setting,
  source: this PR's `TZ=XXX<offset>` runs). Before the fix: 5 of 19 failed at local 00:00 and
  1 of 19 (cache trend) at local 00:05. After: 19 of 19 passed at each of 6 clock settings
  (00:00 to 00:30). The independent review (`~/.rsi/research/primary-plan/v16/coord/REVIEW-MIDNIGHT-902-20261111.md`)
  reproduced it: old file 5 failed at local 00:00:20, 1 failed at 00:05:30; new file 19 of 19 at
  00:00:00, 00:00:01, 00:00:20, 01:00, 12:00, 23:59:30 and 23:59:58. To re-check, run the file with
  a `TZ` that puts the local clock at 00:00:20 (POSIX `TZ` offsets are west of UTC, so `XXXhh:mm:ss`
  means local = UTC minus that offset):

  ```bash
  TZ=$(python3 -c 'import time;s=int(time.time())%86400;d=(s-20)%86400;h,r=divmod(d,3600);print(f"XXX{h:02d}:{r//60:02d}:{r%60:02d}")') \
    python -m pytest -q tests/test_p34b4b_session_end_scans.py
  ```

  `TZ=<that value> date +%T` prints `00:00:20`. Run it within a few seconds of computing the
  value, or the clock moves past the window.
