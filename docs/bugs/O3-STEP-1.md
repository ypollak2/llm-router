---
id: O3-STEP-1
status: fixed in this change (`fix/o3-null-step-turns`)
---
## O3-STEP-1. O3 counted rows with no `step_class` as human turns

- **Symptom.** Found in the review of #367 (`docs/bugs/P09-11.md`): `offload_share.py` still treated any
  row whose `step_class` is not `continuation` as the start of a human turn, so null-step rows were
  turns in n, in the Haiku/local/claude split and, as turn boundaries, in the 2-turn redo window.
  The figures of that effect on a live ledger were not re-measured in this change.
- **Cause.** Three sites tested `step_class != "continuation"`: `_Conversation`'s default `begins`,
  `build_units.begins_turn` and the transcript-role counters. The proxy now always labels a row
  (`proxy/server.py` `step_kind(body)`), so a null label occurs only on rows written before GE1
  (#313); `subagent_first` rows (the proxy's own label) were turns too unless a transcript said
  sidechain.
- **Fix.** A turn is a non-side-call row with `step_class == turn_first`, minus transcript sidechain
  calls (the KPIS.md O3 definition: "minus sub-agent first calls"). Null-step and `subagent_first`
  rows stay per-call units but start no turn and end no redo window. They are counted, never guessed:
  `o3.excluded.no_step_class` and `o3.excluded.step_subagent_first`, and printed on the O3 line. Keys
  are only added. The small-n rule (`too few to tell` below `MIN_N`) is unchanged.
- **Rule.** A turn boundary is defined by the label it claims (`step_class == turn_first`), never by
  "not the other labels": a null label is not a kind (same rule as P09-11 for G1_proxy).
- **Test.** `tests/test_offload_share_alignment.py`:
  `test_null_step_and_subagent_first_rows_cannot_start_a_turn`,
  `test_null_step_rows_do_not_close_the_redo_window_of_a_turn`,
  `test_scorecard_prints_the_null_step_exclusion_and_keeps_the_small_n_rule`. All three fail on
  6d0b7535. Fixtures: `tests/_o3_fixture.py` now defaults `proxy_row(step=...)` to `turn_first`, and
  `test_local_session_accepted.py` used the non-existent label `turn-first`, now `turn_first`.
