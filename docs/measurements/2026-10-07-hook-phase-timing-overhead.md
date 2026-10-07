# Hook phase timing: what it costs

Measured 2026-10-07 on the machine this repo is developed on (macOS, Apple silicon,
Python 3.13.14; not CI), while other jobs were running (load average 6-12 at the time, so
the noise floor below is wider than in the 2026-10-04 recorder measurement). Produced by
`scripts/bench_hook_latency.py` from the tree that adds `phases_ms` (hook versions:
auto-route 45, session-start 23). The output below is the only source of the numbers.
The budget for this change (PLAN M4.1) is 2 ms or less added per hook invocation.

## How

- **A/B.** A is `src/llm_router/hooks` exactly as on `origin/main` (recorder, no phases); B is the
  working tree. Each round runs A and B in shuffled order as a subprocess with a benign
  `auto-route` payload, in a throwaway `HOME` / `LLM_ROUTER_HOME`, `PATH=/usr/bin:/bin`.
  300 rounds, 3 discarded warm-ups. "Paired median diff" is the median of (B - A).
  Both variants import the same `llm_router.hook_latency` (the editable install), so the
  difference is the phase calls in the hook file plus the larger row.
- **Control (A/A).** B is a second copy of A: the paired difference is the noise floor, not a cost.
- **Micro.** 40 fresh processes: one `with phase(...)` while a hook is being timed, and the exit
  write of a row that carries 7 phases against the old row.
- **Not run: `session-start` in A/B** (it spawns detached processes and can reach the network).
  It makes up to 11 phase calls per run at the micro cost below. A separate isolated functional
  run (empty `HOME`, `PATH` without `security` or `ollama`, warm-up off, Ollama URL on a dead port)
  confirmed every session-start phase is written; its numbers are not production data.

## Result

- One `with phase(...)`: **0.3 us** median and p95 (micro). `auto-route` makes up to 14 phase calls
  on its longest path, `session-start` up to 16: under 5 us per run.
- The exit write with 7 phases: **26.1 us** against **23.5 us** for the old row: +2.6 us.
- A/B `auto-route`: paired median diff **-0.88 ms** (control A/A **+0.40 ms**). Both lie inside the
  noise of a loaded machine; neither is distinguishable from 0. The direct cost (about 8 us)
  is the figure that bounds the change; 2 ms is not approached.
- Only `auto-route` and `session-start` rows grow: from 106 bytes to 232 (8 phases) or 287 (11
  phases). Those two hooks wrote 393 of 31,271 rows (1.3%) in the live file on 2026-10-07, so the
  4 MiB cap shortens the window `kpi` sees by about 2% (53 KB more over the 3.27 MB copy of the live file).
- The bench prints "A = origin/main (no recorder)" for the baseline label; on this base
  `origin/main` already has the recorder, so A is "recorder, no phases".

## Output

```
### micro
fresh processes: 40
import_us        median    210.3 us   p95    240.7 us
begin_us         median      3.0 us   p95      3.4 us
first_write_us   median    191.7 us   p95    252.6 us
warm_median_us   median     23.5 us   p95     25.1 us
warm_p95_us      median     30.1 us   p95     42.3 us
phase_ctx_median_us median      0.3 us   p95      0.3 us
phase_ctx_p95_us median      0.3 us   p95      0.3 us
warm_with_7_phases_median_us median     26.1 us   p95     28.0 us

### control (A/A)
A = origin/main (no recorder)   B = a second copy of A (CONTROL)   rounds=300   python=3.13.14
auto-route           A median  195.25 ms  B median  194.41 ms  paired median diff  +0.40 ms  mean diff  +6.46 ms  (recorded elapsed median 129.9 ms, 303 rows; wall minus recorded 64.5 ms)

### ab
A = origin/main (no recorder)   B = working tree   rounds=300   python=3.13.14
auto-route           A median  246.46 ms  B median  247.50 ms  paired median diff  -0.88 ms  mean diff  -1.32 ms  (recorded elapsed median 164.3 ms, 303 rows; wall minus recorded 83.2 ms)
```
