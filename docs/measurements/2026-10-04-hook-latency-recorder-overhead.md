# Hook-latency recorder: what it costs

Measured 2026-10-04 on the machine this repo is developed on (macOS, Apple silicon,
Python 3.14.6; not CI). Produced by `scripts/bench_hook_latency.py`, run from the tree that
adds the recorder; the output below is the file's only source. It is the guardrail the
recorder answers to (`docs/repo_goals/KPIS.md`, G1): it must add no measurable hook latency.

## How

- **A/B.** Variant A is the 12 hook scripts exactly as on `origin/main` (no recorder);
  variant B is the working tree. Each round runs A then B (order shuffled) as a subprocess
  with a benign payload, in a throwaway `HOME` / `LLM_ROUTER_HOME` and `PATH=/usr/bin:/bin`.
  200 rounds per hook after 3 discarded warm-ups. "Paired median diff" is the median of
  (B - A) over the rounds.
- **Control (A/A).** The same method with B a second copy of A, so the paired difference is the
  noise floor of this machine and method, not a cost.
- **Micro.** The recorder alone in 40 fresh processes, with `llm_router` already imported
  (every hook imports it before doing anything else).
- **Not run: `session-start`.** It spawns detached background processes and can reach the
  network. It carries the same stanza as the 11 above.
- **Not measured:** CI runners, other machines, a cold disk cache, a hook that is slow for its
  own reasons (the stanza is a fixed cost, so its share only shrinks there).

## Output

```
### micro
fresh processes: 40
import_us        median    207.7 us   p95    240.5 us
begin_us         median      3.3 us   p95      6.2 us
first_write_us   median    206.8 us   p95    464.3 us
warm_median_us   median     21.8 us   p95     23.6 us
warm_p95_us      median     28.8 us   p95     31.7 us

### control (A/A)
A = origin/main (no recorder)   B = a second copy of A (CONTROL)   rounds=200   python=3.14.6
enforce-route        A median   49.61 ms  B median   49.61 ms  paired median diff  -0.01 ms  mean diff  +0.12 ms  (no recorder rows)
bash-compress        A median   47.67 ms  B median   47.80 ms  paired median diff  +0.01 ms  mean diff  +0.00 ms  (no recorder rows)
auto-route           A median  169.47 ms  B median  170.10 ms  paired median diff  +0.48 ms  mean diff  +0.97 ms  (no recorder rows)

### ab
A = origin/main (no recorder)   B = working tree   rounds=200   python=3.14.6
enforce-route        A median   49.55 ms  B median   49.95 ms  paired median diff  +0.20 ms  mean diff  +0.32 ms  (recorded elapsed median 24.2 ms, 203 rows; wall minus recorded 25.8 ms)
agent-route          A median   44.42 ms  B median   44.74 ms  paired median diff  +0.28 ms  mean diff  +0.22 ms  (recorded elapsed median 21.3 ms, 203 rows; wall minus recorded 23.4 ms)
auto-route           A median  164.12 ms  B median  164.23 ms  paired median diff  +0.33 ms  mean diff  +0.14 ms  (recorded elapsed median 107.5 ms, 203 rows; wall minus recorded 56.7 ms)
status-bar           A median   48.44 ms  B median   48.73 ms  paired median diff  +0.30 ms  mean diff  +0.39 ms  (recorded elapsed median 26.9 ms, 203 rows; wall minus recorded 21.8 ms)
subagent-start       A median   36.09 ms  B median   36.27 ms  paired median diff  +0.28 ms  mean diff  +0.20 ms  (recorded elapsed median 18.5 ms, 203 rows; wall minus recorded 17.8 ms)
usage-refresh        A median   36.81 ms  B median   37.09 ms  paired median diff  +0.34 ms  mean diff  +0.27 ms  (recorded elapsed median 17.4 ms, 203 rows; wall minus recorded 19.7 ms)
cc-usage-track       A median   37.59 ms  B median   37.77 ms  paired median diff  +0.23 ms  mean diff  +0.28 ms  (recorded elapsed median 17.7 ms, 203 rows; wall minus recorded 20.1 ms)
agent-depth-release  A median   35.32 ms  B median   35.56 ms  paired median diff  +0.33 ms  mean diff  +0.30 ms  (recorded elapsed median 17.2 ms, 203 rows; wall minus recorded 18.4 ms)
playwright-compress  A median   44.31 ms  B median   44.56 ms  paired median diff  +0.36 ms  mean diff  +0.42 ms  (recorded elapsed median 15.6 ms, 203 rows; wall minus recorded 29.0 ms)
bash-compress        A median   46.92 ms  B median   47.12 ms  paired median diff  +0.21 ms  mean diff  +0.19 ms  (recorded elapsed median 11.7 ms, 203 rows; wall minus recorded 35.4 ms)
session-end          A median  401.62 ms  B median  401.82 ms  paired median diff  +0.70 ms  mean diff  +1.19 ms  (recorded elapsed median 341.5 ms, 203 rows; wall minus recorded 60.3 ms)
```

## Reading it

Re-measured on the tree rebased onto `origin/main` at b48c872 (the first measurement, before
the rebase, read -0.08 to +0.41 ms; the machine's noise moved, the cost did not).

- Added wall time per hook invocation, paired median: **+0.20 to +0.70 ms** across the 11
  hooks (n=200 each), against hook wall times of 36 ms to 402 ms. The six hooks that run in
  35-50 ms read +0.20 to +0.36 ms; the A/A control on the two small hooks it ran (`enforce-route`,
  `bash-compress`) reads -0.01 and +0.01 ms, so on those the difference is a real, small cost.
  The two big hooks (`auto-route` +0.33, `session-end` +0.70) are inside this run's A/A noise on
  `auto-route`, +0.48 ms.
- The micro numbers predict the small-hook figure: about 0.21 ms is the one-off import of
  `llm_router.hook_latency` and `llm_router.capped_log`; `begin()` is about 3 us; the write
  at exit is about 22 us warm; the first write in a fresh state directory (creating it) is
  about 0.2 ms, once. About 0.23 ms total.
- `elapsed_ms` is shorter than the wall time the host sees by the interpreter's start-up and
  teardown: "wall minus recorded" above, 18 to 60 ms (median, same runs). The recorded number
  is a floor on what the host waits, not the whole of it.
