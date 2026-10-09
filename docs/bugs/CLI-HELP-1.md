---
id: CLI-HELP-1
status: fixed in this change (#323 review follow-up)
---
## CLI-HELP-1. `llm-router uninstall --help` ran a real uninstall

- **Symptom.** During the #323 review, `llm-router uninstall --help` uninstalled. It edited the
  review clone's `.vscode/mcp.json` and `.windsurf/mcp.json`. A scan of every subcommand with
  `--help` (HOME and cwd in a temp dir) on adf93a02 found more. `uninstall` rewrote
  `~/.claude/settings.json` and the cwd MCP files. `update` and `onboard` rewrote the hooks.
  `init-claude-memory` wrote config. `budget`, `team`, `gain`, `doctor`, `summary`, `probe` and
  `explain-dashboard` wrote state DBs. `broker` wrote a secret and kept running. `gateway` tried to
  bind its port. `setup`, `init-policy`, `soak`, `tui`, `dashboard`, `routing-report` exited non-zero.
  `routing-health` and `test` ran their report and self-test instead of printing help.
- **Cause.** `cli.main` passed `args[1:]` to each handler. 21 of the 64 handlers never look for
  `-h`/`--help`, so the flag was ignored and the command ran. `okf`, `provider`, `semantic`,
  `sessions` and `benchmark` check only the first argument, so `okf gc --help` ran the gc dry run and
  `provider list --help` listed providers.
- **Fix.** `cli._subcommand_help` runs before dispatch. If a help flag is in the arguments and the
  subcommand is not one that handles help itself (`_OWN_HELP_ANYWHERE`: argparse or an
  all-arguments check; `_OWN_HELP_FIRST`: only `<cmd> --help`), it prints that subcommand's lines
  from the module usage text and exits 0. It does not import the subcommand. An undocumented name
  still gets the unknown-command error (exit 2).
- **Test.** `tests/test_cli_help_is_inert.py` runs every subcommand dispatched in `cli.main` (64)
  and every `<cmd> <sub>` form in the usage text (12) with `--help` in a subprocess. HOME and cwd
  are seeded with host configs that contain llm_router entries. Each run must exit 0, print usage,
  raise no traceback and change no file. On main adf93a02, 30 of the 76 cases fail (21
  subcommands, 9 nested forms).
  `test_the_cases_cover_every_subcommand` guards against an empty case list.

