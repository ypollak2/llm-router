<!-- llm-router-rules-version: 1 -->
# LLM Router — OpenClaw Routing Rules

> These rules apply when using llm-router MCP tools inside OpenClaw.
> OpenClaw is MCP-first — all tools load from config automatically.

---

## How to Route

OpenClaw loads MCP servers from `~/.openclaw/mcp.json`. Once llm-router is
registered, call MCP tools by name before answering substantive tasks:

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

Run `llm_savings` to see cross-session totals.

---

## Token-Efficient Responses

Skip preamble. Lead with result. Fragments fine when meaning is clear.
No trailing summaries. ≥3 items → bullets. Never restate the user's request.
