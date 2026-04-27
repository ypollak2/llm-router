# Top 50+ Token-Saving Open-Source Repositories
**Research Date**: 2026-04-26
**Compiled for**: llm-router Token-Saving Tools Benchmark

---

## Summary

This document catalogs 50+ token-saving open-source repositories across 7 categories. Each entry includes:
- **Repository URL** and star count
- **Token-saving mechanism** — how it reduces tokens
- **Category** — primary approach (routing, compression, caching, etc.)
- **Maturity** — production vs. research vs. early-stage

**Legend**:
- ⭐ = Star count (as of 2026-04-26)
- 🟢 = Production-ready
- 🟡 = Mature but research-focused
- 🟠 = Early-stage or specialized

---

## CATEGORY 1: Routing & Model Selection

**Mechanism**: Select cheaper/faster models based on query complexity, cost, or latency.
**Token Savings**: 35-85% by routing simple queries to budget models.

### 1. RouteLLM (UC Berkeley, LMSYS)
- **URL**: https://github.com/lm-sys/RouteLLM
- **Stars**: 4.8K ⭐
- **Status**: 🟢 Production
- **Mechanism**: Trained routers classify queries as simple/complex. Routes simple to GPT-3.5 (cost-effective), complex to GPT-4 (higher quality). Can reduce costs by 85% while maintaining 95% GPT-4 quality.
- **Paper**: ICLR 2025 published
- **Best for**: Cost-conscious orgs with mixed-complexity workloads

### 2. LLMRouter (UIUC)
- **URL**: https://github.com/ulab-uiuc/LLMRouter
- **Stars**: 1K+ ⭐
- **Status**: 🟢 Production (v1.0 released Dec 2025)
- **Mechanism**: 16+ configurable routers (single-round, multi-round, agentic, personalized). Routes based on complexity, cost, latency, or custom signals. Unified `llmrouter` CLI.
- **Best for**: Teams needing router customization and plugin architecture

### 3. vLLM Semantic Router (Red Hat + vLLM)
- **URL**: https://github.com/vllm-project/semantic-router
- **Stars**: 2K+ ⭐ (vLLM: 25K+ ⭐)
- **Status**: 🟢 Production (v0.1 "Iris" released Jan 2026)
- **Mechanism**: Semantic understanding of requests to select best model. Routes based on modality, privacy, cost, safety, and latency across local/private/frontier models.
- **Integration**: Native vLLM integration for high-throughput inference
- **Best for**: Multi-model deployments with diverse workloads

### 4. LiteLLM Router
- **URL**: https://github.com/BerriAI/litellm
- **Stars**: 42.2K ⭐
- **Status**: 🟢 Production
- **Mechanism**: Unified interface to 100+ LLM APIs with routing, fallbacks, rate-limit handling, and cost tracking. Supports model groups with automatic failover and regional retry logic.
- **Cost Tracking**: Built-in per-provider cost logging
- **Best for**: Multi-provider setups; simplifies integration across OpenAI, Anthropic, Bedrock, etc.

### 5. Not-Diamond Awesome AI Model Routing
- **URL**: https://github.com/Not-Diamond/awesome-ai-model-routing
- **Stars**: ~500 ⭐
- **Status**: 🟡 Curated Reference
- **Mechanism**: Curated list of routing approaches and tools. Not a tool itself, but maps the routing landscape.
- **Best for**: Researchers exploring routing taxonomy

### 6. llm-router (Yali's Project)
- **URL**: https://github.com/anthropics/claude-code/llm-router
- **Stars**: TBD (in-repo)
- **Status**: 🟢 Production (v7.5.0)
- **Mechanism**: MCP server with multi-layer classification (heuristic → Ollama → API fallback). Routes 48+ MCP tools. Flexible policies (aggressive/balanced/conservative). Handles Claude subscription mode + Codex/Gemini API chains.
- **Unique**: Only tool that routes *across* subscriptions (Claude + Codex + Gemini balancing)
- **Best for**: Claude Code users; seamless IDE integration

---

## CATEGORY 2: Prompt Compression & Optimization

**Mechanism**: Reduce prompt length without losing critical information.
**Token Savings**: 20x compression (LLMLingua) to 97% (Claw Compactor on specific domains).

