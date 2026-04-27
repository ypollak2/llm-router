# llm-router Routing Rules for OpenCode

Cost-aware LLM routing for OpenCode IDE.

## Routing via MCP (llm_auto)

Use the `llm_auto` command for intelligent automatic routing:
- `llm_auto` — Automatic routing (recommended for most tasks)
- `llm_research` — Web research, current information
- `llm_code` — Code generation and implementation
- `llm_generate` — Content and documentation
- `llm_analyze` — Problem decomposition
- `llm_query` — Factual lookups

## Token-Efficient Response Style

When using llm-router, responses use a token-efficient preamble style:
- No unnecessary filler ("I think", "basically", etc.)
- Direct answers before explanation
- Code examples shown inline
- Minimal preamble in context-heavy tasks

## Configuration

The llm-router MCP server is configured in `~/.config/opencode/config.json` with:
- Free-first routing (Ollama → cloud APIs → Claude)
- Budget pressure tracking
- Smart complexity classification
