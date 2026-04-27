# Phase 1 Setup & Development Guide

**Status**: Week 1 — Base Framework Complete
**Date**: 2026-04-26

---

## Overview

Phase 1 establishes the framework for fair comparison of 10 token-saving tools across 1,600 diverse tasks.

**Deliverables (Week 1)**:
- ✅ `tools/base_wrapper.py` — Abstract interface for all tools
- ✅ `tools/llm_router_wrapper.py` — Reference implementation + tests
- ✅ `tests/test_tool_wrappers.py` — Comprehensive unit tests
- 🔄 **Next**: Implement 9 remaining tool wrappers (Weeks 2-3)

---

## Architecture

### Base Wrapper Interface

Every tool wrapper must implement the `BaseToolWrapper` abstract class:

```python
class BaseToolWrapper(ABC):
    async def initialize(self) -> None
    async def execute(task_input: ToolInput, technique_variant: str) -> ToolOutput
    async def cleanup(self) -> None
```

### Standardized Input/Output

**ToolInput** (standardized for all tools):
```python
@dataclass
class ToolInput:
    prompt: str
    model: str = "gpt-3.5-turbo"
    max_tokens: Optional[int] = None
    temperature: float = 0.7
    metadata: Dict[str, Any] = {}
```

**ToolOutput** (standardized metrics from all tools):
```python
@dataclass
class ToolOutput:
    response: str
    input_tokens: int
    output_tokens: int
    compressed_input_tokens: Optional[int]
    compression_ratio: Optional[float]
    latency_ms: float
    preprocessing_ms: float
    inference_ms: float
    quality_score: Optional[float]
    tool_name: str
    technique_variant: str
    timestamp: datetime
    error: Optional[str]
```

### Key Metrics Tracked

Every tool execution collects:
- **Token metrics**: Input, output, compressed (if applicable)
- **Compression ratio**: Original / compressed tokens
- **Timing breakdown**: Total, preprocessing, inference
- **Quality**: 0-1 score (from judge or heuristic)
- **Metadata**: Tool name, variant, timestamp, errors

---

## Adding a New Tool Wrapper

### Template

```python
# tools/mytool_wrapper.py

from base_wrapper import BaseToolWrapper, ToolInput, ToolOutput, register_tool

@register_tool("mytool")
class MyToolWrapper(BaseToolWrapper):
    async def initialize(self) -> None:
        await super().initialize()
        # Load model, validate config, test connectivity

    async def execute(self, task_input: ToolInput, technique_variant: str = "default") -> ToolOutput:
        # 1. Estimate input tokens
        input_tokens = self._estimate_tokens(task_input.prompt)

        # 2. Apply technique (routing, compression, caching, etc.)
        start = time.time()
        response = await self._apply_technique(task_input.prompt, technique_variant)
        preprocessing_ms = (time.time() - start) * 1000

        # 3. Get output tokens
        output_tokens = self._estimate_tokens(response)

        # 4. Return standardized output
        return ToolOutput(
            response=response,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=(time.time() - start) * 1000,
            preprocessing_ms=preprocessing_ms,
            tool_name="mytool",
            technique_variant=technique_variant,
        )

    async def cleanup(self) -> None:
        # Release resources
        await super().cleanup()
```

### Technique Variants

Each tool should support variants that control its token-saving behavior:

**RouteLLM variants**:
- `simple_routing`, `complex_routing`, `confidence_threshold_0.5`, etc.

**LLMLingua variants**:
- `llmlingua_20x`, `llmlingua2_6x`, `longllmlingua_rag`, etc.

**Claw Compactor variants**:
- `code_only`, `balanced`, `aggressive`, `all_content`, etc.

**DSPy variants**:
- `bootstrap_few_shot`, `miprov2`, `auto_generated_examples`, etc.

**Caveman (llm-router) variants**:
- `caveman_off`, `caveman_lite`, `caveman_full`, `caveman_ultra`

---

## Testing Wrappers

### Unit Tests

Run tests for a single wrapper:
```bash
cd research
python -m pytest tests/test_tool_wrappers.py::TestToolWrapperInterface -v
```

Run all wrapper tests:
```bash
python -m pytest tests/test_tool_wrappers.py -v
```

### Manual Testing

