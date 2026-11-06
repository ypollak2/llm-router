---
id: BIND-HOLD-1
status: fixed in `bind/mcp-holds` (deploy: `llm-router install --force` for hook auto-route 79); residual holds on longer prompts are by design, see Known limits
---
## BIND-HOLD-1. Short acknowledgements, continuations and compaction summaries held tools

- **Symptom (own replication of the BIND-R2 count with the loaders of `scripts/release/outcome_audit.py`; window
  2026-10-04T09:50:20Z to 2026-11-03T09:50:20Z; 23 organic sessions, 652 counted prompts; counts only, no text read into a report).**
  40 unique `Routing directive BLOCKED` tool results (39 Bash, 1 Write), 6.1 per 100 prompts (R2 counted 51 in 509 prompts, 10
  per 100, over its 9 sessions; the sets differ, the shape is the same). The prompt before each block had 3 words (9
  times), 4 (3), 5 (4), 6 (8), 7 (2), 9 (1), 10 (2): 29 of 40 at ten words or fewer. Nothing at 11, 12 or 13 words. Then 14,
  18, 24, 33, 55, 55 and 59 words (7), and four compaction summaries ("This session is being continued from a previous
  conversation ...", 1,863 to 2,294 words each). Of the 95 probe calls BIND-R2 labelled throwaway (appendix A, class e, llm and
  route doors), 70 followed a prompt of ten words or fewer, none one of 11 or 12 words, and 9 followed a compaction summary.
  After the F59 exemption (2026-10-18), 11 blocks fall on or after 2026-10-19 in this window: 8 follow a prompt of ten words
  or fewer (one six-word "keep going" variant held Bash for a generate, a delegate and a query turn),
  2 a compaction summary, 1 a 33-word shell snippet. 83% of the MCP calls come from two coordination sessions.
- **Cause.** The prompt hook wrote a hold (pending route) for every routed prompt whose class the mode enforces,
  whatever its size or nature. The exemptions that existed (F59 coordination Bash in smart mode, F69 acknowledgements,
  F82 pasted shell commands, the `_STRICT_ACK_RE` bypass) are content patterns: each new phrasing (a 6-word "keep going"
  variant, a 5-word wait instruction, a 7-word go-ahead) fell through to a class that holds, and a
  machine-written compaction summary classifies as code, research or delegate like any long prompt. A hold for a prompt a cheaper model
  has nothing to do with only invites a toll (BIND-TOLL-1).
- **Fix.** `llm_router.hold_policy.hold_exempt_reason`, called by `auto-route.py` before the pending state is written:
  a prompt of ten words or fewer (`SHORT_PROMPT_MAX_WORDS`, the largest value before the gap above) or one that starts
  with "This session is being continued from a previous conversation" writes no hold. The route hint stays, worded as
  advice ("nothing held"), and the hook records an `exempt` row. A user-forced `local:` or `codex:` prefix is an explicit
  request to route and keeps its hold, and so do `hard` and `strict` (the explicit opt-in to hold everything, where the
  `llm_act` redirect of short executable asks such as "push to main" is a deliberate design; a trivial call does not
  release it, so in `hard` it costs a retry, not a toll (`strict` has no retry valve, see BIND-TOLL-1). That sentence
  was false for a prompt with fewer than three content words
  in the first version of this fix, which released it on any routing door; the review of PR #817 at 549d02a9 found it and
  `hold_policy.forwards_owner_prompt` now judges such a prompt by whether the call restates all of its content words,
  `tests/test_w2_enforce_route_holds.py::test_a_reply_ok_probe_does_not_release_a_tiny_prompts_hold`). The rule is applied in `smart` mode only: the modes that never block
  (`suggest`, `soft`, `advise`, `shadow`, `off`) have no hold to take away and keep their text byte for byte. A stale hold from the previous turn is consumed as before. Every prompt writes a
  `prompt` row first, so the denominator is complete (`hold_events.jsonl`, `docs/RETENTION.md`).
