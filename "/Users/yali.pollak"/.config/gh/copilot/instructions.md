<!-- llm-router-rules-version: 1 -->
# LLM Router — GitHub Copilot CLI Routing Rules

> These rules apply when using llm-router MCP tools inside GitHub Copilot CLI.
> Copilot CLI supports MCP servers via /mcp add or config file.
> The .claude-plugin/ manifest is loaded automatically by Copilot CLI.

---

## How to Route

Copilot CLI loads MCP servers from the project or global config. Once llm-router is
registered, call MCP tools directly:

```
Rule: for research/generate/code/analyze tasks → call llm_auto(prompt=<the task>)
```

For research tasks specifically, always prefer `llm_research` (web-grounded via Perplexity).

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

- File edits — execute directly with Copilot CLI tools
- Short inline questions you already know

---

## Savings Visibility

Run `llm_savings` to see cross-session totals.

---

## Token-Efficient Responses

Skip preamble. Lead with result. Fragments fine when meaning is clear.
No trailing summaries. ≥3 items → bullets. Never restate the user's request.
