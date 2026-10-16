QA-LAST-RESORT-1 (D-114): MCP Q&A keeps local models before Claude when no other
provider remains, so an installed Ollama can answer instead of failing on a missing
Anthropic API key. Local models are still removed when a non-Claude provider remains.
D-115 extends this to SDK `llm_router.route` and the zero-Claude hook. MCP `llm()`/
`llm_route`, gateway and route server use `route_and_call` with this rule. These
callers use `keep_if_only_local=True`; False is only the normal hook DIRECT path,
whose turn falls through to the host Claude session. Normal hook mode is unchanged.
Reconnect MCP servers to deploy.
Details in docs/bugs/QA-LAST-RESORT-1.md.
