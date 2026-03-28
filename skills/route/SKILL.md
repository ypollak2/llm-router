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

## Behavior

When invoked:

1. **Analyze the task** to determine:
   - Task type: research, generation, analysis, coding, or general query
   - Complexity: simple (budget), moderate (balanced), or complex (premium)

2. **Select the appropriate tool**:
   - Research/facts/current events → `llm_research`
   - Writing/content/brainstorming → `llm_generate`
   - Analysis/reasoning/debugging → `llm_analyze`
   - Code generation/refactoring → `llm_code`
   - Simple questions → `llm_query`

3. **Set the routing profile** based on complexity:
   - Quick draft or exploration → `budget`
   - Standard quality work → `balanced`
   - High-stakes or complex reasoning → `premium`

4. **Execute** and return the result with cost metadata

## Examples

```
/route What's the latest funding news in AI?
→ llm_research (budget) → Perplexity Sonar

/route Write a compelling product description for our new feature
→ llm_generate (balanced) → Gemini 2.5 Pro

/route Analyze this error trace and find the root cause: [paste]
→ llm_analyze (premium) → OpenAI o3

/route Implement a rate limiter in Python using sliding window
→ llm_code (balanced) → GPT-4o
```
