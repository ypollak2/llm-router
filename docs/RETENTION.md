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