Test a wrapper interactively:
```python
import asyncio
from tools.mytool_wrapper import MyToolWrapper
from base_wrapper import ToolInput

async def test():
    wrapper = MyToolWrapper("mytool", {})
    await wrapper.initialize()

    task_input = ToolInput(prompt="Your test prompt")
    output = await wrapper.execute(task_input, "variant_name")

    print(f"Response: {output.response}")
    print(f"Compression: {output.compression_ratio}")
    print(f"Latency: {output.latency_ms}ms")

    await wrapper.cleanup()

asyncio.run(test())
```

---

## Phase 1 Timeline

### Week 1 (✅ Complete)
- ✅ `tools/base_wrapper.py` — Abstract interface
- ✅ `tools/llm_router_wrapper.py` — Reference impl (baseline)
- ✅ `tests/test_tool_wrappers.py` — Test suite

### Week 2 (✅ Complete — All 9 Additional Wrappers)

**Tool 1 (LLMLingua)** ✅
- `tools/llmlingua_wrapper.py` — 250 lines
- Supports: llmlingua_20x (20x compression), llmlingua2_6x (6x fast), longllmlingua_rag (RAG-optimized)
- Compression ratios: 0.05 (20x), 0.17 (6x), 0.25 (RAG)

**Tool 2 (RouteLLM)** ✅
- `tools/routellm_wrapper.py` — 230 lines
- Supports: threshold_0.5 (aggressive), threshold_0.7 (balanced), threshold_0.9 (conservative)
- Routing: Complexity-based (simple→GPT-3.5, complex→GPT-4)

**Tool 3 (LiteLLM)** ✅
- `tools/litellm_wrapper.py` — 240 lines
- Supports: cost_optimized, latency_optimized, quality_optimized routing
- Coverage: 100+ API providers with fallback chains

**Tool 4 (GPTCache)** ✅
- `tools/gptcache_wrapper.py` — 270 lines
- Supports: strict_similarity (90%), loose_similarity (70%), rag_optimized
- Mechanism: Semantic caching (0% compression on cache hit)

**Tool 5 (Claw Compactor)** ✅
- `tools/claw_wrapper.py` — 300 lines
- Supports: code_only, json_only, text_only, balanced, aggressive
- Compression ratios: 0.35-0.65 (35-65% reduction)

**Tool 6 (DSPy)** ✅
- `tools/dspy_wrapper.py` — 280 lines
- Supports: bootstrap_few_shot, miprov2, auto_generated, minimal
- Compression ratios: 0.30-0.50 (50-70% reduction via example optimization)

**Tool 7 (vLLM Semantic Router)** ✅
- `tools/vllm_semantic_router_wrapper.py` — 270 lines
- Supports: task_specific (code/reasoning/summarization), speed_optimized, quality_optimized
- Task-aware routing reduces both latency (30-70%) and cost

**Tool 8 (Headroom)** ✅
- `tools/headroom_wrapper.py` — 290 lines
- Supports: priority_aware, summarize_on_overflow, adaptive, aggressive_truncate
- Compression ratios: 0.50-0.70 (30-50% reduction via intelligent truncation)

**Tool 9 (TensorZero)** ✅
- `tools/tensorzero_wrapper.py` — 320 lines
- Supports: ab_tested, feedback_optimized, multi_armed_bandit, experimental
- Learning platform: Progressive prompt optimization through experimentation

### Week 3
**Tool 7 (DSPy)**:
- `tools/dspy_wrapper.py`
- Supports: BootstrapFewShot, MIPROv2 optimization
- Tests: `test_dspy_wrapper.py`

**Tool 8 (Headroom)**:
- `tools/headroom_wrapper.py`
- Supports: JSON, code, text compression
- Tests: `test_headroom_wrapper.py`

**Tool 9 (TensorZero)**:
- `tools/tensorzero_wrapper.py`
- Supports: Prompt optimization, A/B testing variants
- Tests: `test_tensorzero_wrapper.py`

### Week 4
**Orchestration & Testing**:
- `runners/benchmark_runner.py` — Main orchestrator
- Ray cluster setup
- End-to-end dry-run on 10-task sample

---

## File Structure

