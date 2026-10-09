---
id: CODEX-1
status: fixed in this change (#323)
---
## CODEX-1. Codex refused to start: `invalid transport in mcp_servers.llm_router`

- **Symptom.** Codex exited at startup with `~/.codex/config.toml:443:14 invalid transport
  in mcp_servers.llm_router` (owner's machine, reported in #323). Codex raises this when a
  `[mcp_servers.<name>]` table has neither `command` nor `url`.
- **Cause.** Codex writes `[mcp_servers.llm_router.tools.<x>]` itself when the user picks
  "always allow" on a tool the installer did not pre-approve. Manifest replay
  (`install_manifest.apply_uninstall`) removed only the tables it had recorded, so Codex's own
  tool table outlived the server table and left an `llm_router` server with no transport.
  A binary-less `install` and the legacy `uninstall_host_integrations` fallback did not look
  for that state either.
- **Fix.** `codex_host.remove_toml_subtree` removes the whole `mcp_servers.llm_router`
  subtree (any quoting or spacing in the header, and dotted keys under `[mcp_servers]` or at
  the root); comment lines right above the next kept table stay. `has_orphan_mcp_tables`
  detects the no-transport state for manifest replay, binary-less install, legacy uninstall
  and `doctor`. Install and uninstall re-check after removal: `✓ Removed orphaned ...` is
  printed only when the server is gone and the file parses; otherwise a `⚠ ... delete them by
  hand` line names what is left (a dotted key whose value spans lines is not cut).
- **Test.** `tests/test_codex_install.py::test_uninstall_takes_tool_tables_codex_wrote_itself`
  and `::test_install_without_binary_clears_orphaned_tool_tables` (both red on main 75700df4:
  the orphan table stays). Also `::test_legacy_uninstall_clears_orphaned_tool_tables`,
  `::test_*_reports_orphans_it_could_not_remove`,
  `tests/commands/test_doctor.py::TestRunDoctorHost::test_run_doctor_host_codex_orphan_tool_tables_say_invalid_transport`
  and the `remove_toml_subtree` tests in `tests/test_codex_host.py`. Each of 10 mutants
  (one per code path, e.g. the legacy cleanup or the doctor branch disabled) turns one of
  them red.

