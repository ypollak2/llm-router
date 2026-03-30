<!-- llm-router-rules-version: 2 -->
# LLM Router — Global Routing Rules

> Installed by llm-router. These rules ensure every task is evaluated for optimal model routing.

## Routing Hint Enforcement (CRITICAL)

When a `[ROUTE: <task_type>/<complexity> via <method>]` hint appears in conversation context (injected by the UserPromptSubmit hook), you MUST follow it:

1. **Read the hint** — it tells you which `llm_*` tool to use and the task complexity
2. **Call the tool** — use the matching MCP tool (`llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_code`, `llm_image`, etc.)
3. **Never override** — do NOT ignore the hint because you think the task is "local", "codebase work", or something you can handle yourself

The router's classification IS the decision. It evaluated the prompt against multiple signal layers (heuristic scoring, local LLM, API fallback) and determined the best routing. Trust it.

## Task Type → Tool Mapping

| Route hint | MCP tool | Use case |
|---|---|---|
| `research/*` | `llm_research` | Web search, current events, market analysis |
| `generate/*` | `llm_generate` | Content creation, writing, brainstorming |
| `analyze/*` | `llm_analyze` | Deep analysis, comparisons, evaluations |
| `code/*` | `llm_code` | Code generation, refactoring, bug analysis |
| `query/*` | `llm_query` | Quick factual questions, lookups |
| `image/*` | `llm_image` | Image generation |

## When No Hint is Present

If no `[ROUTE:]` hint appears (hook not installed or prompt was skipped), use your judgment:
- Research requiring web access → `llm_research`
- Content generation → `llm_generate`
- Deep analysis beyond training data → `llm_analyze`
- Code generation for external model → `llm_code`
- Simple factual questions → `llm_query`

## Why Routing Saves Tokens Even for Simple Tasks

The primary value is **token arbitrage**: cheap models (Gemini Flash, GPT-4o-mini, Haiku) cost
~50-100x less than Opus. When a `[ROUTE: query/simple]` hint fires for a git commit message or
a one-line answer, calling `llm_query` saves real subscription tokens — Opus only orchestrates,
the cheap model generates the actual response.

Do NOT treat tasks as "too simple to route" — simple tasks are exactly when routing saves the most.

## What NOT to Do

- Do NOT ignore routing hints — ever
- Do NOT decide that a task is "too simple" for routing — simple tasks are the best candidates
- Do NOT skip routing because you're "already working on the codebase"
- Do NOT re-classify tasks that have already been classified by the hook
- Do NOT handle a task yourself because it seems "local" — local tasks still consume Opus tokens
