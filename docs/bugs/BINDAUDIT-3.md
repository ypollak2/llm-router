---
id: BINDAUDIT-3
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-3. The audit ignored session_kind_overrides.json, so sessions the owner marked non-organic were scored

- **Symptom.** `~/.llm-router/session_kind_overrides.json` names `b145b9e7` (the v16 coordinator, D-335) and
  `b9f04425` (a research session) as `research`. `~/.rsi/research/primary-plan/v16/rel16-main/release-artifacts/outcome-audit/outcome_audit_16.0.0.jsonl` (n=564 units) still held 182 of their units: 141 from
  `b145b9e7` (126 `agent_route_codex`, 15 `routed_mcp`) and 41 from `b9f04425` (182 of 564, 32% of the audit's
  population), in a number defined on organic sessions.
- **Cause.** Nothing in `scripts/release/outcome_audit.py` read the override file (0 matches for `session_kind` in
  the file at 5c056309); organic scope came from the transcript's entrypoint and cwd only.
- **Fix.** `read_session_overrides` reads `{sid: {kind, reason}}` the way the product does (a missing or malformed
  file or entry is no override; only a valid kind counts) and `is_organic(meta, override_kind)` applies the
  precedence "override, then the rules": a non-organic kind excludes the session, and `load_sessions` does not read
  its file; `organic` lifts the cwd/worktree/sandbox rules but not the structural ones (a human-less sdk entrypoint, a
  synthetic id). The summary lists the excluded sessions (`scope.session_kind_overrides`). The tag files
  `session_kind_<sid>.json` are not consulted: they record what the SessionStart hook classified from the same cwd
  and entrypoint, which the audit already reads.
- **Test.** `tests/test_bind_audit.py::test_overridden_session_is_not_read_and_its_units_are_gone`,
  `::test_override_file_reader_ignores_bad_entries` and `::test_is_organic_override_precedence`.
