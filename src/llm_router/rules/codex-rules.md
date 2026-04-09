<!-- llm-router-rules-version: 1 -->
# LLM Router — Codex CLI Routing Rules

> These rules apply when using llm-router MCP tools inside Codex CLI (openai/codex).
> Codex CLI has no UserPromptSubmit hook, so auto-routing is not available.
> Use `llm_auto` to get equivalent routing + cross-session savings tracking.

---

## How to Route Without a Hook

Since Codex CLI cannot inject routing hints automatically, call `llm_auto` explicitly
for any task that benefits from a cheaper external model.

```
Rule: before answering research, code generation, writing, or analysis tasks,
call llm_auto(prompt=<the task>) and return its output.
```

This gives you:
- Same free-first routing chain as Claude Code (Ollama → Codex built-in → paid APIs)
- Cross-session savings tracking (server-side SQLite, not per-session hooks)
- Periodic savings envelope every 5 calls

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

Codex CLI has no session-end hook for savings summaries.
Run `llm_savings` periodically to see cross-session totals.
`llm_auto` shows a reminder every 5 calls automatically.
