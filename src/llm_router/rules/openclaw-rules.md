# llm-router Routing Rules for OpenClaw

Configure intelligent LLM routing for OpenClaw with the llm_auto command.

## Available MCP Tools

- `llm_auto` — Automatic intelligent routing (recommended)
- `llm_research` — Research and current information
- `llm_code` — Code generation and development
- `llm_generate` — Content and documentation creation
- `llm_analyze` — Analysis and problem-solving
- `llm_query` — Simple information requests

## Token-Efficient Response Format

Responses use preamble-free formatting:
- Answers first, reasoning second
- Skip filler words and hedging language
- Use fragments when appropriate for clarity
- Focus on technical substance

## Setup

The llm-router MCP server is configured in `~/.openclaw/mcp.json` with smart routing that:
- Starts with free local models (Ollama)
- Falls back to API providers when needed
- Applies budget pressure automatically
