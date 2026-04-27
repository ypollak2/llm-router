"""
Real-time progress monitoring for benchmark execution.

Tracks:
- Execution progress (completed vs total)
- Success/failure rates
- Performance metrics (compression, latency)
- ETA calculation
"""

import json
import time
from pathlib import Path
from typing import Dict, Optional
from collections import defaultdict


class ProgressMonitor:
    """Monitor benchmark execution progress in real-time."""

    def __init__(self, results_file: Path = Path("results/benchmark_results.jsonl")):
        """Initialize progress monitor.

        Args:
            results_file: Path to results JSONL file
        """
        self.results_file = results_file
        self.start_time: Optional[float] = None
        self.last_count = 0

    def start(self) -> None:
        """Start monitoring."""
        self.start_time = time.time()
        self.last_count = 0
        print("📊 Progress monitor started")

    def get_stats(self) -> Dict:
        """Get current benchmark statistics.

        Returns:
            Dict with counts, rates, metrics, and ETA
        """
        if not self.results_file.exists():
            return {
                "results": 0,
                "success": 0,
                "failed": 0,
                "success_rate": 0.0,
                "avg_compression": 0.0,
                "avg_latency_ms": 0.0,
                "elapsed_sec": 0,
                "eta_sec": None,
            }

        results = []
        success_count = 0
        failed_count = 0
        compression_sum = 0.0
        compression_count = 0
        latency_sum = 0.0

        with open(self.results_file, "r") as f:
            for line in f:
                try:
                    result = json.loads(line)
                    results.append(result)

                    if result["success"]:
                        success_count += 1
                    else:
                        failed_count += 1

                    if result["compression_ratio"] is not None:
                        compression_sum += result["compression_ratio"]
                        compression_count += 1

                    latency_sum += result["latency_ms"]
                except json.JSONDecodeError:
                    continue

        total = len(results)
        elapsed = time.time() - self.start_time if self.start_time else 0
        success_rate = (success_count / total * 100) if total > 0 else 0

        # Estimate ETA
        if total > self.last_count and elapsed > 0:
            rate = (total - self.last_count) / elapsed  # results per second
            remaining = (1600 * 10 * 4) - total  # estimated total executions
            eta = remaining / rate if rate > 0 else None
        else:
            eta = None

        self.last_count = total

        return {
            "results": total,
            "success": success_count,
            "failed": failed_count,
            "success_rate": success_rate,
            "avg_compression": (
                compression_sum / compression_count if compression_count > 0 else 0.0
            ),
            "avg_latency_ms": latency_sum / total if total > 0 else 0.0,
            "elapsed_sec": int(elapsed),
            "eta_sec": int(eta) if eta else None,
        }

    def print_progress(self) -> None:
        """Print formatted progress report."""
        stats = self.get_stats()

        elapsed_min = stats["elapsed_sec"] / 60
        eta_min = stats["eta_sec"] / 60 if stats["eta_sec"] else None

        print(f"\n{'='*60}")
        print(f"📈 Benchmark Progress")
        print(f"{'='*60}")
        print(f"Results collected:    {stats['results']:,}")
        print(f"Successes:            {stats['success']:,}")
        print(f"Failures:             {stats['failed']:,}")
        print(f"Success rate:         {stats['success_rate']:.1f}%")
        print(f"Avg compression:      {stats['avg_compression']:.2f}")
        print(f"Avg latency:          {stats['avg_latency_ms']:.0f}ms")
        print(f"Elapsed time:         {elapsed_min:.1f} minutes")
        if eta_min:
            print(f"ETA:                  {eta_min:.1f} minutes remaining")
        print(f"{'='*60}\n")

    def get_tool_stats(self) -> Dict[str, Dict]:
        """Get per-tool statistics.

        Returns:
            Dict mapping tool_name to statistics
        """
        if not self.results_file.exists():
            return {}

        tool_stats = defaultdict(lambda: {
            "count": 0,
            "success": 0,
            "failed": 0,
            "avg_compression": 0.0,
            "avg_latency_ms": 0.0,
        })

        with open(self.results_file, "r") as f:
            for line in f:
                try:
                    result = json.loads(line)
                    tool = result["tool_name"]

                    tool_stats[tool]["count"] += 1
                    if result["success"]:
                        tool_stats[tool]["success"] += 1
                    else:
                        tool_stats[tool]["failed"] += 1

                    if result["compression_ratio"] is not None:
                        tool_stats[tool]["avg_compression"] += result["compression_ratio"]

                    tool_stats[tool]["avg_latency_ms"] += result["latency_ms"]
                except json.JSONDecodeError:
                    continue

        # Calculate averages
        for tool, stats in tool_stats.items():
            if stats["count"] > 0:
                stats["avg_compression"] = stats["avg_compression"] / stats["count"]
                stats["avg_latency_ms"] = stats["avg_latency_ms"] / stats["count"]
                stats["success_rate"] = (
                    stats["success"] / stats["count"] * 100 if stats["count"] > 0 else 0
                )

        return dict(tool_stats)

    def print_tool_stats(self) -> None:
        """Print per-tool performance statistics."""
        stats = self.get_tool_stats()

        print(f"\n{'='*80}")
        print(f"📊 Per-Tool Statistics")
        print(f"{'='*80}")
        print(f"{'Tool':<20} {'Count':>8} {'Success':>8} {'Compression':>12} {'Latency':>10}")
        print(f"{'-'*80}")

        for tool, data in sorted(stats.items(), key=lambda x: x[1]["count"], reverse=True):
            print(
                f"{tool:<20} {data['count']:>8} {data['success']:>8} "
                f"{data['avg_compression']:>12.2f} {data['avg_latency_ms']:>10.0f}ms"
            )

        print(f"{'='*80}\n")


async def demo():
    """Demo progress monitoring."""
    monitor = ProgressMonitor()
    monitor.start()

    # Simulate progress updates
    for i in range(5):
        await asyncio.sleep(2)
        monitor.print_progress()
        if i == 4:
            monitor.print_tool_stats()


if __name__ == "__main__":
    import asyncio

    asyncio.run(demo())