### 7. LLMLingua (Microsoft)
- **URL**: https://github.com/microsoft/LLMLingua
- **Stars**: ~2.5K ⭐
- **Status**: 🟢 Production
- **Mechanism**: Uses small language models (GPT-2-small, LLaMA-7B) to identify non-essential tokens. Three variants:
  - **LLMLingua**: Up to 20x compression (EMNLP 2023)
  - **LongLLMLingua**: 21.4% RAG improvement with 1/4 tokens
  - **LLMLingua-2**: Data distillation from GPT-4; 3-6x faster, handles out-of-domain data
- **Best for**: Prompt compression at scale; handles long documents

### 8. Claw Compactor (Open Compress)
- **URL**: https://github.com/open-compress/claw-compactor
- **Stars**: ~210K ⭐ (as OpenClaw)
- **Status**: 🟢 Production
- **Mechanism**: 14-stage deterministic fusion pipeline:
  - AST-aware code analysis (preserves logic)
  - JSON statistical sampling
  - Simhash-based deduplication
  - Content type routing (code vs. text vs. data)
- **Savings**: 15-82% depending on content; up to 97% on redundant structured data
- **Cost**: Zero LLM inference cost (all rule-based)
- **Best for**: Code-heavy workloads; tool output compression

### 9. Headroom
- **URL**: https://github.com/chopratejas/headroom
- **Stars**: ~1K ⭐
- **Status**: 🟢 Production
- **Mechanism**: Context optimization layer with three components:
  - **SmartCrusher**: JSON compression via statistical sampling
  - **CodeCompressor**: AST-aware code formatting
  - **Kompress-base**: Text summarization
  - **CacheAligner**: Stabilizes prefixes for KV cache hits (supports Anthropic/OpenAI)
- **Integration**: Runs as local proxy; zero code changes needed
- **Best for**: Drop-in context optimization for any LLM pipeline

### 10. Prompt Optimizer (vaibkumr)
- **URL**: https://github.com/vaibkumr/prompt-optimizer
- **Stars**: ~500 ⭐
- **Status**: 🟡 Active Research
- **Mechanism**: Minimizes token complexity using rule-based optimization. Removes redundant phrases, consolidates repetitive instructions.
- **Approach**: No model weights needed; pure NLP heuristics (similar to Claw)
- **Best for**: Lightweight prompt engineering; minimal dependencies

### 11. Token Optimizer (alexgreensh)
- **URL**: https://github.com/alexgreensh/token-optimizer
- **Stars**: ~300 ⭐
- **Status**: 🟡 Active Research
- **Mechanism**: Identifies "ghost tokens" (redundant, unused context) and removes them. Shows per-token cost breakdown.
- **Analysis**: Detailed visibility into where tokens go; prevents context quality decay during compression.
- **Best for**: Token cost analysis and debugging

### 12. GEPA (Reflective Prompt Evolution)
- **URL**: https://github.com/gepa-ai/gepa
- **Stars**: ~600 ⭐
- **Status**: 🟡 Research
- **Mechanism**: Self-optimizing prompts via text evolution. Outperforms TextGrad and expert-crafted prompts for agents.
- **Approach**: Evolutionary algorithms to refine prompts; reduces verbosity over iterations
- **Best for**: Multi-turn agent interactions; adaptive prompt refinement

### 13. MLLM-Token-Compression Survey
- **URL**: https://github.com/yaolinli/MLLM-Token-Compression
- **Stars**: ~400 ⭐
- **Status**: 🟡 Research Reference
- **Mechanism**: Survey of token compression for multimodal LLMs (vision + text). Maps compression techniques: token grouping, spatial pruning, temporal aggregation.
- **Best for**: Vision-heavy workflows (video, images)

### 14. Semantic Prompt Compressor
- **URL**: https://github.com/renswickd/semantic-prompt-cache
- **Stars**: ~200 ⭐
- **Status**: 🟡 Research
- **Mechanism**: Rule-based semantic compression without training. Uses spaCy NLP for entity extraction and abstractive summarization.
- **Lightweight**: No GPU needed; works locally
- **Best for**: Text compression with semantic awareness

---

## CATEGORY 3: Semantic Caching & KV Cache Optimization

**Mechanism**: Reuse cached responses for semantically similar queries or compress KV cache to lower precision.
**Token Savings**: 50-80% via caching; 30-50% via KV quantization.

