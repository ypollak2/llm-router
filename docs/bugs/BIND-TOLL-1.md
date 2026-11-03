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
  user is the existing `claude:` prefix (`docs/proxy.md`, "Routing prefixes"; "When a prompt holds a tool"). A pending
  file with no `original_prompt`, or an owner prompt with fewer than three content words, keeps the old release (nothing
  to judge, so nothing is held forever). Hooks: `enforce-route.py` 24 to 25, `auto-route.py` 78 to 79.
- **Known limits.** A forward in another language than the owner's prompt (a translation) is not recognised, so the hold
  stays until the second block of the tool pivots it: one extra blocked call, no toll. In `strict` mode (which disables the
  free valves) such a hold now has no release but a recognised forward, `claude:` or `set-enforce off`. A probe still
  runs as a model call (it is allowed, not denied: denying MCP tools under a hold risked a loop); it just no longer
  helps. The wording match is lexical, so it is a judgement of overlap, not of intent.
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
  verbatim, paraphrased, excerpted, probe, status, Hebrew and context-stuffed calls). Mutation check on unmodified
  origin/main 5c056309 (second checkout, new test files copied in): of 64 tests (29 + 15 + 19 in the three hook and door
  files, plus the rewritten block-message test) 59 fail and 5 pass, the 5 being preservation controls
  (`test_the_decision_goes_to_the_stdout_the_caller_holds_when_run_in_process`,
  `test_no_probe_is_recorded_where_nothing_is_held`, `test_a_hook_newer_than_the_package_says_only_what_is_true`,
  `test_a_forced_local_prefix_keeps_its_hold_even_when_short`,
  `test_research_with_no_provider_is_an_answer_that_says_what_to_do`).
  Existing tests: the 39 enforcement-hook test files, the W2 files, D-141, F25, F26, R12, doctor and the fail-open,
  silent-write, hook-pair and docs-lint ratchets (65 files, with the hook-pair guard's git base required, 2026-11-03, the tree of this
  PR) give 2,794 passed and 1 deselected. Five existing tests changed (the block-message test above and the four listed in
  BIND-HOLD-1); every other existing test is untouched and passes.
- **To measure after deploy (not a result of this PR).** `python -m llm_router.hold_ledger --days 30`:
  `blocked_per_100_prompts` (bar: under 1) and `probes_short` (bar: 0), on organic sessions after the fix is live.