- **Known limits.** (1) A reader-side exemption in `enforce-route.py` (drop a pending file whose `original_prompt` is short)
  was built and removed: 164 of the existing tests seed short `original_prompt` fixtures and expect a hold, and a pending
  file only outlives the fix across a partial upgrade. (2) The 7 holds after prompts of 14 to 59 words stay, by design: with
  BIND-TOLL-1 they cost at most one retried call. In this window, if every one of them remained, that is 7 of 652 prompts,
  1.1 per 100; two of the seven are `coordination` holds from before F59 that smart mode no longer holds for Bash, and one
  is a `<bash-input>` record that was probably never a prompt the hook saw. This is an in-sample projection on the window the rule was
  read from, not a result: the bar (under 1 BLOCKED per 100 prompts) is measured on organic sessions after deploy.
  (3) The threshold is a line drawn through one machine's gap, not a judgement of the prompt: a self-contained
  question of ten words ("explain the difference between a mutex and a semaphore in python") is answered by Claude
  without a hold in smart mode, even with `LLM_ROUTER_QA_ROUTING=on`. A narrower rule (exempt a short prompt only when the
  existing context, coordination, acknowledgement or continuation detectors claim it) was measured and not taken: those
  detectors claim 25 of the 29 short held prompts of the window, so 4 would still hold, which with the 7 longer holds is
  11 of 652 prompts (1.7 per 100) against 7 (1.1 per 100) for the plain rule. (4) The owner's resolved enforcement mode was
  not read for this work (`llm-router doctor` reports it). It is inferred to be `smart` because coordination holds stopped
  after F59, which only exempts smart mode; if it is `hard`, this exemption does not apply to it. (5) No hold on a short
  question matches the owner decision of 2026-10-01 for development Q&A recorded in `auto-route.py` (local-model answers
  acceptable 3-17% of the time against 60-100% for Sonnet, `~/.rsi/research/routing-experiment-2026-10-01`).
- **Test.** `tests/test_w2_auto_route_holds.py` runs the real prompt hook as a subprocess:
  `test_a_short_prompt_of_a_held_class_writes_no_hold` (3 prompts),
  `test_the_ten_word_boundary_on_the_real_hook` (10 words: no hold; 11: a hold),
  `test_a_compaction_summary_writes_no_hold` (with its control: the same text without the marker holds),
  `test_control_a_real_request_still_writes_a_hold`, `test_a_forced_local_prefix_keeps_its_hold_even_when_short`,
  `test_stale_holds_do_not_survive_into_a_short_prompt`, `test_every_prompt_is_counted_even_when_nothing_else_is_written`,
  `test_the_ledger_carries_no_prompt_text`; the existing tests that seeded a short prompt and expected a smart-mode hold
  were changed to use a longer one or to assert the new outcome per mode
  (`tests/test_coordination_introspection_not_routed.py::test_executable_coordination_prompt_keeps_its_directive_and_pending_route`,
  `::test_a_longer_executable_coordination_prompt_keeps_its_pending_route_in_both_modes`,
  `::test_qa_routing_env_restores_the_previous_behaviour`, `tests/test_qa_not_routed.py` (same name),
  `tests/test_context_aware_routing.py::test_self_contained_routable_prompt_still_writes_pending_state`);
  `tests/test_w2_hold_policy.py::test_short_prompts_never_hold` (11 shapes),
  `::test_the_boundary_is_ten_words`, `::test_a_compaction_summary_is_not_a_request`,
  `::test_no_prompt_is_no_evidence_so_not_exempt`. Mutation check on unmodified origin/main 5c056309: 13 of the 15
  tests in `test_w2_auto_route_holds.py` fail; the `local:` preservation test and the degraded-install text test pass.
  Also `test_hard_and_the_modes_that_never_block_are_unchanged_for_a_short_prompt` (hard, strict and suggest keep the
  pending state and the text they had) and `test_the_claude_prefix_is_the_explicit_user_bypass` (smart, hard, strict).
