<!-- llm-router-rules-version: 1 -->
# LLM Router — GitHub Copilot Routing Rules

> These rules apply when using llm-router MCP tools inside GitHub Copilot
> (VS Code agent mode with `.vscode/mcp.json`).
> Copilot has no hook system, so cost-routing is not available.
> These rules enable capability extension via external specialized models.

---

## What llm-router Adds in Copilot

| Capability | Tool | Value |
|---|---|---|
| Live web search | `llm_research` | Perplexity Sonar — real-time results |
| Auto-routing | `llm_auto` | Classifies + routes, tracks savings server-side |
| Image generation | `llm_image` | DALL-E 3 / Flux |
| Savings visibility | `llm_savings` | Cross-session stats from all hosts |

---

## Usage Pattern

In Copilot agent mode, you can call MCP tools via `@llm-router`:

```
@llm-router llm_auto What are the latest breaking changes in React 19?
@llm-router llm_research What's the current price of AAPL?
@llm-router llm_savings
```

---

## Honest Framing

Like Claude Desktop, Copilot cannot benefit from subscription cost-routing.
Use `llm_auto` for tasks that benefit from external models, and `llm_savings`
to see aggregate savings accumulated via Claude Code sessions.
