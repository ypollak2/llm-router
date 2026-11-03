---
id: BINDAUDIT-1
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-1. A Codex unit was paired with the latest Agent call before its ledger row, so about a third were paired with a neighbouring call

- **Symptom.** `~/.rsi/research/primary-plan/v16/rel16-main/release-artifacts/outcome-audit/outcome_audit_16.0.0.jsonl` (window 2026-10-04T09:50:20Z to 2026-11-03T09:50:20Z, n=406 `agent_route_codex`
  units): `~/.rsi/research/primary-plan/v16/coord/BIND-R1-codex-handoff-20261103.md` section 4 joined each delegated row to the PreToolUse:Agent hook run that produced it and compared
  that with the audit's pairing: 208 correct, 98 paired to a neighbouring Agent call (98 of 317, 31%), 2 nested, 9
  none. Re-measured on the same 317 delegated rows (`~/.llm-router/north_star_units.jsonl` and the transcripts,
  2026-11-03): 204 correct, 97 neighbouring, 16 unpaired. Of the 35 units BIND-R1 followed by hand
  (`tests/fixtures/bind_w3/r1_codex_sample.json`) the audit paired its own call for 26. The headline (0 used) did not
  depend on it, but every per-call fact built on the pairing (which model ran, whether it finished, what the parent
  did next) belonged to the wrong call in a third of the units.
- **Cause.** `_codex_units` took the latest unclaimed Agent call issued within `AGENT_MATCH_WINDOW_S = 900` seconds
  before the ledger row (`scripts/release/outcome_audit.py` before this change). The hook writes its row when it
  finishes (median 47 s, max 289 s over n=317 delegated rows, `~/.rsi/research/primary-plan/v16/coord/BIND-R1-codex-handoff-20261103.md` section 0), so any Agent call issued after the hook
  began and before it ended is nearer to the row than the call the row belongs to. The ledger carried no
  `tool_use_id` to say which.
- **Fix.** `_pair_codex_row` in `scripts/release/outcome_audit.py`: by the ledger's `tool_use_id` when the row has
  one (BIND W1 writes it); otherwise by the PreToolUse:Agent hook run that followed the row (the harness's own record
  of which call it ran the hook for): the nearest unclaimed run in [-2 s, +20 s], a run whose call has the row's
  `subagent_type` first. The old rule remains only as a last resort, with its window cut to the hook's own 320 s
  timeout. On the 16.0.0 window 317 of 317 delegated rows pair that way (offset median 0.39 s, max 5.5 s), 317 of 317
  agree with the ledger's `subagent_type` and with the sub-agent's `agent-*.meta.json` (`toolUseId`, `agentType`),
  and 35 of 35 of the BIND-R1 sample pair to the call the hand table names (`scripts/release/audit_agreement.py`, n=35,
  one model's unblinded hand table; the pairing rule was written from those rows, so this is an in-sample check).
- **Test.** `tests/test_bind_audit.py::test_old_rows_are_paired_by_their_hook_run_not_by_the_latest_call` (two
  Agent calls 1 s apart; the 900 s rule picks the later one for both rows) and
  `::test_new_rows_are_paired_by_tool_use_id` (the row names an older call than the nearest). Against the real
  window, by hand (the suite may not read the owner's home): `python3 scripts/release/audit_agreement.py --until
  2026-11-03T09:50:20Z` reports 35 of 35 (`::test_agreement_cli_on_a_synthetic_corpus` covers its wiring), and
  `tests/test_bind_thresholds.py::test_a_hook_run_joins_a_row_from_two_seconds_before_to_twenty_seconds_after`,
  `::test_the_last_resort_pairing_reaches_330_seconds_back_and_two_seconds_ahead` and
  `::test_two_hook_runs_pair_by_subagent_type_before_distance` pin the three windows and the type tie-break.
