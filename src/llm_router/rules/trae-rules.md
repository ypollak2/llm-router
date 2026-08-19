<!-- llm_router-rules-version: 1 -->
# LLM Router — Trae IDE Routing Rules

> These rules apply when using llm_router MCP tools inside Trae IDE (ByteDance).
> Trae v1.3.0+ supports MCP via mcpServers config and .rules files.

---

## How to Route

Trae loads MCP servers from the mcpServers config block. Once llm_router is registered,
call MCP tools by name for any substantive task:

```
Rule: for research/generate/code/analyze tasks → call llm_auto(prompt=<the task>)
```

---

## Task Type → Tool Mapping

| Task | Tool | Notes |
|---|---|---|
| Simple factual question | `llm_query` | Ollama → Gemini Flash → GPT-4o-mini |
| Research / current events | `llm_research` | Perplexity (web-grounded) |
| Writing / content | `llm_generate` | Gemini Flash → Haiku |
| Deep analysis | `llm_analyze` | Ollama → GPT-4o |
| Code generation | `llm_code` | Ollama → Codex → o3 |
| Deep reasoning / proofs | `llm_reason` | Extended-thinking model |
| Unknown / auto-detect | `llm_auto` | Classifies + routes automatically |

---

## When NOT to Route

- File edits executed by Trae's built-in editor tools
- Short inline questions you already know

---

## Savings Visibility

Run `llm_savings` to see cross-session totals.
`llm_auto` shows a savings reminder every 5 calls.

---

## Token-Efficient Responses

Skip preamble. Lead with result. Fragments fine when meaning is clear.
No trailing summaries. ≥3 items → bullets. Never restate the user's request.
