---
id: QA-LAST-RESORT-1
status: fixed in `fix/d114-qa-local-last-resort` (deploy: reconnect MCP servers; the auto-route hook refreshes at hook version 58)
---
## QA-LAST-RESORT-1. MCP Q&A lost its local fallback when only Claude remained

- **Symptom (2026-10-15).** env-doctor found that a fresh-process
  `llm_route(complexity_override="simple")` Q&A chain collapsed to
  `anthropic/claude-haiku-4-5` only. With no Anthropic API key and
  `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`, it raised `AuthenticationError` and ended
  "All models failed for query/budget", although Ollama was installed.
- **Cause.** D-14 stripped local models from every mixed Q&A MCP chain. When the
  only other candidates were Claude models without an Anthropic key, the chain
  had nothing that could run. In `router.py`, `_build_and_filter_chain` keeps
  `anthropic/*` entries via `_claude_sub_ok` when
  `config.llm_router_claude_subscription` is true: `config.available_providers`
  excludes anthropic in subscription mode, but dropping those entries used to
  leave an empty chain. Without an Anthropic API key, calling such an entry raises
  `AuthenticationError`. D-114 works around this by trying local first; it does
  not fix the AuthenticationError itself.
- **Fix.** D-114/D-115 retain local Q&A fallbacks for MCP, SDK, and zero-Claude
  hook callers. D-116 re-inserts local entries after the last non-Claude entry
  in the non-local chain, preserving relative order within both groups. If none
  exists, local goes first. Claude providers come from `contracts.CLAUDE_PROVIDERS`.
  A listed non-Claude provider may fail at call time (quota, auth, CLI error), so
  local ($0) is tried before trailing Claude entries. The accepted trade-off is
  that a weaker local answer may win over a working Claude.
  Both primary and emergency BUDGET chains use this rule. The blast radius is MCP
  `llm()`/`llm_route`, gateway and route server via `route_and_call`, SDK
  `llm_router.route`, and the zero-Claude hook (D-115). These callers use
  `keep_if_only_local=True`. False is only the normal hook DIRECT path, which
  strips local even if empty or Claude-only: its turn falls through to the host
  Claude session. Normal hook mode is unchanged.
  Under a daily cap, any capped Q&A chain that contains a local model is now
  served by it in smart and hard modes. Before D-116 a capped [openai, ollama]
  Q&A chain raised `BudgetExceededError`; with Claude present smart fell through
  to Claude. LOCAL-TIMEOUT-1 still demotes a recently timed-out local model to
  the end, after Claude.
- **Test.** `tests/test_d114_qa_local_last_resort.py` pins provider classification,
  string and ModelSpec ordering, interleaved chains, local-only and non-Q&A chains,
  normal hook stripping, and Claude-only chains without locals.
  `test_mcp_non_claude_call_failure_tries_local_before_claude` and
  `test_mcp_non_claude_and_local_failure_tries_claude_last` pin call-time failures;
  `test_mcp_local_timeout_cooldown_wins_over_d116` pins timeout demotion.
  `test_build_chain_qa_last_resort` and `test_sdk_qa_last_resort` pin builder and
  real SDK dispatch with and without external keys. MCP tests also pin emergency
  BUDGET fallback. Updated D-14 tests in `tests/test_mcp_qa_no_local.py`, including
  `test_qa_moves_local_after_the_last_non_claude_entry` and
  `test_qa_moves_openai_compat_local_servers_after_non_claude`, now assert order
  instead of absence and pin smart/hard daily caps for mixed Q&A chains.
  `test_qa_prompt_is_served_by_local_as_last_resort_in_zero_claude` in
  `tests/test_zero_claude_scenarios.py` pins the zero-Claude served answer.
