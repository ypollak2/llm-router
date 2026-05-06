<!-- llm-router-rules-version: 1 -->
# LLM Router — Pi Coding Agent Routing Rules

> These rules apply when using llm-router MCP tools inside Pi (pi.dev coding agent).
> Pi supports MCP servers via `~/.pi/agent/mcp.json`.
> Use `llm_auto` for cross-session routing + savings tracking.

---

## How to Route

Pi loads MCP servers from `~/.pi/agent/mcp.json`. Once llm-router is
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

- File editing (Read/Edit/Write) — do locally
- Git operations — do locally
- Tasks requiring Pi's built-in tools — let Pi handle them

---

## Cost Savings

Pi's native model is included in your subscription. llm-router adds value by:
- Routing simple tasks to free models (Ollama, Gemini Flash free tier)
- Tracking cost across sessions
- Providing analytics on routing decisions
