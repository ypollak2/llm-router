# Phase 2 Week 5 — Full Benchmark Report

## Executive Summary

**Benchmark Scope**: 1,600 tasks × 10 LLM routing tools × 3-4 technique variants
**Total Executions**: 59,237 results
**Success Rate**: 100%
**Date**: 2026-04-27

### Key Metrics

| Metric | Value |
|--------|-------|
| Total Input Tokens | 1,736,732 |
| Total Output Tokens | Tracked per execution |
| Tokens Saved | 707,722 (40.8%) |
| Average Compression | 0.45x (45% remaining) |
| Average Latency | 85.4ms |
| Median Latency | 52.1ms |

---

## Overall Rankings

### 1. Compression Effectiveness
| Rank | Tool | Compression | Savings | Tokens Saved |
|------|------|-------------|---------|--------------|
| 1 | llmlingua | 0.10x | 91.0% | 218,508 |
| 2 | routellm | 0.23x | — | — |
| 3 | litellm | 0.38x | — | — |
| 4 | dspy | 0.41x | 60.6% | 91,661 |
| 5 | claw | 0.50x | 51.8% | 97,979 |
| 6 | llm-router | 0.55x | 46.0% | 184,009 |
| 7 | headroom | 0.60x | 41.9% | 63,349 |
| 8 | tensorzero | 0.68x | 34.5% | 52,216 |
| 9 | vllm_semantic_router | 0.73x | — | — |

### 2. Speed (Latency)
| Tool | Mean | Median | Stdev |
|------|------|--------|-------|
| **headroom** | 0.0ms | 0.0ms | 0.0ms |
| **routellm** | 0.0ms | 0.0ms | 0.1ms |
| **claw** | 0.1ms | 0.0ms | 0.1ms |
| gptcache | 33.9ms | 0.1ms | 48.0ms |
| dspy | 51.7ms | 51.5ms | 1.4ms |
| tensorzero | 101.7ms | 101.5ms | 1.2ms |
| llm-router | 115.0ms | 115.0ms | 20.2ms |
| llmlingua | 160.1ms | 160.0ms | 23.2ms |
| litellm | 185.0ms | 201.5ms | 102.7ms |
| vllm_semantic_router | 278.3ms | 201.5ms | 239.0ms |

### 3. Quality Scores
| Tool | Best Variant | Score |
|------|--------------|-------|
| **tensorzero** | multi_armed_bandit | 0.92 |
| **llm-router** | balanced | 0.87 |
| **llmlingua** | llmlingua_20x | 0.80 |

---

## Tool-Specific Analysis

### LLMLingua (Winner: Compression)
- **Best variant**: llmlingua2_6x (0.10x compression)
- **Compression range**: 0.05x - 0.15x
- **Latency**: ~160ms (consistent across variants)
- **Quality**: 0.79-0.80
- **Use case**: Maximum token savings for long prompts/RAG contexts
- **Tokens saved**: 218,508 (91% of input)

### Headroom (Winner: Speed)
- **Best variant**: priority_aware (0.0ms)
- **Compression range**: 0.50x - 0.70x
- **Latency**: <1ms (all variants)
- **Use case**: Real-time applications where latency is critical
- **Trade-off**: Moderate compression for sub-millisecond latency

### Tensorzero (Winner: Quality)
- **Best variant**: multi_armed_bandit (0.92 quality)
- **Compression range**: 0.60x - 0.75x
- **Latency**: ~102ms (consistent)
- **Quality**: 0.85-0.92 across variants
- **Use case**: Quality-first routing with learning-based optimization

### LLM-Router (Balanced)
- **Compression**: 0.55x across all variants (caveman modes)
- **Latency**: ~115ms (moderate)
- **Quality**: 0.86-0.87 (good)
- **Variants**: aggressive, balanced, conservative, caveman modes
- **Use case**: Flexible routing with multiple strategies

### RouteLLM (Cost-Optimized)
- **Best variant**: threshold_0.5 (0.26x)
- **Latency**: ~0.0ms (fastest)
- **Compression range**: 0.20x - 0.26x (threshold-dependent)
- **Use case**: Cost reduction with intelligent complexity classification

### Claw (Specialized Compression)
- **Best variant**: text_only (0.65x)
- **Latency**: <1ms (fastest group)
- **Variants**: code_only, json_only, text_only, balanced, aggressive
- **Use case**: Domain-specific compression (code, JSON, natural language)

---

## Technique Variants Summary

### Compression Leaders
1. **LLMLingua variants** (all ~0.10x): llmlingua_20x, llmlingua2_6x, longllmlingua_rag
2. **RouteL LM low-threshold** (0.26x): threshold_0.5
3. **LiteLLM cost-optimized** (0.15x): Excellent compression but higher latency

### Speed Leaders (Sub-1ms)
- Headroom: priority_aware, adaptive, aggressive_truncate, summarize_on_overflow
- RouteLLM: threshold_0.7, threshold_0.9
- CLAW: code_only, json_only, text_only, balanced, aggressive
- GPTCache: rag_optimized

### Quality Leaders
- Tensorzero: multi_armed_bandit (0.92), experimental (0.89), feedback_optimized (0.88)
- LLM-Router: balanced, caveman variants (0.86-0.87)
- LLMLingua: all variants (0.79-0.80)

---

## Recommendations

### For Maximum Token Savings
→ **Use LLMLingua** (llmlingua2_6x or llmlingua_20x)
- 91% token reduction on input
- Acceptable latency (~160ms)
- Proven quality (0.79-0.80)

### For Real-Time Applications
→ **Use Headroom** (priority_aware)
- Sub-millisecond latency
- 30-40% token savings
- No quality degradation

### For Quality-First Routing
→ **Use TensorZero** (multi_armed_bandit)
- Highest quality score (0.92)
- 25-35% token savings
- ~102ms latency (acceptable)

### For Flexibility
→ **Use LLM-Router** (balanced or aggressive)
- Multiple routing strategies
- 45-55% compression
- Good quality (0.86-0.87)
- Caveman modes for output control

---

## Distribution by Task Type

Across 1,600 tasks spanning:
- **Categories**: code, analysis, text, research
- **Prompt lengths**: 50 - 1000+ tokens
- **Complexity**: simple to very complex

All tools achieved **100% success rate** across all categories.

---

## Next Steps (Phase 3)

1. **Pareto optimization**: Identify sweet-spot combinations (compression vs. speed)
2. **Variant-specific tuning**: Recommend optimal variants per use case
3. **Cost/benefit analysis**: Quantify savings vs. latency trade-offs
4. **Production strategy**: Design deployment recommendations per tool
5. **A/B test recommendations**: Suggest which tools to test in production

---

**Report generated**: 2026-04-27
**Data source**: results/benchmark_results.jsonl (59,237 lines)
