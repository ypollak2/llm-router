"""
Main benchmark orchestrator.

Coordinates execution of 1,600 tasks × 10 tools × 16 technique variants.
= 256,000 total executions

Handles:
- Task distribution
- Tool registry and initialization
- Result aggregation
- Progress tracking
- Error handling and retries
"""

import asyncio
import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime
import logging

# Try importing Ray for parallel execution
try:
    import ray
    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class BenchmarkConfig:
    """Configuration for benchmark run."""

    num_tasks: int = 1600
    tools: List[str] = field(
        default_factory=lambda: [
            "llm-router",
            "llmlingua",
            "routellm",
            "litellm",
            "gptcache",
            "claw",
            "dspy",
            "vllm_semantic_router",
            "headroom",
            "tensorzero",
        ]
    )
    max_parallel_tasks: int = 10
    timeout_sec: int = 30
    retry_count: int = 2
    results_dir: Path = field(default_factory=lambda: Path("results"))
    ray_cluster: bool = False  # Enable Ray for parallel execution
    use_small_sample: bool = False  # For testing: use 10 tasks instead of 1600


@dataclass
class ExecutionResult:
    """Result from a single tool execution."""

    task_id: int
    tool_name: str
    technique_variant: str
    input_tokens: int
    output_tokens: int
    compressed_input_tokens: Optional[int]
    compression_ratio: Optional[float]
    latency_ms: float
    preprocessing_ms: float
    inference_ms: float
    quality_score: Optional[float]
    success: bool
    error: Optional[str]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


