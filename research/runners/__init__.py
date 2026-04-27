"""
Runners for orchestrating tool benchmarking.

Modules:
- benchmark_runner: Main orchestrator coordinating all 1,600 tasks × 10 tools × 16 variants
- tool_runner: Per-tool execution with timeout/retry logic
- quality_evaluator: LLM-based quality assessment
"""
