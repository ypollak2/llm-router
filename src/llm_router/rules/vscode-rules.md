<!-- llm-router-rules-version: 1 -->
# LLM Router — VS Code / GitHub Copilot Routing Rules

> These rules apply when using llm-router MCP tools inside VS Code with GitHub Copilot.
> VS Code loads MCP servers from .vscode/mcp.json (workspace) or the user mcp.json.
> Use `llm_auto` for routing + cross-session savings tracking.

---

## How to Route

VS Code loads MCP servers on startup. Once llm-router is registered, call tools by name:

```
Rule: for research/generate/code/analyze tasks → call llm_auto(prompt=<the task>)
```

Add to `.github/copilot-instructions.md` for workspace-level routing guidance:

```
When a task requires live web search, call the llm_research MCP tool.
For code generation or complex analysis, call llm_code or llm_analyze.
For auto-routing with savings tracking, call llm_auto.
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

Run `llm_savings` in Copilot Chat to see cross-session totals.

---

## Token-Efficient Responses

Skip preamble. Lead with result. Fragments fine when meaning is clear.
No trailing summaries. ≥3 items → bullets. Never restate the user's request.
