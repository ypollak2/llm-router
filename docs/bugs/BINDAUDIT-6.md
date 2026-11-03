---
id: BINDAUDIT-6
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-6. Fixed-reply probes and status views sat in the used-rate denominator

- **Symptom.** `~/.rsi/research/primary-plan/v16/coord/BIND-R2-mcp-unused-20261103.md` section 2: of the n=158 routed_mcp rows of `~/.rsi/research/primary-plan/v16/rel16-main/release-artifacts/outcome-audit/outcome_audit_16.0.0.jsonl`, 82 asked for a fixed
  reply ("Reply OK", "Reply with one word: ready"; 40 of the 96 not-meant-to-be-relayed calls were made under a
  hold whose text says "call ANY llm_* tool") and 7 were router status views. None has anything to relay, so
  none can be used, and all 89 were in `attempted`.
- **Cause.** Every `mcp__llm_router__*` tool_use was an attempted unit in `scripts/release/outcome_audit.py`; the
  audit had no label for a call that carries nothing to relay.
- **Fix.** Label `not_applicable`, outside `attempted` (`is_probe_prompt`, `NON_CONTENT_TOOLS`; signals
  `probe_fixed_reply`, `hold_release_probe` when issued in the turn after a `Routing directive BLOCKED` deny, and
  `status_view`). The rule is the prompt's shape, not its outcome: one line that starts with "reply" (under 150
  characters) or ends with "reply OK" (under 200); a question mark makes it a real question and a second line means
  it carries context (0 of the 82 has a newline; median 11 characters, longest 129). On the 16.0.0 population
  (n=158 rows, `scripts/release/audit_agreement.py`) it finds 82 probe rows (R2: 82) and 7 views, 89 in all (R2: about
  89); attempted falls to 61 (R2: about 61). Because leaving the denominator raises the rate,
  `scripts/release/outcome_gate.py` accepts a `not_applicable` row only with a signal from `NOT_APPLICABLE_SIGNALS`,
  recounts `attempted` without them, prints how many there are, and the summary carries
  `not_applicable.used_rate_if_included`.
- **Test.** `tests/test_bind_audit.py::test_probe_prompt_rule`,
  `::test_summary_excludes_not_applicable_from_the_denominator` and
  `::test_error_on_a_probe_is_not_applicable_and_error_on_a_task_is_unused`; the gate side in
  `tests/test_outcome_gate.py::test_not_applicable_rows_are_outside_the_denominator`,
  `::test_a_headline_that_counts_not_applicable_as_attempted_fails_the_recount` and
  `::test_not_applicable_needs_an_allowed_reason`.
