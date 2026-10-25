---
id: EDITSURV-UTC-1
status: fixed in #733
---
## EDITSURV-UTC-1. Edit survival read git `--since` as local time, so redo was over-counted on hosts east of UTC

- **Symptom.** `tests/test_northstar.py::test_edit_ledger_applied_and_survived_is_used`
  failed under `TZ=Asia/Jerusalem` and passed under UTC, so CI (UTC) stayed green. On a host
  east of UTC an edit that survived was scored as redo; west of UTC a redo could be missed.
- **Cause.** `edit_survival._commits_touching_since` formatted the UTC `since_epoch` with no
  zone and passed it as `git log --since=`. Git reads a zone-less date as local time, so the
  cut-off moved by the host's UTC offset: east of UTC it landed in the past and commits made
  just before the edit were counted as after it. Present since #181 (moved into the package
  in #214).
- **Fix.** The `--since` string carries an explicit `+0000` (#733). No other git date
  argument exists in `src/`, `scripts/` or `hooks/` (independent review of #733). Persisted
  rows written before the fix on a non-UTC host are not recomputed: `north_star_units.jsonl`
  `routed_edit` units, the quality-breaker rates read from them, and
  `outcome_audit_<release>.jsonl` edit rows may be biased (size not measured).
- **Test.** `tests/test_edit_survival_utc.py::test_commit_count_is_zone_independent` and
  `::test_judge_row_verdict_is_zone_independent` over UTC, Asia/Jerusalem, America/New_York
  and Pacific/Kiritimati (6 of 12 FAIL on 2c5a876e).
