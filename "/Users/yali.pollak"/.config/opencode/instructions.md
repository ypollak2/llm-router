<!-- llm-router-rules-version: 1 -->
# LLM Router — OpenCode Routing Rules

> These rules apply when using llm-router MCP tools inside OpenCode (opencode-ai/opencode).
> OpenCode has no UserPromptSubmit hook, so auto-routing is not available by default.
> Use `llm_auto` to get equivalent routing + cross-session savings tracking.

---

## How to Route Without a Hook

Before answering any research, code generation, writing, or analysis task, call `llm_auto`:

```
Rule: llm_auto(prompt=<the task>) → return its output
```

This gives you:
- Free-first chain (Ollama → Codex/OpenCode built-in → paid APIs)
- Cross-session savings tracking (SQLite, not per-session)
- Periodic savings summary every 5 calls

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

- File edits (Read/Write/Edit operations) — execute directly
- Short inline questions where you already have the answer

---

## Savings Visibility

OpenCode has no session-end hook for savings summaries.
Run `llm_savings` periodically to see cross-session totals.
`llm_auto` shows a reminder every 5 calls automatically.

---

## Token-Efficient Responses

Skip preamble. Lead with result. Fragments fine when meaning is clear.
No trailing summaries. ≥3 items → bullets. Never restate the user's request.

- ✗ "I'll help you with that. Let me route this to the appropriate model..."
- ✓ "Routed → Haiku. [result]"
