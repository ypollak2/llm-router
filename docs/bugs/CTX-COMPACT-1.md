---
id: CTX-COMPACT-1
status: fixed in `fix/ctx-compact-spill`
---
## CTX-COMPACT-1. Compaction deleted the oldest session-context events mid-session

- **Symptom.** In long sessions the live `projects/<hash>/session_context_<sid>.jsonl` was rewritten
  to its newest 150 lines whenever it passed 300 lines or 256KB, and the older lines were gone.
  Hundreds of events were lost in real long sessions, against R-CTX-4 (a session's context is never
  lost mid-session). Owner decision D-57: spill to the archive.
- **Cause.** `session_store._maybe_compact` kept `records[-_COMPACT_TO:]` and `os.replace`d the live
  file; torn/unparsable lines were silently dropped too.
- **Fix.** Before the replace, `_spill_to_archive` appends the dropped lines (size/count overflow and
  torn/unparsable lines, raw), in original order, to `archive/session_context_<sid>.jsonl` and fsyncs; if the spill
  fails the live file is left alone. TTL-expired records are NOT spilled: B-02 retention stays a
  physical delete (`LLM_ROUTER_PERSIST_TTL_DAYS`, default 30 days, so it never touches a live session). A crash between append and replace leaves a duplicate, never a
  loss. `archive_session` (SessionEnd) already appended to an existing archive and now guards a
  missing trailing newline (one-byte seek) and fsyncs before unlinking live, so the archive is spilled lines then live lines. `restore_session`
  prepends the archive and drops live lines byte-identical to an archive line (same ts and hash), so
  a crash overlap does not duplicate; the later compaction of a large restored file spills once into
  the fresh archive. The live file stays bounded, so hook read cost is unchanged.
- **Test.** `tests/test_session_store_spill.py`: 180 writes, bounded live, live + archive never
  shrinks and equals the written sequence; line and size triggers; TTL-expired deleted, not archived (B-02); torn line spilled; SessionEnd archive
  order; resume with no dup or loss across two end/resume cycles; crash between append and replace.
  On main (2f001d53) the spill tests fail, as the archive is never written.
