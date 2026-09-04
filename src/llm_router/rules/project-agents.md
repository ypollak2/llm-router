<!-- llm_router-rules-version: 1 -->
## LLM Router — routing rules for this repository

This repository is worked on with **LLM Router** installed in the agent host
(Claude Code, Codex CLI, or both). Before answering, route the *thinking* to the
cheapest model that can do it; use your own file and shell tools as usual.

| Task | Call | Typically served by |
|---|---|---|
| Simple question, lookup, one-liner | `llm_query` | local Ollama / the free seat |
| Deep analysis, review, design trade-off | `llm_analyze` | the subscription seat |
| Code generation or refactor | `llm_code` | coder model / Codex |
| Research, current events | `llm_research` | web-grounded model |
| Writing, docs, content | `llm_generate` | fast cloud model |

- If a `⚡ ROUTE:` hint is in your context, follow it: call the named tool and
  use its result. If the result is weak or the model refuses, do the work yourself.
- Never invent a routed answer. Route with `context=…` when the task needs files
  from this repository, or handle it directly.
- Seats already paid for (Claude, ChatGPT, Google) are the free tier; API keys are
  used only for a tier no seat covers.
