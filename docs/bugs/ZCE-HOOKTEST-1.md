---
id: ZCE-HOOKTEST-1
status: fixed on `fix/zce-hooktest-pythonpath` (test-only change; nothing to deploy)
---
## ZCE-HOOKTEST-1. The zero-Claude-edit hook tests ran another checkout's code in a worktree

- **Symptom (2026-11-11).** In a git worktree, `tests/test_zero_claude_edit_scope.py` failed 9 of 60 tests (the ones that run the UserPromptSubmit hook as a subprocess, e.g. `test_symbol_only_prompt_ambiguous_falls_through`), on origin/main and on the #908 branch alike, while CI passed them.
- **Cause.** `_run()` builds the subprocess env from PATH/LANG/LC_ALL/TMPDIR only, so it drops `PYTHONPATH`. The hook script under test then imports `llm_router` from whatever the interpreter has installed: in a worktree that is the editable install of the main checkout, not the tree under test. CI installs the checkout itself, so the mismatch never shows there.
- **Fix.** `_run()` sets `PYTHONPATH` to this tree's `src` (`ROOT / "src"`), the pattern `tests/test_compression_hooks_fire.py` already uses.
- **Import-origin evidence (2026-11-11).** The main checkout's venv interpreter (`.venv/bin/python`) with a stripped env (`env -i PATH=/usr/bin:/bin HOME=<scratch>`) resolves `llm_router.__file__` to `/Users/yaliandrona/Projects/llm-router/src/llm_router/__init__.py`; with `PYTHONPATH=<worktree>/src` it resolves to `<worktree>/src/llm_router/__init__.py`. `auto-route.py` imports `llm_router` (line 43) and inserts no `sys.path` entry of its own.
- **Test.** The file itself, from the worktree root (`T=$PWD`): `sandbox-exec -p '<deny :11434 :11435 :8797>' env PYTHONPATH=$T/src <main-checkout>/.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="-m 'not slow and not requires_ollama and not requires_api_keys and not requires_codex'" tests/test_zero_claude_edit_scope.py`. origin/main's version of the file gives 9 failed / 51 passed (e.g. `test_symbol_only_prompt_ambiguous_falls_through`), the fixed version 60 passed (2026-11-11, worktree `llm-router-wt-zcecls`). The outer `PYTHONPATH` reaches pytest only: `_run()` rebuilt the hook's env without it, which is the bug.
