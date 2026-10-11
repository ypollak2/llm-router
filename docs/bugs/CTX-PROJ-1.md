---
id: CTX-PROJ-1
status: fixed in `fix/ctx-project-per-session` (owner decision D-57; sessions already scattered keep their old shards)
---
## CTX-PROJ-1. One session id scattered across several `projects/<hash>/` context files

- **Symptom.** Session 639eda01 ended with 3 `session_context_<sid>.jsonl` files under 3 project
  hashes. Context prep, restore and the SessionEnd archive each read one shard and saw a fraction
  of the session.
- **Cause.** `session_store._project_id` hashed the repo root of `os.getcwd()` of the WRITING
  process. Bash-spawned test/E2E processes inherit the session id and call `router.route_and_call`
  (finalizer records events), and a `cd` into a git worktree changes the root, so each wrote under
  its own hash.
- **Fix.** `_project_id(project_root, session_id)`: for a known session id the project is the
  repo root of the session's cwd from `~/.llm-router/session_cwd_<sid>.json` (written by the prompt
  hook), pinned on first use in `session_project_<sid>.json` so later `cd`s cannot move it.
  Final semantics, in precedence order:
  1. `LLM_ROUTER_PROJECT_ID` wins over everything.
  2. A session's **pin outranks a caller-derived `project_root`**. MCP/gateway callers pass the
     session's LATEST cwd (`mcp_roots._root_from_session_pointer`); honouring it would read
     hash(worktree) while writers write hash(repo). An explicit `project_root` is used only when the
     session has no pin and no cwd record, or when no session id is given.
  3. No session id / no cwd record: the old behaviour (writer's cwd), nothing pinned.
  The pin is created atomically (temp file + `os.link`; the loser of a race re-reads the winner's
  pin, one retry), is not age-limited, and `_session_path`/`_archive_path` pass the session id so
  record, load, purge, merge, archive and restore resolve identically.
  **Deploy-time adoption.** When a pin is first CREATED for a sid, its existing shards in other
  project dirs (live and `archive/`) are folded into the pinned project via `merge_session_shards`
  (ts-ordered, de-duplicated, under the target's lock; sources kept as `*.premerge`). So a session
  running at deploy does not orphan its pre-deploy events. If the lock times out the merge is
  skipped and the shards stay put: run `llm-router sessions merge`. Events written before the first
  hook record exists (the first-write race) are adopted the same way once the pin appears.
  The merge holds each source's lock (sorted order, 0.5 s; a source it cannot lock is skipped and
  left in place) across read+rename, writes the target first and renames sources second (a crash
  leaves duplicates, which dedupe, never data only in `*.premerge`), keeps lines byte-for-byte
  (non-ASCII escapes intact, so restore's byte-identical dedupe still matches) and keeps
  unparseable lines raw. `record_event` re-resolves its path under the lock and retries once, so a
  writer that resolved the pre-pin path cannot recreate the old shard.
  **Cleanup.** `cleanup_old_sessions` deletes `session_project_<sid>.json` and
  `session_cwd_<sid>.json` older than `_TTL_DAYS` when no `session_context_<sid>.jsonl` exists
  (live or archive, any project dir). `*.premerge` / `*.merging` leftovers age out on the same
  TTL (B-02: no plaintext copy outlives retention).
- **Test.** `tests/test_ctx_proj_1_session_project.py` (19 tests): events from repo B, a worktree
  of A and a subdir land in one file under A's hash; pin survives the cwd record moving or ageing;
  pin beats a caller `project_root` (load_events and build_session_context); env override beats pin
  and root; no record / no session id is the old behaviour; first-write race is adopted; archive and
  restore use A's hash; pre-deploy live + archive shards from two other dirs are merged in order
  without loss or duplicates; pin write is atomic; race loser reads the winner; cleanup sweeps only
  orphaned, aged pins and cwd records. Also: premerge/merging retention sweep, held-source-lock skip, stale-path writer retry, raw-line preservation.