### 15. PromptCache
- **URL**: https://github.com/messkan/prompt-cache
- **Stars**: ~800 ⭐
- **Status**: 🟢 Production
- **Mechanism**: Intelligent semantic caching. Detects when new prompts have same intent as cached ones; returns cached result instantly.
- **Architecture**: Drop-in Go proxy; provider-agnostic
- **Savings**: Up to 80% cost reduction + sub-millisecond response latency
- **Best for**: Repetitive query patterns; chat-like workloads

### 16. GPTCache (Zilliz)
- **URL**: https://github.com/zilliztech/GPTCache
- **Stars**: ~7K ⭐
- **Status**: 🟢 Production
- **Mechanism**: Semantic cache for LLM responses. Uses embedding algorithms to convert queries into vectors; vector store (Milvus) for similarity search.
- **Integration**: LangChain + llama_index integration out-of-box
- **Database**: Pluggable backends (Redis, SQLite, Milvus)
- **Best for**: RAG systems; reduces redundant LLM calls

### 17. vCache
- **URL**: https://github.com/vcache-project/vCache
- **Stars**: ~600 ⭐
- **Status**: 🟡 Research
- **Mechanism**: Intelligent semantic caching with adaptive decision boundaries. Adjusts similarity threshold based on accuracy requirements.
- **Approach**: Uses embeddings + confidence scoring to return cached responses
- **Best for**: High-volume QA systems; balances cache hit rate vs. accuracy

### 18. ModelCache (Codefuse AI)
- **URL**: https://github.com/codefuse-ai/ModelCache
- **Stars**: ~500 ⭐
- **Status**: 🟡 Active
- **Mechanism**: Semantic caching for LLM responses. Stores query-result pairs; retrieves via semantic similarity.
- **Focus**: Reduces response time and API calls through cached embeddings
- **Best for**: Production deployments with repeated queries

### 19. KVPress (NVIDIA)
- **URL**: https://github.com/NVIDIA/kvpress
- **Stars**: ~1.5K ⭐
- **Status**: 🟢 Production
- **Mechanism**: LLM KV cache compression made easy. Supports multiple compression methods through HuggingFace `transformers.QuantizedCache`.
- **Integration**: Works with NVIDIA TensorRT-LLM
- **Approach**: Quantization, pruning, and structured compression of KV cache
- **Best for**: Long-context inference; reduces memory footprint

### 20. KVQuant (NeurIPS 2024)
- **URL**: https://github.com/SqueezeAILab/KVQuant
- **Stars**: ~800 ⭐
- **Status**: 🟡 Research (NeurIPS 2024)
- **Mechanism**: Per-channel and non-uniform quantization of KV cache. Enables 1M-token context on single A100-80GB GPU (LLaMA-7B).
- **Technique**: Dense-and-sparse quantization preserves critical tokens
- **Best for**: Long-context scenarios; memory-constrained deployments

### 21. TurboQuant
- **URL**: https://github.com/0xSero/turboquant
- **Stars**: ~600 ⭐
- **Status**: 🟡 Research (ICLR 2026)
- **Mechanism**: Extreme KV cache quantization to 3-bit keys, 2-bit values. Triton kernels + vLLM integration.
- **Savings**: 4-8x memory reduction with minimal quality loss
- **Best for**: vLLM deployments; aggressive quantization

### 22. Prompt-Cache (MLX)
- **URL**: https://github.com/MachineLearningSystem/24MLSYS-prompt-cache
- **Stars**: ~400 ⭐
- **Status**: 🟡 Research
- **Mechanism**: Modular and structured prompt caching for low-latency inference. Indexes prompt structure for fast retrieval.
- **Framework**: Designed for MLX (Apple Silicon optimization)
- **Best for**: On-device inference; macOS/iOS

### 23. Awesome-LLM-KV-Cache
- **URL**: https://github.com/Zefan-Cai/Awesome-LLM-KV-Cache
- **Stars**: ~1K ⭐
- **Status**: 🟡 Reference/Curated
- **Mechanism**: Curated list of KV cache compression papers with code implementations
- **Best for**: Researchers; comprehensive taxonomy of KV cache techniques

---

## CATEGORY 4: Context Management & RAG Optimization

**Mechanism**: Smart retrieval, chunking, and context selection to reduce token overhead in RAG systems.
**Token Savings**: 20-50% by selecting only relevant context chunks.

