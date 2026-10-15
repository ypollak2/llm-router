---
id: QA-LAST-RESORT-1
status: fixed in `fix/d114-qa-local-last-resort` (deploy: reconnect MCP servers)
---
## QA-LAST-RESORT-1. MCP Q&A lost its local fallback when only Claude remained

- **Symptom (2026-10-15).** env-doctor found that a fresh-process
  `llm_route(complexity_override="simple")` Q&A chain collapsed to
  `anthropic/claude-haiku-4-5` only. With no Anthropic API key and
  `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`, it raised `AuthenticationError` and ended
  "All models failed for query/budget", although Ollama was installed.
- **Cause.** D-14 stripped local models from every mixed Q&A MCP chain. When the
  only other candidates were Claude models without an Anthropic key, the chain
  had nothing that could run.
- **Fix.** D-114 keeps D-14 stripping while any non-Claude provider remains.
  Otherwise, MCP retains local entries before Claude entries, preserving relative
  order within each group. Claude providers come from `contracts.CLAUDE_PROVIDERS`.
  Both primary and emergency BUDGET chains use this rule. Hook/SDK mode remains
  unchanged: it strips all local entries, even if the result is empty or Claude-only,
  because the turn can fall through to the host Claude session without an API key.
- **Test.** `tests/test_d114_qa_local_last_resort.py` pins provider classification,
  string and ModelSpec ordering, local-only and non-Q&A chains, unchanged hook/SDK
  stripping, and Claude-only chains without locals. MCP harness tests pin local
  service before Claude, Claude after local failure, stripping when a paid API remains,
  and the emergency BUDGET fallback.
