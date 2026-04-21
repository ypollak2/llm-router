<!-- llm-router-rules-version: 5 -->
# LLM Router — Global Routing Rules

> Installed by llm-router. These rules control which model handles every task.

---

## ROUTING HINT = HARD CONSTRAINT (NOT a suggestion)

When `⚡ MANDATORY ROUTE:` appears in your context, it was injected by the UserPromptSubmit
hook BEFORE you processed this message. It is a binding routing decision, not advice.

**The economic contract**: routing to Haiku/Gemini Flash costs 50–100x less than Opus handling
the task directly. A "good enough" answer from a cheap model is always preferable to a
"perfect" answer from you. The whole point is cost reduction, not quality maximisation.

---

## What You MUST Do When a ROUTE Hint Is Present

1. Call the specified MCP tool **as your first and only substantive action**
2. Return its output to the user
3. Done

```
⚡ MANDATORY ROUTE: query/simple → call llm_query(complexity="simple") [via heuristic]
⚡ MANDATORY ROUTE: research/moderate → call llm_research [via ollama]
⚡ MANDATORY ROUTE: code/complex → call llm_code(complexity="complex") [via api]
⚡ MANDATORY ROUTE: analyze/moderate → call llm_analyze(complexity="moderate") [via heuristic]
⚡ MANDATORY ROUTE: generate/simple → call llm_generate(complexity="simple") [via ollama]
```

The format is: `⚡ MANDATORY ROUTE: {task_type}/{complexity} → call {tool}({args}) [via {method}]`
Extract the tool name and call it with the shown arguments.

---

## What You MUST NOT Do When a ROUTE Hint Is Present

These actions are **explicitly forbidden** when a routing hint fires:

| Forbidden action | Why it violates routing |
|-----------------|------------------------|
| Using the **Agent tool** to spawn subagents | Subagents run on expensive models — defeats cost savings |
| Using **WebSearch** or **WebFetch** directly | Route hint already decided which model handles research |
| Using **Read / Grep / Glob** to answer the question yourself | You're doing the work the cheap model should do |
| Using **Bash** to compute/research the answer | Same — you're the expensive model, stay out of it |
| Answering from your own knowledge without calling the MCP tool | Direct Opus/Sonnet answer = no routing = full token cost |
| Deciding the task is "complex enough" to warrant self-handling | The classifier already decided. It was right. |
| Calling the MCP tool AND ALSO doing the research yourself | Double cost — defeats the purpose entirely |

**The only permitted exception**: if the task requires editing/reading specific files in
the current codebase (not answering a question — actually modifying code), you may use
file tools. But questions about the codebase still route: "how does X work?" → `llm_query`.

---

## Task Type → Tool Mapping

| Route hint | MCP tool | Notes |
|---|---|---|
| `research/*` | `llm_research` | Perplexity / web-grounded models |
| `generate/*` | `llm_generate` | Gemini Flash / Haiku for writing |
| `analyze/*` | `llm_analyze` | Sonnet-class for deep analysis |
| `code/*` | `llm_code` | Haiku / Sonnet for code tasks |
| `query/*` | `llm_query` | Haiku / Gemini Flash for questions |
| `image/*` | `llm_image` | Image generation models |
| `auto/*` | `llm_route` | Full re-classification |

---

## When No Hint Is Present

If no `⚡ MANDATORY ROUTE:` line appears (hook not installed or prompt was skipped):
- Research / current events → `llm_research`
- Writing / content → `llm_generate`
- Deep analysis → `llm_analyze`
- Code questions → `llm_code`
- Simple questions → `llm_query`

---

## What NOT to Do (Summary)

- Do NOT ignore routing hints — ever
- Do NOT use Agent subagents for routed tasks — this is the #1 violation
- Do NOT treat "I could do this better myself" as a reason to skip routing
- Do NOT route AND also do the work yourself — pick one (always pick routing)
- Do NOT re-classify what the hook already classified

---

## Token-Efficient Responses

Routing already saves 50–100x on model cost. Apply these rules to also save output tokens:

**Skip all preamble.** Never open with "I'll help", "Let me", "Great question", "Certainly", "Sure".

**Lead with the result.** Answer first, reasoning only if asked or non-obvious.

**Fragments are fine** when meaning is clear:
- ✗ "I am routing this to Haiku. This saved you $0.012."
- ✓ "Routed → Haiku. Saved $0.012."

**Drop unnecessary articles** (a/an/the) when omitting them doesn't change meaning.

**No trailing summaries.** User can read the output — don't restate it.

**≥3 items → table or bullets**, not prose.

**Never restate the user's request** before answering it.

These rules stack with routing: cheaper model + fewer tokens = maximum cost reduction.