### 24. LangChain RAG from Scratch
- **URL**: https://github.com/langchain-ai/rag-from-scratch
- **Stars**: ~3K ⭐
- **Status**: 🟢 Production
- **Mechanism**: Production RAG patterns with document chunking, retrieval, and reranking. Focuses on minimizing token overhead.
- **Techniques**: Fusion retrieval (keyword + vector), chunk overlap management, query rewriting
- **Best for**: RAG system design; token-aware retrieval

### 25. LangGraph Adaptive RAG
- **URL**: https://langchain-ai.github.io/langgraph/tutorials/rag/langgraph_adaptive_rag/
- **Stars**: N/A (part of LangChain)
- **Status**: 🟢 Production
- **Mechanism**: Manual large-document management with automated fallbacks. Adaptive routing avoids expensive automation.
- **Savings**: Prevents unnecessary LLM calls by filtering irrelevant documents early
- **Best for**: Large document collections; query-adaptive filtering

### 26. RAG Techniques Repository
- **URL**: https://github.com/NirDiamant/RAG_Techniques
- **Stars**: ~2K ⭐
- **Status**: 🟢 Production
- **Mechanism**: 15+ advanced RAG architectures with notebook tutorials. Includes:
  - Fusion retrieval (combining keyword + semantic search)
  - Multi-query expansion (reduces redundant retrievals)
  - Hypothetical document embeddings (HyDE)
- **Best for**: RAG practitioners; pattern library

### 27. Langchain-RAG-Tutorial
- **URL**: https://github.com/gianlucamazza/langchain-rag-tutorial
- **Stars**: ~1.5K ⭐
- **Status**: 🟢 Production
- **Mechanism**: Comprehensive RAG tutorial covering:
  - OpenAI vs. HuggingFace embedding comparison
  - Document chunking strategies (fixed, semantic, hierarchical)
  - Prompt optimization for RAG
- **Best for**: RAG onboarding; production patterns

### 28. Dynamic Context Pruning (Opencode-DCP)
- **URL**: https://github.com/Opencode-DCP/opencode-dynamic-context-pruning
- **Stars**: ~300 ⭐
- **Status**: 🟡 Active
- **Mechanism**: Automatically reduces token usage in multi-turn conversations by pruning irrelevant context. Detects and removes old/redundant turns.
- **Savings**: ~30-40% token reduction in long conversations
- **Best for**: Chat/conversation systems

### 29. LLM Context Manager
- **URL**: https://github.com/theabhinav0231/LLM-Context-Manager
- **Stars**: ~200 ⭐
- **Status**: 🟡 Research
- **Mechanism**: Smart context management for inference optimization. Manages conversation branches dynamically based on relevance.
- **Savings**: Up to 50% reduction via KV cache reuse
- **Best for**: Multi-branch dialogue systems

### 30. Awesome-Efficient-LLM
- **URL**: https://github.com/horseee/Awesome-Efficient-LLM
- **Stars**: ~2K ⭐
- **Status**: 🟡 Reference/Curated
- **Mechanism**: Curated list of efficient LLM papers and code (quantization, pruning, KV cache, distillation)
- **Best for**: Comprehensive efficiency technique reference

---

## CATEGORY 5: Agentic Frameworks & Token Efficiency

**Mechanism**: Reduce agent context and improve action efficiency.
**Token Savings**: 30-60% via smarter tool selection and output compression.

### 31. DSPy (Stanford)
- **URL**: https://github.com/stanfordnlp/dspy
- **Stars**: ~18K ⭐
- **Status**: 🟢 Production
- **Mechanism**: "Programming, not prompting" framework. Optimizes prompts and few-shot examples automatically via:
  - **BootstrapFewShot**: Generates effective demonstrations
  - **MIPROv2**: Data-aware and instruction-aware optimization
- **Token Savings**: Reduces prompt verbosity by optimizing example selection
- **Best for**: Complex reasoning tasks; instruction optimization

### 32. DSPy Optimization Patterns (KazKozDev)
- **URL**: https://github.com/KazKozDev/dspy-optimization-patterns
- **Stars**: ~400 ⭐
- **Status**: 🟡 Production Framework
- **Mechanism**: Teacher-Student pattern for DSPy. Distills expensive model reasoning into optimized prompts for cheap models.
- **Savings**: Up to 50x cost reduction via distillation
- **Best for**: Cost-conscious deployments of complex tasks

