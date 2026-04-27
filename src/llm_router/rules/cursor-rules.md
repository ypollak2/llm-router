# llm-router Routing Rules for Cursor IDE

Configure intelligent LLM routing for Cursor IDE with cost optimization.

## Routing Commands

- `llm_auto` — Automatic routing (recommended)
- `llm_research` — Web-grounded research and current events
- `llm_code` — Code generation and refactoring
- `llm_generate` — Writing, documentation, content creation
- `llm_analyze` — Deep analysis and problem-solving
- `llm_query` — Quick factual questions

## Best Practices

1. Use routing commands explicitly for complex tasks
2. Let `llm_auto` handle simple requests
3. Specify complexity level for better model selection: `llm_code(complexity="complex")`
4. Check the Cursor status bar for current routing mode

## Tips

- The router learns your preferences over time
- Budget pressure automatically downgrades model tier when approaching limits
- All routing decisions are logged for review
