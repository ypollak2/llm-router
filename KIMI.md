# Project Instructions

## LLM Router routing — cost-aware LLM routing

LLM Router MCP tools route each task to the cheapest capable model (Ollama → Flash → GPT-4o → Claude).
Call the relevant tool BEFORE generating your own answer — relay the routed result to the user.

> **Pull routing note:** Unlike Claude Code (which intercepts prompts via hooks automatically),
> Kimi Code uses pull routing: YOU must call the tool. The tool returns the response.

| Task | Tool |
|---|---|
| Code generation, refactors, tests | `llm(task="code")` |
| Questions, explanations, how-to | `llm(task="query")` |
| Debug, review, analysis | `llm(task="analyze")` |
| Research, current events, docs | `llm(task="research")` |
| Prose, docs, email | `llm(task="generate")` |
| Deep reasoning, proofs, root cause | `llm(task="analyze")` |

Never skip routing for non-trivial tasks. LLM Router routes to the cheapest capable model.

## LLM Router routing — cost-aware LLM routing

LLM Router MCP tools route each task to the cheapest capable model (Ollama → Flash → GPT-4o → Claude).
Call the relevant tool BEFORE generating your own answer — relay the routed result to the user.

> **Pull routing note:** Unlike Claude Code (which intercepts prompts via hooks automatically),
> Kimi Code uses pull routing: YOU must call the tool. The tool returns the response.

| Task | Tool |
|---|---|
| Code generation, refactors, tests | `llm(task="code")` |
| Questions, explanations, how-to | `llm(task="query")` |
| Debug, review, analysis | `llm(task="analyze")` |
| Research, current events, docs | `llm(task="research")` |
| Prose, docs, email | `llm(task="generate")` |
| Deep reasoning, proofs, root cause | `llm(task="analyze")` |

Never skip routing for non-trivial tasks. LLM Router routes to the cheapest capable model.
