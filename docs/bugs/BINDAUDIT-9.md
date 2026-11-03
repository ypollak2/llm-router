---
id: BINDAUDIT-9
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-9. The outcome gate compared a used rate with the previous release's whatever method produced it

- **Symptom.** `docs/measurements/outcome_audit_history.json` holds 15.4.0's 0.229% (2 of 873 units), scored by the
  first audit; its transcripts are purged (30-day retention), so it cannot be re-scored. On the 16.0.0 window
  (n=564 units, `~/.rsi/research/primary-plan/v16/rel16-main/release-artifacts/outcome-audit/outcome_audit_16.0.0.jsonl`) the old audit reads 0 and the fixed one reads 10 of 467 (2.141%; overrides lifted, the same
  sessions) and 9 of 291 (3.093%; overrides applied), so a change of method moves the rate by 9 to 13 times that
  baseline. A gate that compares across the two passes or fails for a reason that is not the product's.
- **Cause.** History entries carried no method version, and `scripts/release/outcome_gate.py` compared with
  `previous[-1]`.
- **Fix.** The audit stamps `audit_version` (2) and its change list in the summary; the gate records it in every
  history entry (an entry without one is version 1) and compares only with the latest entry of its own version.
  When the only earlier entries are of another version it prints `AUDIT VERSION MISMATCH` and does what
  `--cross-version` says: `refuse` (the default: FAIL, nothing written), `rebaseline` (PASS without comparing; this
  run becomes the version's baseline) or `compare` (compare anyway, labelled CROSS-VERSION and recorded as such).
  The owner decided D-359 (REBASELINE): the 16.0.0 audit v2 number becomes the baseline, with no comparison to
  15.4.0, run once through `scripts/release/pre-release-verify.sh --outcome-cross-version rebaseline`, which forwards
  the flag to step 13. Without the flag a version mismatch still FAILS, so nothing passes silently. The D-358 report's
  outcome line is now reported, not gated (`scripts/release/bind_report.py`).

  The recorded baseline is sticky (the independent review of PR #816, S1, reproduced the first D-359 version letting a
  re-run with a 0 of 291 artifact and `--cross-version rebaseline` pass and overwrite 3.093%, n=291 units,
  `~/.rsi/research/primary-plan/v16/coord/rv-PR816-20261103.md`). Every history entry now records the release, the audit
  version, the rate, n and `artifact_sha256`. The baseline of a version is its earliest entry. A re-run for the release
  that holds it needs no flag and is compared with that entry: the same artifact passes with nothing written, a
  different one fails when its rate is lower and otherwise passes, and in neither case is the entry replaced.
  Replacing it takes `--force-baseline "<reason>"`, which records the values it replaced and the reason. A later
  release is compared with the release before it and a re-run replaces its own entry, as before. The ratchet compares
  point rates with no tolerance (at 9 of 291 a release with the same true rate scores lower with probability 0.454,
  review S1); that is unchanged and needs an owner decision.
- **Test.** `tests/test_outcome_gate.py::test_a_cross_version_comparison_is_refused_by_default`,
  `::test_rebaseline_passes_without_comparing_and_records_why`, `::test_compare_across_versions_is_labelled_and_can_fail`,
  `::test_after_a_rebaseline_the_next_release_compares_like_for_like`,
  `::test_entries_and_summaries_without_a_version_are_version_1`, and for the release script
  `::test_a_version_mismatch_fails_the_release_script_without_the_flag`,
  `::test_the_rebaseline_flag_is_forwarded_and_records_the_new_baseline` and
  `::test_the_compare_flag_is_forwarded_beside_skip_outcome`; for the sticky baseline
  `::test_the_entry_records_release_version_rate_n_and_the_artifact_hash`,
  `::test_a_rerun_of_the_baseline_release_needs_no_flag_and_changes_nothing`,
  `::test_a_worse_rerun_fails_under_every_policy_and_never_replaces_the_baseline`,
  `::test_a_better_rerun_passes_but_the_baseline_is_kept`,
  `::test_a_different_artifact_with_exactly_the_same_rate_passes_and_the_baseline_is_kept`,
  `::test_force_baseline_replaces_it_and_records_what_it_replaced`,
  `::test_a_baseline_recorded_before_hashes_existed_is_never_replaced_either`,
  `::test_a_later_release_keeps_the_older_rerun_rule_and_force_is_not_for_it` and
  `::test_through_the_release_script_a_rerun_needs_no_flag_and_a_worse_one_cannot_overwrite`; for the report
  `tests/test_bind_report.py::test_outcome_line_is_reported_not_gated_and_cites_d359`.
