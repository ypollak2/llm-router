# LLM Router — Smart Routing Skill

Route tasks to the cheapest capable model automatically using llm-router MCP tools.

## When to Use This Skill

Before answering research, code, writing, or analysis tasks — call the appropriate
llm-router tool instead of answering directly. The router picks the cheapest model
that can handle the task (Ollama → Codex → paid APIs in free-first order).

## Task → Tool Mapping

| Task type | Tool to call | Why |
|-----------|-------------|-----|
| Simple factual question | `llm_query` | Gemini Flash / Groq — 50× cheaper than o3 |
| Research / current events | `llm_research` | Perplexity (web-grounded, not stale) |
| Writing / summaries / brainstorm | `llm_generate` | Gemini Flash / Haiku |
| Deep analysis / debugging | `llm_analyze` | GPT-4o / Gemini Pro |
| Code generation / refactoring | `llm_code` | Ollama → Codex built-in → o3 |
| Don't know which type | `llm_auto` | Auto-classifies + routes, tracks savings |

## How to Call

```
llm_query(prompt="What is the capital of France?")
llm_code(prompt="Refactor this function to use async/await", complexity="moderate")
llm_research(prompt="What changed in Python 3.13?")
llm_auto(prompt="<the full user request>")   # safest default
```

## When NOT to Route

- File read/write/edit operations — execute directly
- Inline tool calls where you have the answer already
- Tasks under 5 words (likely follow-ups, not standalone requests)

## Cost Impact

Routing simple tasks to Gemini Flash instead of o3 saves ~50–100×.
`llm_auto` shows cumulative savings every 5 calls automatically.
Run `llm_savings` anytime to see your totals.
