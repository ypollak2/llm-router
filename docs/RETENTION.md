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

## `examples.jsonl` (P3.6-a, R-CLS-5, R-RTE-7; B-02 applies)

| Item | Value |
|---|---|
| Rows | `text_sha` (16 hex chars of the normalised prompt, `prompt_key`), `source` (`prefix`, `redo`, `rule`), `label` (0 or 1: does the prompt need a frontier model), `ts`. Forced-prefix rows keep their original `forced`, `decided`, `door` fields. **Prompt text is never stored.** |
| User-derived embeddings | A forced-prefix row written by the proxy or MCP may also hold `vec` (L2-normalised float32, base64) and `model`: **an embedding of the person's own prompt**, written only when `LLM_ROUTER_COMPLEXITY_KNN` is on and a kNN artifact exists. It is not the shipped corpus, and an embedding is partly invertible, so treat it as derived from the prompt. It is computed in a worker thread, never on the event loop. The hook writes the row hash-only (no network call on its path), so a hook-door row cannot join the index. |
| Hash only | A `redo` row (a human `r` press: only the proxy ledger's `text_sha` is left) and a `rule` row (an "always local" rule sentence: not a prompt, and an embedding of it would pollute the kNN neighbourhood) never carry `vec`. They are evidence, not index material. |
| Writers | `prefixes.record_example` (hook, proxy, MCP: a forced `local:`/`claude:`/... prefix), `receipt_band` signal `redone` through `examples.record_redo` (a `k` press is not recorded, it is not a test), `user_rules.add_rules` for a `local_only` rule. Files 0600, directory 0700. |
| Kept | Rows newer than 180 days (`examples.MAX_AGE_S`). Older rows are **deleted from the file** on the next append and on every `llm-router learn` (readers also ignore them). At most about 2 MiB (`examples.MAX_BYTES`): past that the oldest half is dropped on the next append. |
| Reader | `llm-router learn` (`classifier_learning.apply`). Accepted examples that carry a `vec` are **copied into `complexity_knn.json`** (embeddings and 0/1 labels; the applied `text_sha` list, at most 5,000, and `examples_added_total` sit in its provenance so a row is applied once). The artifact has no TTL, and deleting `examples.jsonl` does not remove what an accepted update already put there. |
| Remove | `llm-router learn --purge-user-examples` rebuilds the artifact without the last `examples_added_total` rows (the ones `learn` appended; `build_complexity_knn.py` writes a fresh artifact with no `learning` provenance, so the tail rule holds) and keeps the previous artifact as `complexity_knn.json.prev`. Then delete `examples.jsonl` (else the next `learn` applies those rows again) and `complexity_knn.json.prev` (it still holds them). `rm ~/.llm-router/complexity_knn.json*` and rebuilding from the shipped corpus also works. |
| Failure | A failed write is recorded with `failopen` code `LR-FO-EXAMPLE` (or `LR-FO-PREFIX-EXAMPLE` for a prefix row); routing is never blocked. |
| Concurrency | `llm-router learn` takes an exclusive lock (`complexity_knn.json.lock`) for the whole run, writes through a unique temp file, and writes `.prev` only for a base artifact that is still the one it read. |

## `classifier_agreement.jsonl`, `classifier_learning.jsonl` and `classifier_conservative.json` (P3.6)

| Item | Value |
|---|---|
| `classifier_agreement.jsonl` | D-255. One row per turn on which the rules AND the L3 kNN both answered (`proxy/tiers.with_complexity_knn`, only when the kNN runs): `ts`, `rules_tier` (the rules' complexity), `knn_tier` (`frontier` or `standard`), `source`. No text, no hash, no embedding. Two generations of 1 MiB (`capped_log`, about 24,000 rows), so older rows go first; a heavy user's 28-day baseline may then be shorter than 28 days. |
| What it measures | Rules-vs-kNN **agreement** on the frontier boundary, not ground-truth accuracy. With the kNN off, no rows are written and `status` says "not measured (kNN off, n=0)". |
| Fallback | `classifier_shadow.jsonl` (the killed LLM classifier, D-121) is read only when there are no agreement rows and that file exists; the line is then labelled "LLM-shadow fallback". |
| `classifier_learning.jsonl` | One row per `llm-router learn` run: `ts`, `status`, `reason`, number of examples, and the replay `n`, exact-match and under-route of the current and candidate index. No text, no hashes. Two generations of 256 KiB (`capped_log`). |
| `classifier_conservative.json` | The conservative-mode switch: `active`, `checked_at`, `since`, `drop_pts`, window sizes. Written by the status and `kpi` checks; ignored once `checked_at` is 7 days old. |
| Replay set | Named by `LLM_ROUTER_LEARNING_REPLAY`; owned by the person, never copied or logged by the router. Every row needs a `text_sha` (the held-out guard); a file with a row that lacks one is rejected whole. |
