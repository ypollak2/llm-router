# Retention of router-owned history files

Companion to the B-02 notes (`docs/bugs/CTX-COMPACT-1.md`, `docs/bugs/CTX-PROJ-1.md`: session
copies do not outlive their TTL). This file lists the history files that have a retention rule.

## `quota_history.jsonl` (F107)

| Item | Value |
|---|---|
| Rows | `ts`, `five_hour_pct`, `weekly_pct`, `updated_at`, `five_hour_resets_at`, `source`. Quota percentages only, no prompt content. |
| Writers | `statusline_tick.maybe_append_history`, one row per 5-minute slot, also called from the default-layout refresher (`statusline_segments.append_history`). About 288 rows a day. |
| Kept | Rows with `ts` newer than 30 days (`statusline_tick.HISTORY_RETAIN_S`), and at most 20,000 rows (`HISTORY_MAX_ROWS`), newest first. |
| Why 30 days | Longest reader window is 14 days (`quota_rate.median_pp_per_active_hour`, `statusline_snapshot.BURN_DAYS`). 30 days adds a margin for `llm-router kpi --quota-burn --since/--until`, whose coverage block reads the same file for an absolute window; a window older than 30 days reports no history rows. |
| Prune | `statusline_tick.prune_history`: atomic rewrite (temp file, fsync, `os.replace`) under the writers' slot lock, at most once a day (mtime of `.quota_history.pruned`), run only by the background refresher (`statusline_segments.append_history`, `statusline_snapshot.refresh`), never by the synchronous status-line tick. Lines that are not JSON objects with a numeric `ts` are dropped by the rewrite. |
| Failure | A failed prune is recorded with `failopen` code `F107-HISTORY-PRUNE`; the file is left as it was. |
| Reads | `quota_rate` reads only the tail of the file it needs (backward scan in 256 KiB blocks). |

## `active_days.jsonl` (P3.7, S7, D-26 option A)

| Item | Value |
|---|---|
| Rows | `day` (local calendar date, `YYYY-MM-DD`) and `install_id` (16 random hex characters generated locally on the first row). Nothing else: no prompt text, no session id, no path. Nothing is sent anywhere. |
| Writer | `active_days.mark_active`, called from `quota_samples.append_session_sample` (the session-start background child and the Stop sample). Per session: one `stat` of the file (a file whose mtime is today already holds today's row, so no read); the tail read and the append happen at most once per day. Concurrent first writers share one install id (`active_days.id`, created with `O_EXCL`) and same-day duplicate rows are collapsed by the trim. Files are forced to mode 0600 on every write, not only on create. |
| Kept | One row per day, at most 400 rows (`active_days.MAX_ROWS`, about 13 months). Past that the file is rewritten atomically (temp file, fsync, `os.replace`), keeping the FIRST row (the install day and id) and the newest 399. The trim runs when the file is larger than 400 full rows (53 bytes each, `active_days._ROW_BYTES`). |
| Install day | The first marker day, not the operating-system install time: no earlier dated record exists. |
| Reads | `llm-router kpi --retention` and the last line of `llm-router kpi --quota-burn`: install day, active days, day 14, and "active on day 14" as yes / no / not yet (n/a before the first marker). Strict reading: "yes" needs a row dated exactly day 14; "not yet" holds while today is on or before day 14; active on day 15 but not day 14 is "no". The population rate (S7 70% across users) is LATER (16.1). |
| Failure | A failed write is recorded with `failopen` code `P37-ACTIVE-DAY`; the session start is never blocked. |
| Delete | Removing the file resets the install day and id. |
