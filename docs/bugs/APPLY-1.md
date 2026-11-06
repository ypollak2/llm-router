---
id: APPLY-1
status: fixed in `feat/llm-apply-repair` (deploy: none beyond the PR; earlier fail-open rows stay)
---
## APPLY-1. Every successful llm_apply overwrite recorded a false `CHZ-FO-APPLY-TEMP-CLEANUP` fail-open

- **Symptom (2026-11-06).** A live smoke run of `llm_apply` (local build of `feat/llm-apply-repair`, isolated
  `LLM_ROUTER_HOME`, 2 calls) printed `fail_open code=CHZ-FO-APPLY-TEMP-CLEANUP exc=FileNotFoundError` after an
  edit that applied and passed. Nothing was wrong with the files; the fail-open ledger gained a false row, which
  feeds `doctor`/`status` fail-open counts.
- **Cause.** `apply_tool._write_batch` stages each new content in a temporary file and moves it into place with
  `os.replace`, which consumes the temporary name. The `finally` cleanup then unlinked every staged name,
  including the consumed ones, and recorded each `FileNotFoundError` as a fail-open.
- **Fix.** `_write_batch` records the paths whose staged file `os.replace` consumed and the cleanup skips them.
  Staged files for NEW FILE targets (placed with `os.link`, which keeps the name) and backups are still removed.
- **Test.** `tests/test_apply_tool.py::test_successful_overwrite_records_no_cleanup_failopen`: a successful
  overwrite records no `CHZ-FO-APPLY-TEMP-CLEANUP` and leaves no `.llm-apply-*` file. Reverting the fix makes it
  fail (mutation run 2026-11-06).
