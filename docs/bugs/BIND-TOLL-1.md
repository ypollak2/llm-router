---
id: BIND-TOLL-1
status: fixed in `bind/mcp-holds` (deploy: `llm-router install --force` for hooks auto-route 79 and enforce-route 25, then reconnect with `/mcp`)
---
## BIND-TOLL-1. The hold's block text told the model to call ANY `llm_*` tool to clear it, so it sent throwaway "Reply OK" probes

- **Symptom (BIND-R2, the v16 coordinator's read of the 158 `routed_mcp` units of the outcome audit, window
  2026-10-04T09:50:20Z to 2026-11-03T09:50:20Z, 150 distinct tool calls, 9 sessions of 22; one unblinded reader, no second rater).**
  141 of the 158 rows were labelled `unused`. 97 of the 141 (69%) were calls with nothing to relay, and 76 of the 141
  were ack or ping probes ("reply OK", "reply with one word: ready"). 0 of 133 distinct router-call prompts forwarded the
  owner's prompt (4-gram overlap with the preceding owner message under 20% for all 133). The router's own hook was the
  cause of most of them: 40 of the 96 distinct class-e (not meant to be relayed) calls were made under a hold and 43 more
  followed a ROUTE hint with no hold seen. The 9 sessions hold 51 unique `Routing directive BLOCKED` tool results
  (50 Bash, 1 Write; 509 prompts, 10 per 100); 48 of the 133 router-call units have a block in the same human turn, 71
  of the 133 show any hold evidence, and the model says it is releasing a hold in 13. Cost of the
  probes and the blocked turns before them: 104 Claude turns (58 toll calls and 46 blocked attempts, each alone in its turn),
  51.9 million cache-read tokens (1.0% of the cache-read tokens of those sessions). The same toll was priced once
  before: `audit/23_UNKNOWN_UNKNOWNS.md` U-04 (audit of 2026-09) routed "Reply with the single word: acknowledged" to a 30B
  local model that held 42.6% of system memory.
  Population caution: 83% of the rows come from two v16 coordination sessions, so this is not a measure of ordinary use.
