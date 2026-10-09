---
id: CODEX-2
status: fixed in this change (#323 review follow-up)
---
## CODEX-2. Codex TOML removal could write an unparseable config.toml and dropped an indented user table

- **Symptom.** The #323 review fed an orphaned `[mcp_servers.llm_router.tools.x]` table holding
  `w = [\n[1, 2]\n]` (a valid nested array) to the cleanup. The line-based cut read `[1, 2]` as a
  header and returned `[1, 2]\n]`, which does not parse. Manifest replay of
  `[mcp_servers.llm_router]` wrote such a cut unchecked and printed `✓ Removed`. Separately, a
  user's indented `  [keep]` table right after an llm_router table was removed with it.
- **Cause.** `install._clear_codex_orphan_tables` re-parsed the cut with tomllib, but no test
  pinned that guard (a reviewer mutant that dropped it survived).
  `install_manifest._remove_toml_table` (MCP_TABLE branch) had no re-parse at all.
  `codex_host._HEADER_LINE` was anchored at column 0, so an indented header (legal TOML) did not
  end the table being dropped.
- **Fix.** `_remove_toml_table` re-parses the cut before it writes. If the cut does not parse, or
  an llm_router server entry would remain, the file is left unchanged and the line is
  `⚠ [mcp_servers.llm_router] left in <path>: <reason> ...; delete the ... tables by hand`.
  `_HEADER_LINE` accepts leading spaces or tabs.
- **Test.** `tests/test_codex_install.py::test_orphan_cleanup_refuses_an_edit_that_would_not_parse`
  and `::test_legacy_uninstall_leaves_a_nested_array_table_unchanged` (red with the tomllib
  re-parse removed from `_clear_codex_orphan_tables`);
  `::test_manifest_replay_refuses_a_cut_that_would_not_parse` and
  `::test_manifest_replay_reports_a_multiline_dotted_key_it_left` (red on main adf93a02);
  `tests/test_codex_host.py::test_remove_subtree_ends_at_an_indented_header` (space and tab; red on
  main).

