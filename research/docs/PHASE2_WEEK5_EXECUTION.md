# Phase 2 Week 5 — Full Benchmark Execution

**Status**: ✅ READY TO START
**Date**: 2026-04-26
**Scope**: Execute full benchmark on 1,600 diverse tasks

---

## Overview

Phase 2 Week 5 executes the complete benchmark across:
- **1,600 tasks** (4 categories: code, analysis, text, research)
- **10 tools** (compression, routing, caching, optimization, learning)
- **16+ variants per tool** (3-6 technique variants each)
- **Total executions**: ~256,000 (1,600 × 10 × 4 = 64,000 base, more with all variants)

---

## Execution Modes

### Option 1: Sequential Execution (Default)
**Best for**: Single machine testing, verification
**Time estimate**: 2-4 hours for full 1,600-task dataset

```bash
# Run full benchmark sequentially
python3 runners/benchmark_runner.py

# Run small sample (10 tasks) for testing
python3 runners/benchmark_runner.py --sample
```

### Option 2: Ray Cluster (Parallel)
**Best for**: Full-scale benchmarking, distributed execution
**Time estimate**: 30-60 minutes with 8+ workers
**Requires**: Ray (pip install ray)

```bash
# Install Ray first
pip install ray

# Run full benchmark with Ray parallelization
python3 runners/benchmark_runner.py --ray

# Run sample with Ray
python3 runners/benchmark_runner.py --sample --ray
```

---

## Execution Flow

### 1. Initialization
```
✅ Load 1,600 tasks from data/benchmark_tasks.jsonl
✅ Register all 10 tools via ToolWrapperRegistry
✅ Create results directory (results/)
```

### 2. Task Distribution
```
Sequential:   tasks[0..1599] → tools → variants → execute serially
Ray Cluster:  tasks[0..1599] → remote actors → parallel execution
```

### 3. Per-Execution
```
For each (task, tool, variant):
  ├─ Initialize tool (setup, load models)
  ├─ Execute with timeout (30s default)
  ├─ Retry on failure (2x default)
  └─ Record ExecutionResult
    ├─ input/output/compressed tokens
    ├─ compression_ratio
    ├─ latency breakdown (preprocessing, inference)
    ├─ quality_score (automated, null for now)
    ├─ success boolean
    └─ error message (if failed)
```

### 4. Result Aggregation
```
Save all results to: results/benchmark_results.jsonl
├─ 256,000+ ExecutionResult records (one per line)
├─ All metrics captured
├─ Timestamps in ISO 8601 format
└─ Ready for Phase 2 Week 6 quality evaluation
```

---

## Monitoring Progress

### Real-Time Progress Monitor
```python
from runners.progress_monitor import ProgressMonitor

monitor = ProgressMonitor()
monitor.start()

# During execution:
monitor.print_progress()      # Overall stats
monitor.print_tool_stats()    # Per-tool breakdown
```

### Example Output
```
============================================================
📈 Benchmark Progress
============================================================
Results collected:    12,450
Successes:            12,450
Failures:             0
Success rate:         100.0%
Avg compression:      0.52
Avg latency:          72ms
Elapsed time:         8.3 minutes
ETA:                  41.7 minutes remaining
============================================================
```

---

## Expected Results

### Success Metrics
- **Success rate**: Target 95-100% (no timeouts/crashes)
- **Average compression**: 0.50-0.65 (50-65% of original)
- **Average latency**: 50-200ms (varies by tool)
- **Total results**: 256,000+ ExecutionResult records

### Output Structure
```
results/
├── benchmark_results.jsonl         # All 256,000+ results
├── benchmark_results.csv           # Summary stats
└── benchmark_summary.txt           # Human-readable summary
```

### Result Sample
```json
{
  "task_id": 42,
  "tool_name": "llmlingua",
  "technique_variant": "llmlingua_20x",
  "input_tokens": 150,
  "output_tokens": 45,
  "compressed_input_tokens": 10,
  "compression_ratio": 0.067,
  "latency_ms": 142.5,
  "preprocessing_ms": 140.2,
  "inference_ms": 2.3,
  "quality_score": null,
  "success": true,
  "error": null,
  "timestamp": "2026-04-26T21:38:06.570935"
}
```

---

## Ray Cluster Configuration

### Single Machine (8-16 cores)
```python
ray.init(
    num_cpus=8,
    num_gpus=1,  # if available
    object_store_memory=4e9,
)
```

### Multi-Machine (distributed)
```python
ray.init(address="auto")  # Connect to existing cluster
# or
subprocess.run(["ray", "start", "--head"])
```

### Resource Allocation
- **CPU**: 1-2 per remote task
- **GPU**: 0.5 per remote task (if GPU compression tools)
- **Memory**: 2GB per worker (tune based on tool requirements)

---

## Performance Tuning

### For Faster Execution
1. **Increase batch size**: More tasks per batch
2. **Enable Ray**: Use distributed execution
3. **Reduce timeout**: Lower per-task timeout if tools are fast
4. **Skip retries**: Set max_retries=0 for confident tools

### For Better Data Quality
1. **Increase timeout**: Allow slower tools more time
2. **Enable retries**: Catch transient failures
3. **Reduce parallel**: Run sequential to catch edge cases
4. **Add logging**: Detailed tool-level tracing

### Example Config for Speed
```python
config = BenchmarkConfig(
    num_tasks=1600,
    max_parallel_tasks=32,      # Higher parallelism
    timeout_sec=20,              # Shorter timeout
    retry_count=0,               # No retries
    ray_cluster=True,            # Use Ray
)
```

---

## Error Handling

### Common Issues

**1. Timeout (tool takes >30s)**
- Solution: Increase timeout_sec in config
- Or: Tool needs optimization

**2. Memory exhaustion (OOM)**
- Solution: Reduce parallel tasks or Ray workers
- Or: Use swap/memory management

**3. Ray connection failed**
- Solution: Check Ray cluster status: `ray status`
- Or: Fall back to sequential mode

**4. Tool initialization fails**
- Solution: Check tool wrapper implementation
- Or: Verify dependencies installed

### Debug Mode
```bash
# Verbose logging
export LOGLEVEL=DEBUG
python3 runners/benchmark_runner.py --sample

# Profile execution
python3 -m cProfile -o benchmark.prof runners/benchmark_runner.py --sample

# Analyze profile
python3 -m pstats benchmark.prof
```

---

## Next Steps (Phase 2 Week 6)

Once execution completes:
1. Verify 256,000+ results in results/benchmark_results.jsonl
2. Run quality evaluator for automated scoring
3. Integrate Anthropic API for LLM judge (semantic scoring)
4. Collect human ratings (optional, post-Phase 2)
5. Generate evaluation summary

---

## Command Quick Reference

```bash
# Full benchmark
python3 runners/benchmark_runner.py

# Small sample (10 tasks, 1-2 minutes)
python3 runners/benchmark_runner.py --sample

# With Ray (distributed)
python3 runners/benchmark_runner.py --ray

# Monitor progress (separate terminal)
python3 -c "
from runners.progress_monitor import ProgressMonitor
import time
m = ProgressMonitor()
m.start()
while True:
    time.sleep(30)
    m.print_progress()
"

# Check results in real-time
tail -f results/benchmark_results.jsonl | head -20

# Count results
wc -l results/benchmark_results.jsonl
```

---

**Status**: ✅ Ready for Phase 2 Week 5 execution
**Estimated completion**: 2-4 hours (sequential) or 0.5-1 hour (Ray)
**Next milestone**: Phase 2 Week 6 quality evaluation and LLM judge integration

