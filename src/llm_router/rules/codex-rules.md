<!-- llm_router-rules-version: 2 -->
# LLM Router — Codex CLI Routing Rules

> LLM Router routes each task to the cheapest capable model before Codex CLI generates
> its own response. Call the appropriate tool BEFORE answering.
>
> **Pull routing:** Codex CLI uses pull routing — YOU must call the tool. The tool
> returns the response; relay it verbatim. This fires in agent mode (~90% of turns).

## Task Type → Tool Mapping

| Task | Tool | Notes |
|---|---|---|
| Simple factual question | `llm_query` | Ollama → Gemini Flash → GPT-4o-mini |
| Research / current events | `llm_research` | Perplexity (web-grounded) |
| Writing / content | `llm_generate` | Gemini Flash → Haiku |
| Deep analysis / review | `llm_analyze` | Ollama → GPT-4o |
| Code generation / refactor | `llm_code` | Ollama → Codex → o3 |
| Bulk multi-file edits | `llm_edit` | Structured file-level edits |
| Deep reasoning / proofs | `llm_reason` | Extended-thinking model |
| Unknown / auto-detect | `llm_auto` | Classifies + routes automatically |

## Rules

1. Call the matching tool **before** generating your own response.
2. Return the tool's output to the user **as-is** (don't paraphrase).
3. Only use native Codex CLI intelligence for file navigation, shell commands,
   or when all LLM Router MCP servers are unavailable.
4. Use `llm_research` for anything time-sensitive — training has a cutoff.

## Token-Efficient Responses

Skip preamble. Lead with result. Fragments fine when meaning is clear.
No trailing summaries. ≥3 items → bullets. Never restate the user's request.