```
research/
├── tools/
│   ├── __init__.py
│   ├── base_wrapper.py              ✅ DONE (250 lines)
│   ├── llm_router_wrapper.py        ✅ DONE (200 lines, baseline)
│   ├── llmlingua_wrapper.py         ✅ DONE (250 lines)
│   ├── routellm_wrapper.py          ✅ DONE (230 lines)
│   ├── litellm_wrapper.py           ✅ DONE (240 lines)
│   ├── gptcache_wrapper.py          ✅ DONE (270 lines)
│   ├── claw_wrapper.py              ✅ DONE (300 lines)
│   ├── dspy_wrapper.py              ✅ DONE (280 lines)
│   ├── vllm_semantic_router_wrapper.py ✅ DONE (270 lines)
│   ├── headroom_wrapper.py          ✅ DONE (290 lines)
│   └── tensorzero_wrapper.py        ✅ DONE (320 lines)
│
├── tests/
│   ├── __init__.py
│   ├── test_tool_wrappers.py        ✅ DONE (interface tests)
│   ├── test_llmlingua_wrapper.py    🔄 TODO
│   ├── test_litellm_wrapper.py      🔄 TODO
│   └── ... (one per tool)
│
├── runners/
│   ├── __init__.py
│   ├── benchmark_runner.py          🔄 TODO (Week 3-4)
│   ├── tool_runner.py               🔄 TODO
│   └── quality_evaluator.py         🔄 TODO
│
├── config/
│   ├── experiment_config.yaml       🔄 TODO
│   ├── tool_configs/
│   │   ├── llmlingua_config.yaml
│   │   ├── litellm_config.yaml
│   │   └── ... (one per tool)
│
└── docs/
    ├── SETUP.md                     ✅ DONE (this file)
    ├── TOOL_SETUP.md                🔄 TODO (tool-specific setup)
    └── METHODOLOGY.md               🔄 TODO (measurement protocol)
```

---

## Next Immediate Steps (Phase 1 Week 3)

1. **Create individual test modules** for each wrapper (10 test files)
   - Pattern: `tests/test_{tool}_wrapper.py` for each tool
   - Validate interface contract, technique variants, error handling

2. **Run full test suite** to verify all 10 wrappers work correctly
   - `pytest tests/ -v` should show 10 passing test modules

3. **Build orchestration layer** (Week 3-4)
   - `runners/benchmark_runner.py` — Main benchmark executor (coordinates 1,600 tasks)
   - `runners/tool_runner.py` — Per-tool execution logic with timeout/retry
   - `runners/quality_evaluator.py` — Judge quality with LLM + human ratings

4. **Set up Ray cluster** for parallel execution across 10 tools

5. **Create task dataset** for Phase 2 (1,600 diverse tasks across 4 categories)

---

## Verification Checklist

### Phase 1 Week 1-2 (✅ Complete)
- [x] All 10 tool wrappers implemented and syntactically valid
  - llm-router, llmlingua, routellm, litellm, gptcache, claw, dspy, vllm_sr, headroom, tensorzero
  - Total: ~3000 lines of code
  - All follow BaseToolWrapper interface contract
- [x] Base wrapper interface validated with unit tests
- [x] Tool registry functional (all tools importable and registerable)
- [x] Technique variants documented for each tool
- [ ] Individual wrapper test modules (10 test files, Week 3)
- [ ] All wrapper tests passing (>80% coverage, Week 3)

### Phase 1 Week 3-4 (🔄 Pending)
- [ ] Dry-run on 10-task sample completes successfully
- [ ] Ray cluster initializes correctly
- [ ] Metrics collection validated
- [ ] Configuration templates created for all tools
- [ ] Error handling tested (timeout, API failures, etc.)
- [ ] Orchestration runners built (benchmark_runner.py, quality_evaluator.py)

---

## Resources

- **Base wrapper API**: `tools/base_wrapper.py` (full docstrings)
- **Reference implementation**: `tools/llm_router_wrapper.py`
- **Test examples**: `tests/test_tool_wrappers.py`
- **Benchmark methodology**: `../docs/RESEARCH_PROPOSAL.md` (Section 2.2)

---

**Status**: ✅ Phase 1 Week 2 COMPLETE — All 10 tool wrappers implemented
**Next**: Phase 1 Week 3 — Individual test modules + orchestration layer
**Review**: Ready for benchmark infrastructure (Ray, task dataset, quality eval)
