---
id: FLAKE-LAV-1
status: fixed in this change (test infrastructure only; production unaffected)
---
## FLAKE-LAV-1. `_no_repo_mutation` blamed tests for a live Codex rewriting ~/.codex/config.toml

- **Symptom.** Under `pytest -n auto`, random tests (seen in `tests/test_local_agent_verify.py`, and in
  `tests/test_zero_claude_edit_scope.py`) fail or ERROR at teardown with
  `wrote installer artifacts outside its sandbox: ['home:.codex/config.toml']`; they pass alone. Reported
  2026-10-10 on PRs #406/#407/#408 for the verify file.
- **Cause.** Not wall-clock, not ruff/uvx, not shared tmp. The autouse guard `_no_repo_mutation`
  diffed the operator's REAL `~/.codex/config.toml` as `(bytes, mtime_ns)`. A running Codex app/CLI
  appends `[projects."<path>"]` trust tables to that file as it opens directories (operator copy: 1,246
  lines). Whichever test's teardown sampled it after a write was blamed, and the guard then "restored" the
  file from its stale snapshot. Same class as GH#88 (`~/.claude.json`) and GH#92 (`settings.json`).
  Evidence: the verify file alone, 15 workers, 120 CPU busy-loops, `uvx ruff` (no ruff on PATH): 1 teardown
  ERROR in 12 runs; verify + zero_claude_edit_scope: 1 ERROR in 3 runs on a different file, identical
  signature; neither file writes under HOME.
- **Second guard.** The session-wide `_codex_home_untouched_for_the_whole_session` fingerprinted the same file
  whole (mtime+sha256) and errored once per xdist worker at teardown (`ERROR ... teardown of <last test>`;
  first 'fix' attempt: 2 of 50 runs still errored, 2 and 15 errors). Its config.toml entry now uses the slice too.
- **Fix.** `_codex_config_slice` in `tests/conftest.py`: compare only `[mcp_servers.llm_router]` and the
  hook trust records (`hooks.state`), the only things the installer writes (`codex_host`); the file is
  `_REPORT_ONLY` (never restored over a live writer). Genuine escapes still show.
- **Test.** `tests/test_codex_config_guard_slice.py` (6 tests: project-table churn ignored; new/edited
  llm_router table and new trust record detected). Same harness after: see PR body for the 50-run count.
- **Not found.** No timing/ruff failure reproduced in the verify file; its 30 s deadlines held under 8x CPU
  oversubscription (runs took ~40 s vs ~8 s unloaded).
