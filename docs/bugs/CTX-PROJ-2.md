---
id: CTX-PROJ-2
status: fixed in `fix/ctx-proj-2` (follow-up to CTX-PROJ-1, review of PR #422)
---
## CTX-PROJ-2. First-event race strands early events; CLI merge did not lock its target

- **Symptom.** (1) A session's very first `record_event` resolved its path before the hook's
  `session_cwd` record existed; the record appeared before the under-lock re-resolve. Pin creation
  and adoption then ran while this writer held the OLD shard's lock; adoption's 0.5 s source lock
  timed out on that same lock file, skipped the source, and never retried. Events stayed in the
  old shard until `llm-router sessions merge`. (2) `merge_session_shards` without `target=` (CLI)
  took no target lock, so a writer's append during the replace could be lost.
- **Cause.** (1) `_session_path` (which may create the pin and adopt) was called from inside
  `exclusive_lock(old shard)`. (2) Only sources were locked.
- **Fix.** (1) `record_event` resolves the path once more BEFORE taking the lock; if the pin is
  created there, adoption runs with no lock of ours held, and the under-lock re-resolve is a pure
  read. Chosen over (a) skip-re-resolve (breaks the CTX-PROJ-1 stale-path retry: a writer would
  recreate the old shard after another process pinned) and (c) retry marker (new persistent state,
  events stranded meanwhile). Smaller than both: one resolve call moved. Comment on the lock warns
  against creating a pin/adopting while holding the same lock file. (2) The CLI path takes the
  target lock (5 s) first; adoption (`target=` given) already holds it.
- **Test.** `tests/test_ctx_proj_2_first_event_and_cli_lock.py`: first-event race (first resolve
  patched, cwd record written right after) ends with all events in the pinned shard, no manual
  merge; CLI merge blocks while the target lock is held and keeps a concurrent append. Both fail on main.
