---
id: LEDGERMEM-1
status: fixed in `fix/statusline-ledger-memory` (deploy: nothing to restart; the next status line refresh uses it)
---
## LEDGERMEM-1. The status line refresher read the whole proxy ledger and several transcripts into memory every minute

- **Symptom (2026-11-09).** The detached refresher (`llm-router statusline --refresh`, about once a minute)
  peaked at 1.4 to 2.2 GB resident on a 48 GB Mac that also holds a 27 GB local model, and swap grew.
  Measured on the real state, `/usr/bin/time -l ~/.llm-router/live/.venv/bin/python -I rss.py <src> <now>`
  (`compute_scorecard(days=7, now=<fixed>, call_mix_sessions=True, routing_detail=True)`, the call the
  refresher makes), n=3 runs per arm on 2026-11-09: before 1556529152, 2041004032, 2233286656 bytes
  maximum resident set size; after 800489472, 800849920, 801177600 bytes. Ledger measured:
  `~/.llm-router/proxy_calls.jsonl`, 111855798 bytes, 69538 rows (61682 older than 7 days, 2468 from 2 to 7
  days, 5388 from the last 2 days; `dist.py`, counts only, no row text read).
- **Cause.** Three whole-file reads, each holding the text and the parsed rows together:
  1. `proxy/ledger.read_rows()` did `read_text()` + `splitlines()` + `json.loads` of every line, and
     `compute_scorecard` called it with no window. `read_rows()` alone: 888 MB peak resident before, 148 MB after
     (`resource.getrusage` ru_maxrss in one process, same ledger).
  2. `usage_outcome.judge_recent` called `read_text()` on every Claude Code transcript of the last 7 days only to
     test for `"router__"`, twice per scorecard (D3 and the O3 inputs).
  3. `ledger_view._proxy_rows` and `_jsonl_stat` (the ledger line) did the same on `proxy_calls.jsonl`.
- **Fix.** Read line by line. `read_rows(slim_before=, slim_keep=)` keeps every row but cuts a row older than
  `slim_before` to the keys in `slim_keep`. The scorecard needs old history for exactly these, so it keeps
  `ts`, `session_id`, `session_kind`, `tier_policy_version` and the G3 field keys (`_SCORECARD_OLD_KEYS`):
  `KindIndex` (a session's stamped kind), `_g3_completeness` and `_g3_late_first_seen` (the first `ts` each key
  appeared), `_o3_since_view` (the first row of a policy version). Every other consumer reads
  `[window start, now]` only, so the cut is `min(window start, now - 2 days)`. Row count, order and every `ts`
  are unchanged; rows without a numeric `ts` are never cut. The narrower window the task asked about was not
  used: dropping old rows would change the answers above.
  Not fixed here: the rest of the 0.8 GB is `northstar.units` (transcript and debug-log parsing,
  about 0.5 GB by sampled RSS); it is a separate change.
- **Test.** `tests/test_kpi_slim_history.py::test_scorecard_is_identical_to_the_full_read` builds a synthetic
  ledger (history, boundary rows, a session stamped only in history, a G3 key that exists only in history,
  undated rows, torn lines) and compares `compute_scorecard` with the old full-read path in 5 windows.
  Dropping `session_kind`, `tier_proposed` or `tier_policy_version` from `_SCORECARD_OLD_KEYS` turns it red.
  On the real ledger, one fixed `now`: the whole card (`dump.py`, both arms, n=1 each) differed in no path
  between old and new, and none between two old runs. Also: `test_the_history_is_really_slimmed`,
  `test_read_rows_without_arguments_is_unchanged`, `test_router_tool_scan_matches_the_whole_file_check`,
  `test_jsonl_stat_matches_the_whole_file_version`, `test_ledger_view_proxy_rows_keeps_only_the_window`.
