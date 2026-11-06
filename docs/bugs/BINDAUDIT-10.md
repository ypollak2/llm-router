---
id: BINDAUDIT-10
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; found by the independent review of PR #816 before merge)
---
## BINDAUDIT-10. The D-358 report could read PASS on a hidden denominator, a marker alone, a fail-open toll line or a sample the reader chose

- **Symptom.** `~/.rsi/research/primary-plan/v16/coord/rv-PR816-20261103.md` (review of 2bf462de, scratch repros) found
  five readings of the bar that were wrong or open to choice. (S2) Line 1 divided bound by the `delegated` rows only,
  so the runs that delivered nothing (`codex_blocked`, `codex_failed`; 269 of 317 Codex answers said blocked in
  `~/.rsi/research/primary-plan/v16/coord/BIND-R1-codex-handoff-20261103.md` section 2.8, n=317) never lowered it, and
  W1's wrapper invites a second identical Agent call (a re-issue), after which the first unit stayed bound while a
  Claude sub-agent redid the work. (S5) `bound` was a text-marker test, so a sub-agent report that quotes the wrapper
  was bound, and a change of W1's wording was invisible: 0 bound with no reason printed. (S6) The toll line let the
  router's ledger win over the transcripts (a hold the ledger missed read 0.00), answered INSUFFICIENT under the prompt
  floor even with a probe or 6 holds among those prompts, raised `TypeError` on `--min-prompts 0` with no prompts, and
  ignored the ledger's own probe rows. (S7) The line-2 hand check took any bound units the reader chose, so 20
  favourable entries passed it. (S9) A time without a zone (`--since 2026-11-03T09:50:20`) was read as local time.
- **Cause.** `scripts/release/bind_report.py` and `_codex_units` in `scripts/release/outcome_audit.py` were written
  against the 16.0.0 data, where every delegated unit is void: nothing there exercised a delivered result, a re-issue,
  a blocked run, a second source that disagrees or a sample larger than the file, so no test could fail on them.
- **Fix.** Line 1's denominator is every unit where the router emitted a deny carrying a Codex result. A unit is bound
  only when the result is the deny shape (`hookSpecificOutput.permissionDecision == "deny"` in the hook's own stdout,
  or the harness's `PreToolUse:Agent hook error` error result when the deny was honoured) AND carries both phrases of
  the W1 wrapper AND no Claude sub-agent was launched for that tool_use_id AND the same prompt (stripped, the router's
  own `prompt_fingerprint` rule) was not asked again within an hour (`codex_reissue`, `RESPAWN_WINDOW_S`). Beside the line the report
  prints `codex_blocked`, `codex_failed` and the re-issues as counts and shares, and a deny without the wrapper as
  "wording drift or a different deny". Line 2 is decided on a sample fixed in advance (the first 20 bound units by
  `sha256("D-358/hand-check/v1:<unit_id>")`); entries for other units are ignored and counted; it stays PENDING until
  every sampled unit is entered. Line 3 evaluates every source and the worst decides: a probe fails it at any n, a
  source whose Wilson lower bound is at or above the bar fails it at any n, and INSUFFICIENT is returned only when
  nothing shows the bar broken (a small source reading over the bar blocks a PASS); the ledger's probe rows count.
  `--since` and `--until` refuse a time with no zone.
- **Test.** `tests/test_bind_report.py::test_the_denominator_is_every_deny_that_carried_a_codex_result_and_the_runs_that_did_not_are_printed`,
  `::test_a_reissue_after_a_delivered_result_counts_as_not_bound_and_is_printed`,
  `::test_a_deny_without_the_codex_wrapper_is_named_as_wording_drift_not_hidden`,
  `::test_a_hand_check_of_units_outside_the_sample_does_not_count`,
  `::test_the_worse_of_the_ledger_and_the_transcripts_decides_and_both_are_shown`,
  `::test_a_probe_still_fails_the_line_below_the_prompt_floor`,
  `::test_a_rate_demonstrably_over_the_bar_fails_below_the_floor`,
  `::test_no_prompts_at_all_is_insufficient_and_does_not_crash_with_a_zero_floor` and
  `::test_a_time_without_a_zone_is_refused_rather_than_read_as_local_time`; in `tests/test_bind_audit.py`
  `::test_a_rendered_deny_with_the_wrapper_is_bound`, `::test_the_hooks_own_stdout_must_hold_the_current_deny_shape`,
  `::test_one_phrase_of_the_wrapper_is_not_the_wrapper` and `::test_the_markers_match_the_real_w1_wrapper_whenever_the_hook_has_it`
  (a change of W1's wording fails it once PR #818 is in the tree; until then `BIND_W1_HOOK` points it at the hook).
- **The mutation claim.** The first version of this PR said that mutations of the fixes were each killed by a test.
  The review ran 47 (n=47, scratch copy of 2bf462de) and 18 survived, among them the 3-day and 50-prompt bars
  (review S8). `tests/test_bind_thresholds.py` pins each threshold on both sides, and `tests/bind_mutations.py`
  re-runs every mutation (a baseline count first, then one edit at a time, never in the live tree) so the claim can be
  reproduced; the PR description carries its output.