- **Cause.** `enforce-route.py` released a hold on `bare_name.startswith("llm_")`, that is on ANY `llm_*` tool,
  and on the expected door, with no look at what the call carried. The block text said so ("Escape valves: Call ANY
  llm_* tool (even a trivial `llm(task="query")`) - clears the lock for this turn") and the prompt hook's hard directive
  said "Call <door> - this releases the hold for this turn". The model took the cheapest call that did. Each toll also
  wrote a `route_acknowledged` realization row, so the savings ledger counted a probe as a route.
- **Fix.** `llm_router.hold_policy` decides what releases a hold. Only a routing door (`ROUTE_DOORS`: `llm`, `llm_route`,
  `llm_act`, `llm_edit`, `llm_local_task`, `llm_text_job` and the legacy task names) whose input carries the owner's
  request releases it (`forwards_owner_prompt`: the call restates at least half of the owner's content words and three of
  them, or is three fifths made of them with at least four shared, as a quoted excerpt of a long prompt is; `context`
  and `system_prompt` do not count). Any other call runs but leaves the hold in place: a probe is logged (`PROBE ...
  HOLD_KEPT` in `enforcement.log`, a `probe` row in `hold_events.jsonl`). The block text and the hard directive now say
  what releases a hold and that a trivial or unrelated call does not. The free valves are unchanged and need no model
  call (second block of one tool in a turn, three in two minutes, a fourth violation), and the explicit bypass for the
  user is the existing `claude:` prefix (`docs/proxy.md`, "Routing prefixes"; "When a prompt holds a tool"). Only a
  pending file with no `original_prompt` keeps the old release (nothing to compare, so nothing is held forever). An
  owner prompt with fewer than three content words ("push to main") is judged too: the call must restate all of its
  content words or be exactly its text, so "Reply OK" does not release it either (review of PR #817 at 549d02a9: the first
  version treated such a prompt as unverifiable and released it on any routing door, in hard and strict, against
  `docs/proxy.md`). Under `soft`, `suggest`, and where an exemption downgraded the call to soft, nothing is held, so
  those modes keep the release they always had (any `llm_*` tool clears the pending file; a left-behind file would have
  logged one more VIOLATION line on the next tool call) and record no probe. Every routing-door call, held or not,
  also writes a `door_call` row with an `ack` verdict (`hold_policy.is_ack_probe`), so the probes a ROUTE hint provokes
  with no hold in force are counted: `ack_probes` in the summary is "0 Reply OK probes". Hooks: `enforce-route.py` 24
  to 25, `auto-route.py` 78 to 79.
- **Known limits.** A forward in another language than the owner's prompt (a translation) is not recognised, so the hold
  stays until the second block of the tool pivots it: one extra blocked call, no toll. In `strict` mode (which disables the
  trap, loop and count valves) such a hold has no release but a recognised forward, `claude:`, the user's next prompt,
  the one-hour routing window, or the router's own CLI (`llm-router set-enforce off`, never held in any mode): a strict
  session can stall, which `SECURITY.md` and `docs/proxy.md` now say and the strict block text no longer hides behind a list
  of valves that are off. A probe still runs as a model call (it is allowed, not denied: denying MCP tools under a hold
  risked a loop); it just no longer helps. The wording match is lexical, so it is a judgement of overlap, not of intent.
  The `ack` verdict is a shape test (a reply verb and at most two words of answer, in 12 words or fewer): a probe
  phrased another way is missed, and the transcript audit stays the check.
- **Test.** `tests/test_w2_enforce_route_holds.py` runs the real hook as a subprocess:
  `test_a_probe_through_a_routing_door_runs_but_does_not_release` (8 cases),
  `test_a_status_or_admin_call_does_not_release` (4), `test_forwarding_the_owners_request_releases_the_hold` (3),
  `test_an_operational_hold_is_released_by_llm_act_carrying_the_task`,
  `test_a_hold_with_nothing_to_compare_keeps_the_old_release`,
  `test_the_second_block_of_one_tool_in_one_turn_pivots_for_free`,
  `test_a_probe_then_a_retry_is_two_calls_not_a_release_by_the_probe`,
  `test_the_block_text_names_the_user_bypass_and_not_the_toll`, `test_a_probe_is_recorded_in_hard_mode_too`,
  `test_no_probe_is_recorded_where_nothing_is_held`,
  `test_hooks_newer_than_the_package_still_decide_and_the_miss_is_counted`;
  `tests/test_route_enforcement_hooks.py::test_block_message_documents_how_a_hold_is_released` (replaces the test that
  pinned the escape valve); `tests/test_w2_auto_route_holds.py::test_the_hold_text_no_longer_offers_a_trivial_call`,
  `::test_a_hook_newer_than_the_package_says_only_what_is_true` and
  `::test_the_claude_prefix_is_the_explicit_user_bypass`; `tests/test_w2_hold_policy.py` (the forward judgement on
  verbatim, paraphrased, excerpted, probe, status, Hebrew and context-stuffed calls). Round 1 mutation check (the tests as of
  6a1e50fd) on unmodified origin/main 5c056309 (second checkout, new test files copied in): of 64 tests (29 + 15 + 19 in the three hook and door
  files, plus the rewritten block-message test) 59 fail and 5 pass, the 5 being preservation controls
  (`test_the_decision_goes_to_the_stdout_the_caller_holds_when_run_in_process`,
  `test_no_probe_is_recorded_where_nothing_is_held`, `test_a_hook_newer_than_the_package_says_only_what_is_true`,
  `test_a_forced_local_prefix_keeps_its_hold_even_when_short`,
  `test_research_with_no_provider_is_an_answer_that_says_what_to_do`).
  Existing tests: this entry claims only the files it names, and the full suite is CI's to run, not this entry's to vouch
  for. The first push (549d02a9) passed a hand-picked 65-file set (the 39 enforcement-hook test files, the W2 files, D-141,
  F25, F26, R12, doctor and the fail-open, silent-write, hook-pair and docs-lint ratchets) and still failed three repo-wide
  tests in CI that the set did not contain: the pricing baseline, the fail-open naming ratchet and the timing lane (review
  of PR #817). Round 2 therefore also runs every test file that references the enforcement hooks or scans the source tree,
  with the hook-pair guard's git base required. Seven existing tests changed: the block-message test above (1), the four
  listed in BIND-HOLD-1 (4) and, in round 2, the two honor-path tests of `tests/test_g3_realization_soak.py` (2), whose
  honoring call used to be sent with an empty input and now carries the owner's prompt (an empty call is not a forward).
  Round 2 (the review of PR #817) added, in the same file: `test_a_reply_ok_probe_does_not_release_a_tiny_prompts_hold`
  (hard, strict), `test_restating_a_tiny_prompt_releases_its_hold` (3 calls, 3 modes),
  `test_a_long_prompt_with_one_content_word_is_judged_the_same_way`, `test_strict_has_no_free_valve_and_a_probe_leaves_the_hold`,
  `test_the_strict_block_text_does_not_promise_a_release_that_never_comes`, `test_strict_still_lets_the_routers_own_cli_run`,
  `test_control_the_other_modes_still_list_their_free_valves`, `test_soft_and_suggest_still_clear_the_pending_file_on_any_llm_tool`
  (with the control `test_control_a_pending_file_under_soft_logs_a_violation_for_a_work_tool`),
  `test_a_door_call_outside_any_hold_is_counted_so_the_probe_bar_is_measurable`,
  `test_a_door_call_under_a_hold_records_whether_it_carried_the_request`, `test_the_ack_verdict_is_on_the_row_and_the_text_is_not`,
  `test_only_routing_doors_get_a_door_call_row`, `test_door_calls_are_counted_in_soft_mode_too`; in
  `tests/test_w2_hold_ledger.py` the organic filter (`test_the_organic_filter_counts_only_organic_sessions`,
  `test_by_kind_is_always_present_and_an_untagged_session_is_its_own_bucket`, `test_a_session_that_was_never_tagged_is_not_organic`,
  `test_the_doctor_counter_ignores_a_harness_session_that_swamps_the_raw_rate`,
  `test_the_doctor_counter_alarms_on_an_ack_probe_outside_any_hold`); in `tests/test_w2_hold_policy.py` the tiny-prompt
  judgement and `is_ack_probe`; `tests/test_w2_auto_route_holds.py::test_the_claude_prefix_is_the_explicit_user_bypass` now
  runs in smart, hard and strict. Round 2 red check (2026-11-03, the tests named above plus
  `test_a_crash_while_writing_the_door_call_row_never_changes_the_decision`): the 43 selected tests of
  `tests/test_w2_enforce_route_holds.py` run against the hook and `hold_policy.py` of 549d02a9 (swapped into a scratch
  copy of the tree): 35 fail, 8 pass. The 8 pin behaviour that did not change: 2 controls, the 4 cases of
  `test_only_routing_doors_get_a_door_call_row`, `test_strict_has_no_free_valve_and_a_probe_leaves_the_hold` and
  `test_strict_still_lets_the_routers_own_cli_run`.
- **To measure after deploy (not a result of this PR).** `python -m llm_router.hold_ledger --days 30 --organic`:
  `blocked_per_100_prompts` (bar: under 1) and `ack_probes` (bar: 0, held or not), on organic sessions after the fix is
  live; `by_kind` shows the other kinds beside it. `probes_short` counts only calls made under a hold and is a diagnostic,
  not the bar.
