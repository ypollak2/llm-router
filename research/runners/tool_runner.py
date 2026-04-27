"""
Per-tool execution runner.

Handles:
- Individual tool initialization and cleanup
- Timeout enforcement
- Retry logic
- Metric collection
- Error handling
"""

import asyncio
import time
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)


class ToolRunner:
    """Executes a single tool with timeout and retry logic."""

    def __init__(
        self,
        tool_wrapper: Any,
        timeout_sec: int = 30,
        max_retries: int = 2,
    ):
        """Initialize tool runner.

        Args:
            tool_wrapper: Initialized tool wrapper instance
            timeout_sec: Timeout for execution in seconds
            max_retries: Number of retries on failure
        """
        self.tool = tool_wrapper
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.execution_count = 0
        self.success_count = 0
        self.error_count = 0

    async def execute(
        self,
        task_input: Any,
        technique_variant: str = "default",
    ) -> Optional[Any]:
        """Execute tool with timeout and retry.

        Args:
            task_input: Input for the tool
            technique_variant: Technique variant to use

        Returns:
            ToolOutput or None on failure
        """
        self.execution_count += 1

        for attempt in range(self.max_retries):
            try:
                start_time = time.time()

                # Execute with timeout
                output = await asyncio.wait_for(
                    self.tool.execute(task_input, technique_variant),
                    timeout=self.timeout_sec,
                )

                elapsed = time.time() - start_time
                self.success_count += 1

                logger.debug(
                    f"{self.tool.tool_name}[{technique_variant}] completed in {elapsed:.2f}s"
                )

                return output

            except asyncio.TimeoutError:
                logger.warning(
                    f"{self.tool.tool_name} timeout (attempt {attempt+1}/{self.max_retries})"
                )
                if attempt == self.max_retries - 1:
                    self.error_count += 1
                    return None

            except Exception as e:
                logger.warning(
                    f"{self.tool.tool_name} error: {e} (attempt {attempt+1}/{self.max_retries})"
                )
                if attempt == self.max_retries - 1:
                    self.error_count += 1
                    return None

            # Brief delay before retry
            await asyncio.sleep(0.1 * (attempt + 1))

        self.error_count += 1
        return None

    async def cleanup(self) -> None:
        """Cleanup tool resources."""
        try:
            await self.tool.cleanup()
        except Exception as e:
            logger.error(f"Error cleaning up {self.tool.tool_name}: {e}")

    def get_stats(self) -> dict:
        """Get execution statistics."""
        return {
            "tool_name": self.tool.tool_name,
            "executions": self.execution_count,
            "successes": self.success_count,
            "errors": self.error_count,
            "success_rate": (
                self.success_count / self.execution_count * 100
                if self.execution_count > 0
                else 0
            ),
        }


class BatchToolRunner:
    """Runs a tool across multiple tasks in batches."""

    def __init__(
        self,
        tool_wrapper: Any,
        timeout_sec: int = 30,
        max_retries: int = 2,
        batch_size: int = 10,
    ):
        """Initialize batch runner."""
        self.runner = ToolRunner(tool_wrapper, timeout_sec, max_retries)
        self.batch_size = batch_size
        self.results = []

    async def execute_batch(
        self,
        task_inputs: list,
        technique_variant: str = "default",
    ) -> list:
        """Execute tool across multiple tasks.

        Args:
            task_inputs: List of task inputs
            technique_variant: Technique variant to use

        Returns:
            List of results (may include None for failures)
        """
        self.results = []

        for i, task_input in enumerate(task_inputs):
            output = await self.runner.execute(task_input, technique_variant)
            self.results.append(output)

            if (i + 1) % self.batch_size == 0:
                logger.info(f"Completed {i+1}/{len(task_inputs)} tasks")

        return self.results

    async def cleanup(self) -> None:
        """Cleanup runner."""
        await self.runner.cleanup()

    def get_stats(self) -> dict:
        """Get batch execution statistics."""
        stats = self.runner.get_stats()
        stats["batch_size"] = self.batch_size
        stats["total_results"] = len(self.results)
        stats["successful_results"] = sum(1 for r in self.results if r is not None)
        return stats
