---
id: CONTEXT-RESUME-1
status: fixed in `fix/context-store-survives-resume` (deploy: re-install hooks; session-end v28, session-start v33)
---
## CONTEXT-RESUME-1. The session context store did not survive `claude --resume` / `--continue`

- **Symptom (2026-10-10, P0.1-a live check).** Sessions A `bb0c0125` and B `8cc5ab6a` (10 turns each,
  `claude -p --resume`) never held more than one turn of events: 4 and 7 lines at archive. Single-process
  session C `de58cd8d` grew to 22. Evidence: `~/.rsi/research/primary-plan/v16/P0.1/live_d42_20261010/`.
  The P0.1-a criterion (>= 5 events, non-decreasing `wc -l`) is per session id, across resumes.
- **Cause.** Each resumed invocation is its own SessionStart..SessionEnd lifecycle under the same session
  id. The previous process's SessionEnd called `archive_session`, which unlinked the store, so the resumed
  session started empty.
- **Fix.** `session_store.archive_session` now moves the store to `projects/<id>/archive/` (appending to an
  earlier archive of the same id); new `restore_session` puts the archive back, ahead of anything already
  recorded live, and consumes it. `session-start.py` calls it only when the payload `source == "resume"`
  (startup, clear and compact do not). Stop still never archives; SessionEnd still removes the live store.
  Disk bound: one archive file per session id, at most the size of its live log (already compacted and
  TTL-purged per record); restore deletes it; `cleanup_old_sessions` sweeps archives not touched for 7 days
  (`_TTL_DAYS`, clock restarted at archive time). Trade-off: a session that is never resumed now leaves its
  scrubbed events on disk for up to 7 days instead of 0; with the session-context kill switch off nothing is
  retained. Residual: SessionEnd `reason` is not used, so a `/clear` session's archive lingers until the
  sweep; a resume from a different project directory looks in that project's archive (scope isolation,
  CHZ-AUD-024, is kept) and finds nothing.
- **Test.** `tests/test_context_store_survives_resume.py`: real hook mains, start -> turns -> Stop ->
  SessionEnd -> SessionStart(resume) x3, line counts 0, 2, 4, 6 non-decreasing, Stop does not archive, archive
  holds every event after each SessionEnd. Removing the `restore_session` call from `session-start.py` fails the line-count test
  (mutation, 1 of 10 tests); on `origin/main` all 10 fail (no `restore_session`).
