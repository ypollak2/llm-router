QA-LAST-RESORT-1 (D-114, D-115, D-116): a Q&A route no longer drops local models. They are
tried after every listed non-Claude provider and before trailing Claude entries, or first
when only Claude is listed, so an installed Ollama answers instead of the call failing on a
missing Anthropic API key or on a non-Claude provider that fails at call time. This applies
to MCP `llm()`/`llm_route`, the gateway and the route server (all via `route_and_call`), the
SDK `llm_router.route`, and the zero-Claude hook. Normal hook DIRECT mode still strips local
for Q&A, because its turn falls through to the host Claude session. Earlier D-14 tests now
assert "after non-Claude, before Claude" instead of "absent". Under a daily cap, a capped Q&A
chain that contains a local model is now served by it in smart and hard modes. Reconnect MCP
servers to deploy; the auto-route hook refreshes at hook version 59.
Details in docs/bugs/QA-LAST-RESORT-1.md.