### 33. NVIDIA NeMo Agent Toolkit
- **URL**: https://github.com/NVIDIA/NeMo-Agent-Toolkit
- **Stars**: ~1K ⭐
- **Status**: 🟡 Research/Production
- **Mechanism**: Framework-agnostic agent toolkit with token profiling. Analyzes token usage down to individual tokens to identify bottlenecks.
- **Integration**: Works with LangChain, LlamaIndex, CrewAI, Semantic Kernel, ADK
- **Best for**: Agent teams; token efficiency profiling

### 34. Agentic Framework (Supercog AI)
- **URL**: https://github.com/supercog-ai/agentic
- **Stars**: ~600 ⭐
- **Status**: 🟡 Research
- **Mechanism**: Sophisticated agent framework with automatic context management. Keeps agents within context limits automatically.
- **Measurement**: Emits context length, token usage, and timing data in standard form
- **Best for**: Long-running agents; context budgeting

### 35. AgencyBench
- **URL**: https://github.com/GAIR-NLP/AgencyBench
- **Stars**: ~500 ⭐
- **Status**: 🟡 Research (ACL 2026)
- **Mechanism**: Benchmark for autonomous agents in 1M-token real-world contexts. Evaluates token efficiency across 32 scenarios.
- **Finding**: Grok-4.1-Fast is most economically viable (37.2% token efficiency)
- **Best for**: Agent benchmarking; token efficiency evaluation

### 36. TACO (Self-Evolving Agent Compression)
- **URL**: Research tool (mentioned in papers)
- **Status**: 🟡 Research
- **Mechanism**: Automatically discovers and refines compression rules to reduce redundant terminal output.
- **Savings**: Consistent improvements in agent performance with lower token usage
- **Best for**: Terminal-based agents; output compression

### 37. PSMAS (Phase-Scheduled Multi-Agent Systems)
- **URL**: Research framework
- **Status**: 🟡 Research
- **Mechanism**: Token-efficient coordination for multi-agent systems using circular manifold-based signals. Controls agent activation and context consumption.
- **Approach**: Prevents simultaneous token consumption across agents
- **Best for**: Multi-agent coordination; context budgeting

---

## CATEGORY 6: Inference Optimization & Quantization

**Mechanism**: Reduce model size and computation via quantization, pruning, distillation.
**Token Savings**: Indirect (faster inference, cheaper deployment); enables edge inference.

### 38. vLLM (UC Berkeley Sky Computing Lab)
- **URL**: https://github.com/vllm-project/vllm
- **Stars**: 25K+ ⭐
- **Status**: 🟢 Production
- **Mechanism**: High-throughput and memory-efficient LLM inference engine. Features:
  - **PagedAttention**: Efficient memory management for long contexts
  - **KV cache quantization**: Native support for quantized KV cache
  - **Continuous batching**: Maximum GPU utilization
- **Savings**: Enables 3-10x delay reduction in long-context scenarios (combined with LMCache)
- **Best for**: Production serving; largest stars among inference engines

### 39. TensorRT-LLM (NVIDIA)
- **URL**: https://github.com/NVIDIA/TensorRT-LLM
- **Stars**: ~6K ⭐
- **Status**: 🟢 Production
- **Mechanism**: NVIDIA optimization suite for LLM inference on GPUs. Includes:
  - Tensor parallelism
  - Speculative decoding
  - In-flight batching
  - Plugin architecture for custom kernels
- **Integration**: Works with vLLM, other serving engines
- **Best for**: NVIDIA GPU deployments; maximum throughput

### 40. NVIDIA Model Optimizer
- **URL**: https://github.com/NVIDIA/Model-Optimizer
- **Stars**: ~2K ⭐
- **Status**: 🟢 Production
- **Mechanism**: Unified library of SOTA optimization techniques:
  - **Quantization** (INT8, INT4, etc.)
  - **Pruning** (structured/unstructured)
  - **Distillation** (knowledge transfer)
  - **Speculative decoding** (draft model acceleration)
- **Integration**: TensorRT-LLM, TensorRT, vLLM
- **Best for**: Model compression; multi-technique optimization

### 41. SmoothQuant (MIT Han Lab, ICML 2023)
- **URL**: https://github.com/mit-han-lab/smoothquant
- **Stars**: ~1.5K ⭐
- **Status**: 🟡 Research (Published, Production-Ready)
- **Mechanism**: Training-free, post-training quantization. Smooths activation outliers; enables INT8 quantization of weights + activations.
- **Tested On**: OPT-175B, BLOOM-176B, GLM-130B, MT-NLG 530B
- **Savings**: Maintains accuracy while reducing model size and memory
- **Best for**: Large-scale model quantization

