# LLM Router Demo 2 — SaaS Builder Report

**Date**: 2026-03-31 00:12  |  **Mode**: LIVE  |  **Pressure**: 0% (default)

## Summary

| Metric | Value |
|--------|-------|
| Tasks | 12 across 5 phases |
| Succeeded | 12/12 |
| Routing correct | 10/12 |
| Total cost | $0.17029 |
| Total tokens | 23,819 |
| Avg latency | 95290ms |

## Results by Tier

| Tier | Tasks | Routing OK | Total Cost | Avg Latency |
|------|-------|-----------|------------|-------------|
| BALANCED | 6 | 4/6 | $0.11673 | 112412ms |
| BUDGET | 3 | 3/3 | $0.01212 | 7357ms |
| PREMIUM | 3 | 3/3 | $0.04143 | 148978ms |

## Per-Task Results

| ID | Phase | Tier | Type | Model | Latency | Cost | Routing |
|---|---|---|---|---|---|---|---|
| P1 | Research | BALANCED | research | `gemini/gemini-2.5-pro` | 39410ms | $0.03881 | ⚠️ ⚠️  RESEARCH should use Perplexity, got gemini/gemini-2.5-pro |
| P2 | Research | BUDGET | query | `gemini/gemini-2.5-flash` | 5382ms | $0.00250 | ✅ ✅ Cheap model for BUDGET (gemini/gemini-2.5-flash) |
| D1 | Design | PREMIUM | analyze | `gemini/gemini-2.5-pro` | 40611ms | $0.04143 | ✅ ✅ Premium model (gemini/gemini-2.5-pro) |
| D2 | Design | BALANCED | analyze | `codex/gpt-5.4` | 146113ms | $0.00000 | ✅ ✅ Mid-tier for BALANCED (codex/gpt-5.4) |
| I1 | Implementation | BALANCED | code | `codex/gpt-5.4` | 280886ms | $0.00000 | ✅ ✅ Mid-tier for BALANCED (codex/gpt-5.4) |
| I2 | Implementation | BALANCED | code | `codex/gpt-5.4` | 137962ms | $0.00000 | ✅ ✅ Mid-tier for BALANCED (codex/gpt-5.4) |
| I3 | Implementation | BUDGET | generate | `gemini/gemini-2.5-flash` | 10707ms | $0.00627 | ✅ ✅ Cheap model for BUDGET (gemini/gemini-2.5-flash) |
| A1 | AI Features | PREMIUM | analyze | `codex/gpt-5.4` | 172945ms | $0.00000 | ✅ ✅ Premium model (codex/gpt-5.4) |
| A2 | AI Features | BALANCED | code | `gemini/gemini-2.5-pro` | 34759ms | $0.04194 | ✅ ✅ Mid-tier for BALANCED (gemini/gemini-2.5-pro) |
| G1 | Growth | BALANCED | research | `gemini/gemini-2.5-pro` | 35341ms | $0.03598 | ⚠️ ⚠️  RESEARCH should use Perplexity, got gemini/gemini-2.5-pro |
| G2 | Growth | BUDGET | generate | `gemini/gemini-2.5-flash` | 5981ms | $0.00334 | ✅ ✅ Cheap model for BUDGET (gemini/gemini-2.5-flash) |
| G3 | Growth | PREMIUM | analyze | `codex/gpt-5.4` | 233377ms | $0.00000 | ✅ ✅ Premium model (codex/gpt-5.4) |

## What Went Right

- **P2 (BUDGET/query)**: ✅ Cheap model for BUDGET (gemini/gemini-2.5-flash)
- **D1 (PREMIUM/analyze)**: ✅ Premium model (gemini/gemini-2.5-pro)
- **D2 (BALANCED/analyze)**: ✅ Mid-tier for BALANCED (codex/gpt-5.4)
- **I1 (BALANCED/code)**: ✅ Mid-tier for BALANCED (codex/gpt-5.4)
- **I2 (BALANCED/code)**: ✅ Mid-tier for BALANCED (codex/gpt-5.4)
- **I3 (BUDGET/generate)**: ✅ Cheap model for BUDGET (gemini/gemini-2.5-flash)
- **A1 (PREMIUM/analyze)**: ✅ Premium model (codex/gpt-5.4)
- **A2 (BALANCED/code)**: ✅ Mid-tier for BALANCED (gemini/gemini-2.5-pro)
- **G2 (BUDGET/generate)**: ✅ Cheap model for BUDGET (gemini/gemini-2.5-flash)
- **G3 (PREMIUM/analyze)**: ✅ Premium model (codex/gpt-5.4)

## What Went Wrong

- **P1 (BALANCED/research)**: ⚠️  RESEARCH should use Perplexity, got gemini/gemini-2.5-pro
- **G1 (BALANCED/research)**: ⚠️  RESEARCH should use Perplexity, got gemini/gemini-2.5-pro

## Cost Efficiency Analysis

Total cost: **$0.17029** for 12 diverse tasks

Estimated cost if all tasks ran on Claude Opus:
- ~$0.26/call × 12 calls = **~$3.12**
- Savings: **~$2.95** (95% reduction)