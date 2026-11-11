---
id: ZCE-HOOKTEST-1
status: fixed on `fix/zce-hooktest-pythonpath` (test-only change; nothing to deploy)
---
## ZCE-HOOKTEST-1. The zero-Claude-edit hook tests ran another checkout's code in a worktree

- **Symptom (2026-11-11).** In a git worktree, `tests/test_zero_claude_edit_scope.py` failed 9 of 60 tests (the ones that run the UserPromptSubmit hook as a subprocess, e.g. `test_symbol_only_prompt_ambiguous_falls_through`), on origin/main and on the #908 branch alike, while CI passed them.
- **Cause.** `_run()` builds the subprocess env from PATH/LANG/LC_ALL/TMPDIR only, so it drops `PYTHONPATH`. The hook script under test then imports `llm_router` from whatever the interpreter has installed: in a worktree that is the editable install of the main checkout, not the tree under test. CI installs the checkout itself, so the mismatch never shows there.
- **Fix.** `_run()` sets `PYTHONPATH` to this tree's `src` (`ROOT / "src"`), the pattern `tests/test_compression_hooks_fire.py` already uses.
- **Test.** The file itself: run in a worktree with the sandboxed command, origin/main's version gives 9 failed / 51 passed, the fixed version 60 passed (2026-11-11, worktree `llm-router-wt-zcecls`).
