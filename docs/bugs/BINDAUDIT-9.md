---
id: BINDAUDIT-9
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-9. The outcome gate compared a used rate with the previous release's whatever method produced it

- **Symptom.** `docs/measurements/outcome_audit_history.json` holds 15.4.0's 0.229% (2/873), scored by the first
  audit; its transcripts are purged (30-day retention), so it cannot be re-scored. On the 16.0.0 window the old audit
  reads 0 of 564 and the fixed one reads 10 of 467 (2.141%; overrides lifted, the same population) and 9 of 291
  (3.093%; overrides applied), so a change of method moves the rate by 9 to 13 times that baseline. A gate that compares
  across the two passes or fails for a reason that is not the product's.
- **Cause.** History entries carried no method version, and `outcome_gate.py` compared with `previous[-1]`.
- **Fix.** The audit stamps `audit_version` (2) and its change list in the summary; the gate records it in every
  history entry (an entry without one is version 1) and compares only with the latest entry of its own version.
  When the only earlier entries are of another version it prints `AUDIT VERSION MISMATCH` and does what
  `--cross-version` says: `refuse` (the default: FAIL, nothing written), `rebaseline` (PASS without comparing; this
  run becomes the version's baseline) or `compare` (compare anyway, labelled CROSS-VERSION and recorded as such).
  Which policy the release uses is an owner decision, still open; the default fails closed until it is made.
- **Test.** `tests/test_outcome_gate.py::test_a_cross_version_comparison_is_refused_by_default`,
  `::test_rebaseline_passes_without_comparing_and_records_why`, `::test_compare_across_versions_is_labelled_and_can_fail`,
  `::test_after_a_rebaseline_the_next_release_compares_like_for_like` and
  `::test_entries_and_summaries_without_a_version_are_version_1`.
