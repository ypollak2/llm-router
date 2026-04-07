# LLM Router Demo Report

**Date**: 2026-04-07 10:14  |  **Mode**: DRY RUN  |  **Pressure**: 0% (default)

## Summary

| Metric | Value |
|--------|-------|
| Tasks run | 6 |
| Succeeded | 6/6 |
| Routing correct | 6/6 |
| Total cost | $0.001200 |
| Total tokens | 1,200 |
| Avg latency | 500ms |

## Task Results

### T1: Quick concept check

- **Tier**: BUDGET  |  **Type**: query  |  **Profile**: budget
- **Expected model**: haiku / gemini-flash
- **Status**: ✅ Succeeded
- **Model used**: `anthropic/claude-haiku-4-5-20251001`
- **Tokens**: 120 in / 80 out
- **Cost**: $0.000200  |  **Latency**: 500ms
- **Routing**: ✅ ✅ Cheap model used for BUDGET tier (anthropic/claude-haiku-4-5-20251001)

### T2: Simple README draft

- **Tier**: BUDGET  |  **Type**: generate  |  **Profile**: budget
- **Expected model**: haiku / gemini-flash
- **Status**: ✅ Succeeded
- **Model used**: `anthropic/claude-haiku-4-5-20251001`
- **Tokens**: 120 in / 80 out
- **Cost**: $0.000200  |  **Latency**: 500ms
- **Routing**: ✅ ✅ Cheap model used for BUDGET tier (anthropic/claude-haiku-4-5-20251001)

### T3: CRUD API implementation

- **Tier**: BALANCED  |  **Type**: code  |  **Profile**: balanced
- **Expected model**: claude-sonnet / gpt-4o
- **Status**: ✅ Succeeded
- **Model used**: `anthropic/claude-sonnet-4-6`
- **Tokens**: 120 in / 80 out
- **Cost**: $0.000200  |  **Latency**: 500ms
- **Routing**: ✅ ✅ Mid-tier model for BALANCED (anthropic/claude-sonnet-4-6)

### T4: Code review of the implementation

- **Tier**: BALANCED  |  **Type**: analyze  |  **Profile**: balanced
- **Expected model**: claude-sonnet / gpt-4o
- **Status**: ✅ Succeeded
- **Model used**: `anthropic/claude-sonnet-4-6`
- **Tokens**: 120 in / 80 out
- **Cost**: $0.000200  |  **Latency**: 500ms
- **Routing**: ✅ ✅ Mid-tier model for BALANCED (anthropic/claude-sonnet-4-6)

### T5: Research: latest FastAPI best practices

- **Tier**: BALANCED  |  **Type**: research  |  **Profile**: balanced
- **Expected model**: perplexity/sonar (web-grounded, always first for RESEARCH)
- **Status**: ✅ Succeeded
- **Model used**: `perplexity/sonar-pro`
- **Tokens**: 120 in / 80 out
- **Cost**: $0.000200  |  **Latency**: 500ms
- **Routing**: ✅ ✅ Perplexity used for RESEARCH (correct)

### T6: Architecture decision: SQLite vs PostgreSQL

- **Tier**: PREMIUM  |  **Type**: analyze  |  **Profile**: premium
- **Expected model**: claude-opus / claude-sonnet (premium analysis)
- **Status**: ✅ Succeeded
- **Model used**: `anthropic/claude-sonnet-4-6`
- **Tokens**: 120 in / 80 out
- **Cost**: $0.000200  |  **Latency**: 500ms
- **Routing**: ✅ ✅ Premium model for PREMIUM tier (anthropic/claude-sonnet-4-6)

## What Went Right

- **T1 (BUDGET)**: ✅ Cheap model used for BUDGET tier (anthropic/claude-haiku-4-5-20251001)
- **T2 (BUDGET)**: ✅ Cheap model used for BUDGET tier (anthropic/claude-haiku-4-5-20251001)
- **T3 (BALANCED)**: ✅ Mid-tier model for BALANCED (anthropic/claude-sonnet-4-6)
- **T4 (BALANCED)**: ✅ Mid-tier model for BALANCED (anthropic/claude-sonnet-4-6)
- **T5 (BALANCED)**: ✅ Perplexity used for RESEARCH (correct)
- **T6 (PREMIUM)**: ✅ Premium model for PREMIUM tier (anthropic/claude-sonnet-4-6)

## What Went Wrong / Needs Fixing

- All tasks routed correctly! 🎉

## Routing Chain Inspection (dry-run only)

Chains shown are what the router *would* use at current pressure:

- **T1 (BUDGET/query)**: `anthropic/claude-haiku-4-5-20251001 → gemini/gemini-2.5-flash → groq/llama-3.3-70b-versatile → deepseek/deepseek-chat → openai/gpt-4o-mini`
- **T2 (BUDGET/generate)**: `anthropic/claude-haiku-4-5-20251001 → gemini/gemini-2.5-flash → deepseek/deepseek-chat → mistral/mistral-small-latest → openai/gpt-4o-mini`
- **T3 (BALANCED/code)**: `anthropic/claude-sonnet-4-6 → anthropic/claude-haiku-4-5-20251001 → deepseek/deepseek-chat → openai/gpt-4o → gemini/gemini-2.5-pro`
- **T4 (BALANCED/analyze)**: `anthropic/claude-sonnet-4-6 → anthropic/claude-haiku-4-5-20251001 → deepseek/deepseek-reasoner → openai/gpt-4o → gemini/gemini-2.5-pro`
- **T5 (BALANCED/research)**: `perplexity/sonar-pro → perplexity/sonar → anthropic/claude-sonnet-4-6 → gemini/gemini-2.5-pro → openai/gpt-4o`
- **T6 (PREMIUM/analyze)**: `anthropic/claude-sonnet-4-6 → anthropic/claude-opus-4-6 → deepseek/deepseek-reasoner → gemini/gemini-2.5-pro → openai/o3`

## Next Steps

Based on these results, refinements needed:

1. [ ] Any BUDGET tasks using expensive models → check pressure logic
2. [ ] Any BALANCED tasks under/over-routing → adjust chain ordering
3. [ ] Any RESEARCH tasks not using Perplexity → check RESEARCH static chain
4. [ ] Any failures → check API keys, model availability, error types
5. [ ] Compare dry-run chains vs live model used → verify no unexpected fallback