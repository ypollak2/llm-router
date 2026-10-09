---
id: SLT-2
status: fixed in this change (product: hardens llm_local_task; CI failure was the suite exposing it)
---
## SLT-2. `changed_files` attributed the router's own SQLite WAL to a task

- **Symptom.** main CI run 37926623089 (commit 03f274a5), job test (3.13):
  `tests/test_agt_a0_local_task_serial.py::test_overlapping_local_tasks_changed_files_attribution`
  failed with `['a.txt', 'home/knowledge/projects/test_overlapping_local_tasks_c0-ab119a97/semantic/index.sqlite-wal'] == ['a.txt']`.
  Job B (queued behind A) reported `[]`; the extra file landed in A's list.
- **Cause.** The conftest sets `LLM_ROUTER_HOME=<tmp_path>/home`, and the test's workdir is
  `tmp_path`, so the router's state dir (knowledge semantic index, SQLite WAL mode) sat inside the
  workdir. Something in the router wrote the index mid-run (timing dependent, hence a flake), and
  #358's `git status`/walk snapshot counted it as the task's edit. Neither task wrote it. Production
  has the same shape when `LLM_ROUTER_HOME` points into a project, or the project is the user's home
  dir and `~/.llm-router` is not gitignored.
- **Fix.** `local_task._own_state_rel` / `_in_own_state`: paths under `paths.llm_router_home()` are
  excluded from both the git state (status and committed paths) and the os.walk snapshot.
- **Test.** `test_router_state_dir_inside_workdir_is_not_attributed[True|False]` writes the WAL
  deterministically inside the run, git and non-git workdirs. Red before the fix with
  `['a.txt', 'home/knowledge/semantic/index.sqlite-wal']`, green after.