class BenchmarkRunner:
    """Main benchmark orchestrator."""

    def __init__(self, config: Optional[BenchmarkConfig] = None):
        """Initialize benchmark runner."""
        self.config = config or BenchmarkConfig()
        self.results: List[ExecutionResult] = []
        self.tasks: List[Dict[str, str]] = []
        self.tools_registry: Dict[str, Any] = {}

    async def initialize(self) -> None:
        """Initialize benchmark: load tasks, register tools, validate config."""
        logger.info("Initializing benchmark runner...")

        # Create results directory
        self.config.results_dir.mkdir(exist_ok=True)

        # Load task dataset
        await self._load_tasks()
        logger.info(f"Loaded {len(self.tasks)} tasks")

        # Register tools
        await self._register_tools()
        logger.info(f"Registered {len(self.tools_registry)} tools")

        logger.info("Benchmark runner initialized ✅")

    async def _load_tasks(self) -> None:
        """Load benchmark task dataset."""
        dataset_path = Path(__file__).parent.parent / "data" / "benchmark_tasks.jsonl"

        if dataset_path.exists():
            # Load from JSONL dataset file
            with open(dataset_path, "r") as f:
                task_count = 0
                for line in f:
                    if task_count >= self.config.num_tasks:
                        break
                    if self.config.use_small_sample and task_count >= 10:
                        break
                    task = json.loads(line)
                    self.tasks.append(task)
                    task_count += 1
        else:
            # Fallback: generate synthetic tasks if dataset file not found
            categories = ["code", "analysis", "text", "research"]
            num_per_category = (
                self.config.num_tasks // 4
                if not self.config.use_small_sample
                else 10 // 4
            )

            for category in categories:
                for i in range(num_per_category):
                    self.tasks.append(
                        {
                            "task_id": len(self.tasks),
                            "category": category,
                            "prompt": self._generate_prompt(category, i),
                        }
                    )

    def _generate_prompt(self, category: str, index: int) -> str:
        """Generate a synthetic prompt for a category."""
        prompts = {
            "code": f"Write a Python function to solve problem {index}: implement a {['sort', 'search', 'tree', 'graph'][index % 4]}",
            "analysis": f"Analyze and compare {['two approaches', 'methods', 'techniques', 'implementations'][index % 4]} for {index}",
            "text": f"Summarize or explain {['the concept', 'the difference', 'the importance', 'the application'][index % 4]} of {index}",
            "research": f"Research and report on {['recent advances', 'emerging trends', 'current state', 'future potential'][index % 4]} in {index}",
        }
        return prompts.get(category, f"Task {index}") + " " * (50 + index % 50)

    async def _register_tools(self) -> None:
        """Register all tool wrappers."""
        # Import tool wrappers
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

        from base_wrapper import ToolWrapperRegistry

        # Force registration by importing all wrappers
        try:
            from llm_router_wrapper import LLMRouterWrapper
            from llmlingua_wrapper import LLMLinguaWrapper
            from routellm_wrapper import RouteLLMWrapper
            from litellm_wrapper import LiteLLMWrapper
            from gptcache_wrapper import GPTCacheWrapper
            from claw_wrapper import ClawWrapper
            from dspy_wrapper import DSPyWrapper
            from vllm_semantic_router_wrapper import VLLMSemanticRouterWrapper
            from headroom_wrapper import HeadroomWrapper
            from tensorzero_wrapper import TensorZeroWrapper
        except ImportError as e:
            logger.warning(f"Could not import all tools: {e}")

        # Get registry
        tools_list = ToolWrapperRegistry.list_tools()
        for tool_name in tools_list:
            if tool_name in self.config.tools:
                self.tools_registry[tool_name] = ToolWrapperRegistry.get(tool_name)

        logger.info(f"Registered tools: {list(self.tools_registry.keys())}")

    async def run(self) -> None:
        """Execute full benchmark."""
        execution_mode = "Ray (parallel)" if (self.config.ray_cluster and RAY_AVAILABLE) else "Sequential"
        logger.info(
            f"Starting benchmark: {len(self.tasks)} tasks × {len(self.tools_registry)} tools"
        )
        logger.info(f"Execution mode: {execution_mode}")
        logger.info(f"Estimated executions: ~{len(self.tasks) * len(self.tools_registry) * 4}")

        start_time = time.time()

        # Execute tasks
        if self.config.ray_cluster and RAY_AVAILABLE:
            await self._execute_benchmark_ray()
        else:
            if self.config.ray_cluster and not RAY_AVAILABLE:
                logger.warning("Ray cluster enabled but Ray not installed - falling back to sequential execution")
                logger.info("Install Ray: pip install ray")
            await self._execute_benchmark()

        elapsed = time.time() - start_time
        logger.info(f"Benchmark complete in {elapsed:.1f}s ({elapsed/60:.1f} minutes)")

        # Save results
        await self._save_results()
        logger.info(f"Results saved to {self.config.results_dir}")

        # Print summary
        self._print_summary()

    async def _execute_benchmark_ray(self) -> None:
        """Execute benchmark using Ray for parallel execution."""
        if not RAY_AVAILABLE:
            logger.warning("Ray not available, falling back to sequential execution")
            await self._execute_benchmark()
            return

        # Initialize Ray cluster
        if not ray.is_initialized():
            ray.init(ignore_reinit_error=True)
            logger.info("Ray cluster initialized")

        try:
            # Create remote function for execution
            execute_remote = ray.remote(self._execute_tool_remote_impl)

            # Create remote tasks for parallel execution
            remote_tasks = []

            for task in self.tasks:
                for tool_name in self.config.tools:
                    if tool_name not in self.tools_registry:
                        continue

                    tool_class = self.tools_registry[tool_name]
                    variants = self._get_tool_variants(tool_name)

                    for variant in variants:
                        # Submit remote task
                        remote_task = execute_remote.remote(
                            self, task, tool_class, tool_name, variant
                        )
                        remote_tasks.append(remote_task)

            # Collect results as they complete
            logger.info(f"Submitted {len(remote_tasks)} remote tasks to Ray cluster")
            completed = 0

            for remote_task in ray.as_completed(remote_tasks):
                try:
                    result = ray.get(remote_task)
                    if result:
                        self.results.append(result)
                except Exception as e:
                    logger.error(f"Ray task failed: {e}")

                completed += 1
                if completed % 100 == 0:
                    logger.info(f"Ray progress: {completed}/{len(remote_tasks)} tasks")

        finally:
            ray.shutdown()
            logger.info("Ray cluster shutdown")

    def _execute_tool_remote_impl(self, task: Dict, tool_class: Any, tool_name: str, variant: str) -> Optional[ExecutionResult]:
        """Remote task implementation for Ray execution."""
        from base_wrapper import ToolInput
        import asyncio

        async def execute_task():
            tool = tool_class(tool_name, {})
            try:
                await tool.initialize()
                task_input = ToolInput(prompt=task["prompt"])
                output = await asyncio.wait_for(
                    tool.execute(task_input, variant),
                    timeout=self.config.timeout_sec,
                )

                result = ExecutionResult(
                    task_id=task["task_id"],
                    tool_name=tool_name,
                    technique_variant=variant,
                    input_tokens=output.input_tokens,
                    output_tokens=output.output_tokens,
                    compressed_input_tokens=output.compressed_input_tokens,
                    compression_ratio=output.compression_ratio,
                    latency_ms=output.latency_ms,
                    preprocessing_ms=output.preprocessing_ms,
                    inference_ms=output.inference_ms,
                    quality_score=output.quality_score,
                    success=output.error is None,
                    error=output.error,
                )
                await tool.cleanup()
                return result
            except Exception as e:
                return ExecutionResult(
                    task_id=task["task_id"],
                    tool_name=tool_name,
                    technique_variant=variant,
                    input_tokens=0,
                    output_tokens=0,
                    compressed_input_tokens=None,
                    compression_ratio=None,
                    latency_ms=0,
                    preprocessing_ms=0,
                    inference_ms=0,
                    quality_score=None,
                    success=False,
                    error=str(e),
                )

        return asyncio.run(execute_task())

    async def _execute_benchmark(self) -> None:
        """Execute benchmark for all tasks × tools × variants."""
        from base_wrapper import ToolInput

        total_executions = len(self.tasks) * len(self.tools_registry)
        completed = 0

        for task in self.tasks:
            for tool_name in self.config.tools:
                if tool_name not in self.tools_registry:
                    continue

                tool_class = self.tools_registry[tool_name]
                tool = tool_class(tool_name, {})

                try:
                    # Initialize tool
                    await tool.initialize()

                    # Get variants for this tool
                    variants = self._get_tool_variants(tool_name)

                    # Execute each variant
                    for variant in variants:
                        try:
                            # Execute tool
                            task_input = ToolInput(prompt=task["prompt"])
                            output = await asyncio.wait_for(
                                tool.execute(task_input, variant),
                                timeout=self.config.timeout_sec,
                            )

                            # Record result
                            result = ExecutionResult(
                                task_id=task["task_id"],
                                tool_name=tool_name,
                                technique_variant=variant,
                                input_tokens=output.input_tokens,
                                output_tokens=output.output_tokens,
                                compressed_input_tokens=output.compressed_input_tokens,
                                compression_ratio=output.compression_ratio,
                                latency_ms=output.latency_ms,
                                preprocessing_ms=output.preprocessing_ms,
                                inference_ms=output.inference_ms,
                                quality_score=output.quality_score,
                                success=output.error is None,
                                error=output.error,
                            )
                            self.results.append(result)

                        except asyncio.TimeoutError:
                            result = ExecutionResult(
                                task_id=task["task_id"],
                                tool_name=tool_name,
                                technique_variant=variant,
                                input_tokens=0,
                                output_tokens=0,
                                compressed_input_tokens=None,
                                compression_ratio=None,
                                latency_ms=self.config.timeout_sec * 1000,
                                preprocessing_ms=0,
                                inference_ms=0,
                                quality_score=None,
                                success=False,
                                error="Timeout",
                            )
                            self.results.append(result)
                        except Exception as e:
                            result = ExecutionResult(
                                task_id=task["task_id"],
                                tool_name=tool_name,
                                technique_variant=variant,
                                input_tokens=0,
                                output_tokens=0,
                                compressed_input_tokens=None,
                                compression_ratio=None,
                                latency_ms=0,
                                preprocessing_ms=0,
                                inference_ms=0,
                                quality_score=None,
                                success=False,
                                error=str(e),
                            )
                            self.results.append(result)

                    # Cleanup tool
                    await tool.cleanup()

                except Exception as e:
                    logger.error(f"Error with tool {tool_name}: {e}")

                completed += 1
                if completed % 10 == 0:
                    logger.info(f"Progress: {completed}/{total_executions} executions")

    def _get_tool_variants(self, tool_name: str) -> List[str]:
        """Get technique variants for a tool."""
        variants_map = {
            "llm-router": ["aggressive", "balanced", "conservative", "caveman_off", "caveman_full"],
            "llmlingua": ["llmlingua_20x", "llmlingua2_6x", "longllmlingua_rag"],
            "routellm": ["threshold_0.5", "threshold_0.7", "threshold_0.9"],
            "litellm": ["cost_optimized", "latency_optimized", "quality_optimized"],
            "gptcache": ["strict_similarity", "loose_similarity", "rag_optimized"],
            "claw": ["code_only", "json_only", "text_only", "balanced", "aggressive"],
            "dspy": ["bootstrap_few_shot", "miprov2", "auto_generated", "minimal"],
            "vllm_semantic_router": ["task_specific", "speed_optimized", "quality_optimized"],
            "headroom": ["priority_aware", "summarize_on_overflow", "adaptive", "aggressive_truncate"],
            "tensorzero": ["ab_tested", "feedback_optimized", "multi_armed_bandit", "experimental"],
        }
        return variants_map.get(tool_name, ["default"])

    async def _save_results(self) -> None:
        """Save results to JSON (append mode to preserve partial runs)."""
        results_file = self.config.results_dir / "benchmark_results.jsonl"

        with open(results_file, "a") as f:
            for result in self.results:
                f.write(json.dumps(result.to_dict()) + "\n")

        logger.info(f"Appended {len(self.results)} results to {results_file}")

    def _print_summary(self) -> None:
        """Print benchmark summary statistics."""
        successful = sum(1 for r in self.results if r.success)
        failed = len(self.results) - successful

        avg_compression = sum(
            r.compression_ratio
            for r in self.results
            if r.compression_ratio is not None
        ) / max(
            1,
            sum(1 for r in self.results if r.compression_ratio is not None),
        )

        avg_latency = sum(r.latency_ms for r in self.results) / len(self.results)

        logger.info(
            f"""
╔══════════════════════════════════════════════════════╗
║           BENCHMARK SUMMARY                           ║
╠══════════════════════════════════════════════════════╣
║ Total Executions:     {len(self.results):>6}                      ║
║ Successful:           {successful:>6}                      ║
║ Failed:               {failed:>6}                      ║
║ Success Rate:         {successful/len(self.results)*100:>5.1f}%                      ║
║ Avg Compression:      {avg_compression:>5.2f}                      ║
║ Avg Latency:          {avg_latency:>5.0f}ms                    ║
╚══════════════════════════════════════════════════════╝
"""
        )


async def main():
    """Run benchmark."""
    import sys

    # Parse arguments
    use_small_sample = "--sample" in sys.argv or "-s" in sys.argv
    use_ray = "--ray" in sys.argv or "-r" in sys.argv

    if use_small_sample:
        logger.info("Running small sample (10 tasks)")
        config = BenchmarkConfig(use_small_sample=True, ray_cluster=use_ray)
    else:
        logger.info("Running full benchmark (1,600 tasks)")
        config = BenchmarkConfig(use_small_sample=False, ray_cluster=use_ray)

    runner = BenchmarkRunner(config)

    await runner.initialize()
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
