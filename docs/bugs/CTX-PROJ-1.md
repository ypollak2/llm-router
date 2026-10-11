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
- **Fix.** `_project_id(project_root, session_id)`: for a known session id (and no
  `LLM_ROUTER_PROJECT_ID` / explicit root) the project is the repo root of the session's cwd from
  `~/.llm-router/session_cwd_<sid>.json` (written by the prompt hook), pinned on first use in
  `session_project_<sid>.json` (O_EXCL, first wins, not age-limited) so later `cd`s cannot move it.
  `_session_path` and `_archive_path` pass the session id, so record, load, purge, merge, archive
  and restore resolve identically; `context._resolve_context_identity` passes it too. First-write
  race: before the hook has written a record nothing is pinned and the writer's cwd is used (old
  behaviour); those early events stay in that shard.
- **Test.** `tests/test_ctx_proj_1_session_project.py` (8 tests): events from repo B, a worktree of A
  and a subdir land in one file under A's hash; pin survives the cwd record moving or ageing; env
  override and explicit root win; no record / no session id is the old behaviour; race behaviour;
  archive and restore use A's hash. On main, the tests that exercise the fix fail.
