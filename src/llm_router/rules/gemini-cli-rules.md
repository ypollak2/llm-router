<!-- llm-router-rules-version: 1 -->
# LLM Router — Gemini CLI Routing Rules

> These rules apply when using llm-router MCP tools inside Gemini CLI (google-gemini/gemini-cli).
> Gemini CLI supports MCP servers and Extensions with hooks.
> Use `llm_auto` for cross-session routing + savings tracking.

---

## How to Route

Gemini CLI loads MCP servers from `~/.gemini/settings.json`. Once llm-router is
registered there, call MCP tools by name:

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

## When NOT to Route

- File edits executed directly by Gemini CLI tools
- Short inline questions you already know the answer to

---

## Savings Visibility

Run `llm_savings` to see cross-session totals.
`llm_auto` shows a savings reminder every 5 calls.

---

## Token-Efficient Responses

Skip preamble. Lead with result. Fragments fine when meaning is clear.
No trailing summaries. ≥3 items → bullets. Never restate the user's request.

- ✗ "I'll help you with that. Let me route this to the appropriate model..."
- ✓ "Routed → Haiku. [result]"
