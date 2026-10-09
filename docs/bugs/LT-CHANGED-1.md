---
id: LT-CHANGED-1
status: fixed in `fix/local-task-changed-files-and-check-shell`
---
## LT-CHANGED-1. llm_local_task misreported changed_files and failed shell-syntax checks with a bare FileNotFoundError

- **Symptom (2026-10-08, ollama/qwen3-coder:30b).** `changed_files` was `[]` although `git status` showed ` M` on the edited file. `acceptance_check="HOME=$(mktemp -d) pytest ..."` died with `FileNotFoundError: 'HOME=$(mktemp'`.
- **Cause.** The os.walk snapshot stops at `_SNAPSHOT_MAX_FILES` in walk order, so files in the unseen tail were never compared. The check is argv-only (tests/test_local_task_authority.py forbids `shell=True`), so shell syntax became a literal program name.
- **Fix.** `_git_state` / `_take_state` / `_state_diff`, called inside `_run_task_serial` under `_AGENT_ENV_LOCK` (concurrent runs cannot claim each other's edits): `git status --untracked-files=all -- .`, content digests, the starting HEAD so a commit made during the run is still reported (`git diff since..HEAD`), renames as old and new path. A workdir that git ignores (non-git dir nested in a repo) falls back to the os.walk snapshot; git lost after the run yields `[]` plus `changed_files_note`. Gitignored files are deliberately not covered (unbounded cost on node_modules/.venv, and they are build output); stated in the docstring and in `changed_files_scope`. Shell syntax in a string check is detected with shlex on unquoted text only (a backtick inside quotes is a literal) and rejected naming the script workaround.
- **Test.** `tests/test_local_task_changed_files_and_check.py`: each case fails when its guard is removed (no `--untracked-files`, constant digest, no prefix strip, no rename entry, no backtick check, no `>`-family tokens, no lost-git note, no `-- .`, no commit union, no ignore detection).
