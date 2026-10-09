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
  `o3.excluded.no_step_class`, `o3.excluded.step_subagent_first` and `o3.excluded.step_side_call` (rows
  labelled `side_call` whose `tier_reason` is not, which `side_call_excluded` never saw), and printed on
  the O3 line. Exception to "keys only added": `o3.excluded.subagent_first` keeps its name but now counts
  only `turn_first` rows the transcript marks sidechain (before: every non-continuation row it marked
  sidechain). The small-n rule (`too few to tell` below `MIN_N`) is unchanged.
- **`subagent_first` is a live label** (`proxy/steps.py` `step_kind`, `proxy/server.py` ledger), so this
  is not an old-rows-only change. The proxy infers it from a tool list with no Agent/Task launcher, so a
  main session run with that tool disabled (settings, `--disallowedTools`, SDK/headless) is labelled
  `subagent_first` too. O3: such a row is a turn only when the transcript join places its message on the
  main thread (role `turn` or `meta`); with no join, orphan, continuation or sidechain it is not.
  `proxy/haiku_guard.py` has no transcript join, so there `subagent_first` rows are never turns. Reviewer
  probe on current-style rows (2 `subagent_first` rows between a Haiku turn and an escalation): redo
  trigger base n=40 k=0 turns=160, head n=40 k=10 turns=80; Haiku-served `subagent_first`-only rows
  n=40 -> n=0. Direction: sub-agent calls no longer push an escalation out of the 2-turn window. This is a
  live behaviour change of a safety trip (the Haiku guard's `redo` trigger).
- **Rule.** A turn boundary is defined by the label it claims (`step_class == turn_first`), never by
  "not the other labels": a null label is not a kind (same rule as P09-11 for G1_proxy).
- **Test.** `tests/test_offload_share_alignment.py`:
  `test_null_step_and_subagent_first_rows_cannot_start_a_turn`,
  `test_null_step_rows_do_not_close_the_redo_window_of_a_turn`,
  `test_scorecard_prints_the_null_step_exclusion_and_keeps_the_small_n_rule`,
  `test_subagent_first_label_is_a_turn_only_with_a_main_thread_join`,
  `test_step_class_side_call_without_the_side_call_reason_is_counted`; and in
  `tests/test_proxy_haiku_guard.py` `test_subagent_first_rows_are_not_turns_for_the_redo_trigger`
  (base `(k, n) = (0, 40)`, head `(40, 40)`). All fail on 6d0b7535. Fixtures: `tests/_o3_fixture.py` now defaults `proxy_row(step=...)` to `turn_first`, and
  `test_local_session_accepted.py` used the non-existent label `turn-first`, now `turn_first`.