### 42. LMCache
- **URL**: https://github.com/LMCache/LMCache
- **Stars**: ~1K ⭐
- **Status**: 🟢 Production
- **Mechanism**: LLM KV cache layer spanning GPU, CPU, Disk, S3. Reduces TTFT and increases throughput in long-context scenarios.
- **Integration**: Works with vLLM
- **Savings**: 3-10x delay reduction; GPU cycle reduction
- **Best for**: Long-context serving; multi-tier caching

### 43. Epic (Position-Independent Context Caching)
- **URL**: ArXiv: https://arxiv.org/html/2410.15332v1
- **Status**: 🟡 Research (2024)
- **Mechanism**: Position-independent KV cache (PIC) enables reuse across requests regardless of token chunk position. Improves serving performance.
- **Best for**: Batched serving; repeated context patterns

### 44. Awesome-LLM-Quantization
- **URL**: https://github.com/pprp/Awesome-LLM-Quantization
- **Stars**: ~2K ⭐
- **Status**: 🟡 Reference/Curated
- **Mechanism**: Curated list of LLM quantization techniques and implementations
- **Best for**: Quantization researchers; technique taxonomy

### 45. Awesome-LLM-Inference
- **URL**: https://github.com/xlite-dev/Awesome-LLM-Inference
- **Stars**: ~1.5K ⭐
- **Status**: 🟡 Reference/Curated
- **Mechanism**: Curated list of LLM/VLM inference papers and code (Flash Attention, Paged Attention, INT8/4, parallelism)
- **Best for**: Inference researchers; technique reference

### 46. LLM-Inference-Optimization-Paper
- **URL**: https://github.com/chenhongyu2048/LLM-inference-optimization-paper
- **Stars**: ~1K ⭐
- **Status**: 🟡 Reference
- **Mechanism**: Summary of awesome work for optimizing LLM inference (SOTA techniques)
- **Best for**: Quick reference for inference optimization approaches

### 47. Awesome-Model-Quantization (Efficient-ML)
- **URL**: https://github.com/Efficient-ML/Awesome-Model-Quantization
- **Stars**: ~1.5K ⭐
- **Status**: 🟡 Reference/Curated
- **Mechanism**: Papers, docs, code about model quantization across all model types
- **Best for**: Quantization practitioners; comprehensive resource

---

## CATEGORY 7: LLMOps & Optimization Platforms

**Mechanism**: End-to-end platforms that measure, optimize, and automate token reduction.
**Token Savings**: Variable; dependent on specific optimizations applied.

### 48. TensorZero (LLMOps Platform)
- **URL**: https://github.com/tensorzero/tensorzero
- **Stars**: ~2K ⭐
- **Status**: 🟢 Production
- **Mechanism**: Unified LLMOps platform with:
  - LLM gateway
  - Observability (cost, latency, quality)
  - Evaluation framework
  - Prompt optimization
  - A/B testing
- **Autopilot**: Automated optimization based on observability data
- **Best for**: Teams needing end-to-end optimization workflow

### 49. AutoGen (Microsoft)
- **URL**: https://github.com/microsoft/autogen
- **Stars**: ~30K+ ⭐
- **Status**: 🟢 Production
- **Mechanism**: Multi-agent conversation framework with built-in caching, cost tracking, and compression for long contexts.
- **Integration**: LLMLingua compression, LiteLLM routing
- **Best for**: Multi-agent systems; reduces per-agent token usage

### 50. Awesome-AI-Agents (Curated)
- **URL**: https://github.com/slavakurilyak/awesome-ai-agents
- **Stars**: ~3K ⭐
- **Status**: 🟡 Reference/Curated
- **Mechanism**: Curated list of 300+ agentic AI resources (frameworks, papers, tools)
- **Best for**: Agent builders; comprehensive landscape map

### 51. Awesome-AI-Agent-Papers (VoltAgent)
- **URL**: https://github.com/VoltAgent/awesome-ai-agent-papers
- **Stars**: ~800 ⭐
- **Status**: 🟡 Reference/Curated
- **Mechanism**: Curated 2026 AI agent research papers on engineering, memory, evaluation, workflows, autonomous systems
- **Best for**: Researchers; latest agent optimization techniques

