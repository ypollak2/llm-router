QA-LAST-RESORT-1 (D-114): MCP Q&A keeps local models before Claude when no other
provider remains, so an installed Ollama can answer instead of failing on a missing
Anthropic API key. Local models are still removed when a non-Claude provider remains.
Hook and SDK behaviour stays unchanged because those turns can fall through to the
host Claude session without an API key. Reconnect MCP servers to deploy.
Details in docs/bugs/QA-LAST-RESORT-1.md.
