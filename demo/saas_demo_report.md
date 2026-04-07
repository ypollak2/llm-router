# LLM Router Demo 2 — SaaS Builder Report

**Date**: 2026-04-07 10:18  |  **Mode**: DRY RUN  |  **Pressure**: 0% (default)

## Summary

| Metric | Value |
|--------|-------|
| Tasks | 12 across 5 phases |
| Succeeded | 12/12 |
| Routing correct | 12/12 |
| Total cost | $0.00240 |
| Total tokens | 1,920 |
| Avg latency | 400ms |

## Results by Tier

| Tier | Tasks | Routing OK | Total Cost | Avg Latency |
|------|-------|-----------|------------|-------------|
| BALANCED | 6 | 6/6 | $0.00120 | 400ms |
| BUDGET | 3 | 3/3 | $0.00060 | 400ms |
| PREMIUM | 3 | 3/3 | $0.00060 | 400ms |

## Per-Task Results

| ID | Phase | Tier | Type | Model | Latency | Cost | Routing |
|---|---|---|---|---|---|---|---|
| P1 | Research | BALANCED | research | `perplexity/sonar-pro` | 400ms | $0.00020 | ✅ ✅ Perplexity for RESEARCH |
| P2 | Research | BUDGET | query | `anthropic/claude-haiku-4-5-20251001` | 400ms | $0.00020 | ✅ ✅ Cheap model for BUDGET (anthropic/claude-haiku-4-5-20251001) |
| D1 | Design | PREMIUM | analyze | `anthropic/claude-sonnet-4-6` | 400ms | $0.00020 | ✅ ✅ Premium model (anthropic/claude-sonnet-4-6) |
| D2 | Design | BALANCED | analyze | `anthropic/claude-sonnet-4-6` | 400ms | $0.00020 | ✅ ✅ Mid-tier for BALANCED (anthropic/claude-sonnet-4-6) |
| I1 | Implementation | BALANCED | code | `anthropic/claude-sonnet-4-6` | 400ms | $0.00020 | ✅ ✅ Mid-tier for BALANCED (anthropic/claude-sonnet-4-6) |
| I2 | Implementation | BALANCED | code | `anthropic/claude-sonnet-4-6` | 400ms | $0.00020 | ✅ ✅ Mid-tier for BALANCED (anthropic/claude-sonnet-4-6) |
| I3 | Implementation | BUDGET | generate | `anthropic/claude-haiku-4-5-20251001` | 400ms | $0.00020 | ✅ ✅ Cheap model for BUDGET (anthropic/claude-haiku-4-5-20251001) |
| A1 | AI Features | PREMIUM | analyze | `anthropic/claude-sonnet-4-6` | 400ms | $0.00020 | ✅ ✅ Premium model (anthropic/claude-sonnet-4-6) |
| A2 | AI Features | BALANCED | code | `anthropic/claude-sonnet-4-6` | 400ms | $0.00020 | ✅ ✅ Mid-tier for BALANCED (anthropic/claude-sonnet-4-6) |
| G1 | Growth | BALANCED | research | `perplexity/sonar-pro` | 400ms | $0.00020 | ✅ ✅ Perplexity for RESEARCH |
| G2 | Growth | BUDGET | generate | `anthropic/claude-haiku-4-5-20251001` | 400ms | $0.00020 | ✅ ✅ Cheap model for BUDGET (anthropic/claude-haiku-4-5-20251001) |
| G3 | Growth | PREMIUM | analyze | `anthropic/claude-sonnet-4-6` | 400ms | $0.00020 | ✅ ✅ Premium model (anthropic/claude-sonnet-4-6) |

## What Went Right

- **P1 (BALANCED/research)**: ✅ Perplexity for RESEARCH
- **P2 (BUDGET/query)**: ✅ Cheap model for BUDGET (anthropic/claude-haiku-4-5-20251001)
- **D1 (PREMIUM/analyze)**: ✅ Premium model (anthropic/claude-sonnet-4-6)
- **D2 (BALANCED/analyze)**: ✅ Mid-tier for BALANCED (anthropic/claude-sonnet-4-6)
- **I1 (BALANCED/code)**: ✅ Mid-tier for BALANCED (anthropic/claude-sonnet-4-6)
- **I2 (BALANCED/code)**: ✅ Mid-tier for BALANCED (anthropic/claude-sonnet-4-6)
- **I3 (BUDGET/generate)**: ✅ Cheap model for BUDGET (anthropic/claude-haiku-4-5-20251001)
- **A1 (PREMIUM/analyze)**: ✅ Premium model (anthropic/claude-sonnet-4-6)
- **A2 (BALANCED/code)**: ✅ Mid-tier for BALANCED (anthropic/claude-sonnet-4-6)
- **G1 (BALANCED/research)**: ✅ Perplexity for RESEARCH
- **G2 (BUDGET/generate)**: ✅ Cheap model for BUDGET (anthropic/claude-haiku-4-5-20251001)
- **G3 (PREMIUM/analyze)**: ✅ Premium model (anthropic/claude-sonnet-4-6)

## What Went Wrong

- All tasks routed correctly! 🎉

## Cost Efficiency Analysis

Total cost: **$0.00240** for 12 diverse tasks

Estimated cost if all tasks ran on Claude Opus:
- ~$0.26/call × 12 calls = **~$3.12**
- Savings: **~$3.12** (100% reduction)