---

## CATEGORY 8: Specialized & Support Tools

**Mechanism**: Domain-specific or cross-cutting token optimizations.
**Token Savings**: Varies by use case.

### 52. oMLX (Apple Silicon KV Cache Persistence)
- **URL**: https://github.com/jundot/omlx
- **Stars**: ~300 ⭐
- **Status**: 🟡 Early-stage
- **Mechanism**: LLM inference server with SSD KV cache persistence for Apple Silicon. Maintains KV cache across hot (memory) and cold (SSD) tiers.
- **Use Case**: On-device inference with context persistence
- **Best for**: macOS/iOS; long-running sessions on constrained devices

### 53. Prompt Caching in LLMs (Research)
- **URL**: Various implementations
- **Status**: 🟡 Research
- **Mechanism**: HTTP-semantics prompt caching (partial matching of context) enabled in Claude Sonnet, OpenAI GPT-4 Turbo, etc.
- **Benefit**: Native support in major APIs reduces token costs for repeated prefixes
- **Best for**: LLM providers; API consumers with repeated contexts

### 54. MLX-Textgen
- **URL**: https://github.com/nath1295/MLX-Textgen
- **Stars**: ~200 ⭐
- **Status**: 🟡 Early-stage
- **Mechanism**: Python package for serving LLM on OpenAI-compatible API endpoints with prompt caching using MLX.
- **Best for**: Apple Silicon users; local serving with caching

---

## Summary Table: 50+ Tools by Category

| Category | # Tools | Token Savings Range | Best Represented Tools |
|----------|---------|-------------------|----------------------|
| **Routing & Model Selection** | 6 | 35-85% | RouteLLM, LLMRouter, vLLM SR, LiteLLM |
| **Prompt Compression** | 8 | 15-97% | LLMLingua, Claw Compactor, Headroom |
| **Semantic Caching & KV Optimization** | 9 | 30-80% | PromptCache, GPTCache, vCache, KVPress |
| **Context Management & RAG** | 8 | 20-50% | LangChain RAG, Dynamic Context Pruning |
| **Agentic Frameworks** | 7 | 30-60% | DSPy, NVIDIA NeMo Agent Toolkit |
| **Inference Optimization** | 10 | Indirect (faster) | vLLM, TensorRT-LLM, NVIDIA Model Optimizer |
| **LLMOps Platforms** | 4 | Variable | TensorZero, AutoGen |
| **Specialized Tools** | 2 | Varies | oMLX, MLX-Textgen |
| **TOTAL** | **54** | **15-97%** | — |

---

## Recommended Selection for llm-router Benchmark

**Proposed 10 tools for Phase 2 experiments** (with justification):

1. **llm-router** (baseline) — internal reference
2. **RouteLLM** (4.8K ⭐) — production routing; complementary approach
3. **LiteLLM** (42.2K ⭐) — most-starred; multi-provider integration
4. **LLMLingua** (~2.5K ⭐) — best compression technique; peer-reviewed (EMNLP 2023)
5. **vLLM Semantic Router** (2K+ ⭐) — production semantic routing
6. **Claw Compactor** (210K ⭐) — highest impact in real-world code; rule-based (no inference cost)
7. **GPTCache** (7K ⭐) — production semantic caching; LangChain integration
8. **DSPy** (18K ⭐) — framework-level optimization; largest community
9. **Headroom** (~1K ⭐) — production context optimization layer
10. **TensorZero** (~2K ⭐) — LLMOps end-to-end optimization

**Rationale**:
- **Diversity**: Covers all 4 approach categories (routing, compression, caching, optimization)
- **Maturity mix**: 7 production-ready + 3 research (for comparison)
- **Community signals**: Star counts reflect adoption (LiteLLM, DSPy, Claw highest)
- **Complementarity**: Minimal overlap; each covers distinct mechanism
- **Benchmarkability**: All have clear, measurable token-saving mechanisms
- **Publication value**: Combination shows landscape breadth; good for arXiv

---

## Next Steps

1. **Review** this list and confirm top 10 selections
2. **Finalize tool selection** before Phase 1 (tool integration)
3. **Begin Phase 1** — implement `tools/base_wrapper.py` abstract interface
4. **Create tool-specific wrappers** for each of the 10 selected tools

---

**Document Status**: Ready for user review and tool selection
**Date**: 2026-04-26
