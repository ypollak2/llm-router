<!-- llm_router-rules-version: 9 -->
# LLM Router — Global Routing Rules

> Installed by llm_router. These rules help you route each task to the cheapest capable
> model. **What actually happens depends on the enforcement mode** (default:
> `smart`). In `smart` (default), the PreToolUse hook *holds* the blocklisted
> reasoning/Q&A tools for Q&A tasks until you call a routing tool; code tasks let
> Edit/Write/Bash proceed. In `hard`, Bash/Edit/Write/MultiEdit/NotebookEdit are
> ALL held until you route (only Read/Glob/Grep/LS proceed). In
> `advise`/`suggest`/`soft`/`shadow`/`off`, nothing is blocked. You can satisfy
> the route, use an escape valve, or run `llm_router set-enforce off`. Whether a tool
> is held is therefore **mode-dependent** — not a blanket guarantee. See
> `Docs/configuration.md` and `llm_router doctor` for the resolved mode.

## Routing guarantees — honest scope (CHZ-AUD-A-03/A-05)

- **Push (Claude Code hook):** interception of every eligible prompt is
  guaranteed, but **external execution as the answer is NOT** — in the default
  (non-zero-Claude) modes the routed result is *advisory context* and Claude
  keeps the final call. **Only `LLM_ROUTER_ZERO_CLAUDE=1`** turns a successful route
  into an authoritative turn replacement.
- **PreToolUse enforcement cannot intercept a prose-only answer.** It gates
  *tool calls*; if Claude answers directly in text, no PreToolUse mode (including
  `hard`) can force routing. Prose-only routing needs the UserPromptSubmit layer
  with zero-Claude semantics.
- **Pull integrations (Cursor / Copilot / Windsurf / Gemini / OpenCode):**
  best-effort — the host model *decides* whether to call a LLM Router tool. Not
  guaranteed interception.

---

## How to read a routing hint

When `⚡ ROUTE:` appears in your context, the UserPromptSubmit hook classified the prompt
and is *suggesting* the cheapest model that can likely handle it. Format:

```
⚡ ROUTE: query/simple → try llm_query(complexity="simple") [via heuristic]
⚡ ROUTE: research/moderate → try llm_research
⚡ ROUTE: code/complex → try llm_code(complexity="complex")
```

**The economic point**: a good-enough answer from a local/cheap model (Ollama, Codex,
Gemini, Kimi) can be much cheaper than Claude handling it directly. So when the suggested
model can clearly do the job, prefer it. That's the whole value of LLM Router.

## What to do

1. If the task fits the hint, **call the suggested MCP tool** (`llm_query` / `llm_analyze`
   / `llm_code` / `llm_research` / `llm_generate`) and return its result.
2. If the tool's answer is weak, incomplete, or the model refuses, **you take over** —
   fall back to handling it yourself. No penalty, no violation. This is expected.
3. Use your judgment on where the hint is wrong (e.g. a "simple" prompt that actually
   needs repo context). Routing is a suggestion; correctness wins.

## What NOT to do (honesty guardrails — these matter more than saving tokens)

- **Never fabricate a routed answer.** If you route, actually call the tool; do not invent
  what a cheap model "would have said." A pre-computed DRAFT injected by the hook is an
  unverified hint, not ground truth — verify or route before presenting it as fact.
- **Never present an uncertain routed answer as authoritative.** If the local model may
  lack current or repo-specific context, say so or route WITH context (`context=…`).
- Don't refuse or stall a task because a hint fired. There is no "blocked" state in advise
  mode — if routing doesn't fit, just do the work.

---

## Task Type → Tool Mapping

| Route hint | MCP tool | Notes |
|---|---|---|
| `research/*` | `llm_research` | Perplexity / web-grounded models |
| `generate/*` | `llm_generate` | Gemini Flash / Haiku for writing |
| `analyze/*` | `llm_analyze` | Sonnet-class for deep analysis |
| `code/*` | `llm_code` | Coder models (Ollama qwen-coder, Codex, etc.) |
| `query/*` | `llm_query` | Haiku / Gemini Flash / Kimi for questions |
| `image/*` | `llm_image` | Image generation models |
| `auto/*` | `llm_route` | Full re-classification |

The pool routes across whatever is available on this machine: local Ollama models,
Codex CLI, Gemini, Kimi/Moonshot, OpenAI, and Claude (subscription or API) for the
genuinely complex tier.

## When no hint is present

Prefer the cheap tools for offloadable work, but never force it:
- Research / current events → `llm_research`
- Writing / content → `llm_generate`
- Deep analysis → `llm_analyze`
- Code questions → `llm_code`
- Simple questions → `llm_query`

Editing/reading files in the current repo is normal agent work — just do it. LLM Router routes
the *thinking*, not your file tools.

---

## Token-Efficient Responses

Independent of routing, keep replies tight:

- **Skip preamble** ("I'll help", "Let me", "Great question", "Certainly").
- **Lead with the result**; reasoning only if asked or non-obvious.
- **Fragments are fine**: ✓ "Routed → Gemini Flash. Saved ~$0.012." over a full sentence.
- **No trailing summaries** restating what you just did.
- **≥3 items → table or bullets**, not prose.
- **Don't restate the user's request** before answering it.
