<!-- llm-router-rules-version: 1 -->
# LLM Router — Cursor IDE Routing Rules

> These rules apply when using llm-router MCP tools inside Cursor IDE.
> Cursor loads MCP servers from ~/.cursor/mcp.json (global) or .cursor/mcp.json (project).
> Use `llm_auto` for routing + cross-session savings tracking.

---

## How to Route

Cursor loads MCP servers on startup. Once llm-router is registered, call tools by name:

```
Rule: for research/generate/code/analyze tasks → call llm_auto(prompt=<the task>)
```

---

## Task Type → Tool Mapping

| Task | Tool | Notes |
|---|---|---|
| Simple factual question | `llm_query` | Gemini Flash / Groq |
| Research / current events | `llm_research` | Perplexity (web-grounded) |
| Writing / content | `llm_generate` | Gemini Flash / Haiku |
| Deep analysis | `llm_analyze` | GPT-4o / Gemini Pro |
| Code generation | `llm_code` | Ollama → Codex → o3 |
| Unknown / auto-detect | `llm_auto` | Classifies + routes |

---

## Savings Visibility

Run `llm_savings` in Cursor chat to see cross-session totals.

---

## Token-Efficient Responses

Skip preamble. Lead with result. Fragments fine when meaning is clear.
No trailing summaries. ≥3 items → bullets. Never restate the user's request.
