---
name: route
description: Route a task to the best LLM based on task type and complexity
trigger: /route
---

# /route — Smart LLM Task Router

Route any task to the optimal LLM automatically.

## Usage

```
/route <task description>
```

## Auto-Classification

Most prompts are classified automatically by the `UserPromptSubmit` hook — no `/route` needed. The hook uses a **multi-layer classification chain**:

1. **Heuristic scoring** (instant, free) — Three signal layers accumulate evidence:
   - Intent patterns (+3) — action verbs and task markers
   - Topic patterns (+2) — domain-specific nouns
   - Format patterns (+1) — structural and temporal cues
   - High-confidence match (score >= 4) routes immediately

2. **Ollama local LLM** (~1s, free) — When heuristics are uncertain, qwen3.5 classifies locally via the chat API with thinking disabled

3. **Cheap API model** (~$0.0001) — If Ollama is unavailable, Gemini Flash or GPT-4o-mini classifies

4. **Weak heuristic / auto fallback** — Last resort: low-confidence heuristic match or `llm_route` (full LLM classifier)

## Task Categories

| Category | Tool | Signals |
|----------|------|---------|
| Research | `llm_research` | Current events, news, funding, trends, market data, rankings |
| Generate | `llm_generate` | Writing, drafting, brainstorming, emails, articles, translations |
| Analyze | `llm_analyze` | Evaluation, debugging, comparison, trade-offs, code review |
| Code | `llm_code` | Implementation, refactoring, building, bug fixes |
| Query | `llm_query` | Simple questions, definitions, explanations |
| Image | `llm_image` | Visual generation, design, artwork |

## Complexity & Profiles

| Complexity | Profile | Model Tier |
|------------|---------|------------|
| Simple | `budget` | Gemini Flash, GPT-4o-mini |
| Moderate | `balanced` | GPT-4o, Gemini 2.5 Pro |
| Complex | `premium` | o3, Gemini 2.5 Pro |

## Savings Awareness

Every 5th routed task, the system shows estimated savings: Claude API costs avoided and rate limit capacity preserved. Run `llm_usage` for a detailed breakdown.

## Examples

```
What are the top 3 AI startups that raised funding?
→ research (heuristic, score=8) → llm_research (budget) → Perplexity Sonar

Write me a blog post about productivity tips
→ generate (heuristic, score=5) → llm_generate (balanced) → Gemini 2.5 Pro

Compare React vs Vue for our new project
→ analyze (ollama, qwen3.5) → llm_analyze (balanced) → GPT-4o

Implement a rate limiter in Python using sliding window
→ code (heuristic, score=4) → llm_code (balanced) → GPT-4o

What is a monad?
→ query (ollama, qwen3.5) → llm_query (budget) → Gemini Flash
```

## Configuration

Environment variables:
- `LLM_ROUTER_OLLAMA_MODEL` — Ollama model (default: `qwen3.5:latest`)
- `LLM_ROUTER_OLLAMA_URL` — Ollama server (default: `http://localhost:11434`)
- `LLM_ROUTER_OLLAMA_TIMEOUT` — Timeout in seconds (default: `5`)
- `LLM_ROUTER_CONFIDENCE_THRESHOLD` — Heuristic score cutoff (default: `4`)
