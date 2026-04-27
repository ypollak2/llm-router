# llm-router Routing Rules for VS Code

This section configures llm-router for optimal cost-aware LLM routing.

## Smart Routing Strategy

- **Research & web queries**: Route via `llm_research` for web-grounded answers
- **Code generation**: Route via `llm_code` for implementation tasks
- **Content creation**: Route via `llm_generate` for writing and documentation
- **Deep analysis**: Route via `llm_analyze` for complex problem-solving
- **Quick questions**: Route via `llm_query` for simple lookups

## Usage in VS Code

Press **Ctrl+L** to access the llm-router prompt. The MCP server automatically:
- Classifies task complexity (simple/moderate/complex)
- Routes to optimal LLM provider (local Ollama → cloud APIs → Claude)
- Tracks spending and applies budget pressure
- Caches classification results for consistency

## Tips

- Start with `llm_auto` for automatic intelligent routing
- Use `llm_research` for current events and web-based information
- Use `llm_code` with complexity hints for better model selection
