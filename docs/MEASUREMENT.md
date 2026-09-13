# Measuring anything in this repo

These rules are duplicated into the local `CLAUDE.md` (which this repo
deliberately does not commit — it is machine-specific). They live here as well
so a clone inherits them, because every one of them was paid for.

Each rule below is traceable to a specific wrong answer produced on 2026-09-13
during an investigation into "routing stopped working". The investigation was
correct in the end; it was confidently wrong twice on the way, and published
both wrong answers.

These rules exist because on 2026-09-13 an investigation into "routing stopped
working" produced **two confidently wrong answers** before the right one. Both
were denominator errors, not reasoning errors. Read this before quoting a rate.

### Never quote a rate from `auto-route-debug.log` without excluding test runs

Test-suite invocations write to the production log with `session_id=unknown`.
On 2026-08-31 that was **1,037 of 1,938 entries — 54% of the file.** A rate
computed over the raw log is wrong by roughly a factor of two, and it is wrong
in a direction that looks like a regression.

```
real prompts = lines with prompt_len= AND a session_id that is present and != unknown
```

Use `scripts/routing_rate.py`. Do not write another ad-hoc parser; two of them
disagreed today.

### A rate without its denominator is not a measurement

Days with 21-64 prompts produced rates of 2.5%, 1.6%, 0% and 4.3%. All four were
noise, and all four were briefly reported as a collapse. **Always print n.**
Below ~50 real prompts, say "too few to tell" instead of a number.

### The routing rate is a property of the WORKLOAD, not of the router

Measured: release/CI days route at ~62%, ordinary code days at 78-96%. The
`_is_context_dependent` gate fired 1.5% on 31 Aug and 32.2% on 1 Sep — a 21x
jump with **no commit touching that code** — and recovered to 4.4% by 10 Sep
with no fix applied.

Before concluding "the router regressed", check what the user was doing. A day
of `fix(ci)` prompts is full of file references, which is exactly what the gate
is built to catch. **Code regressions do not heal themselves; workload shifts
do.**

### An unlogged branch will cost a day

`ENFORCE=off` and `shadow` skipped the DIRECT block while logging nothing, and
that branch accounted for 28.5% of one day's prompts. It was invisible in every
report until the hook was run under all four modes side by side.

**Invariant:** every invocation logging `prompt_len=` must log exactly one
terminal outcome — `DIRECT:`, `DIRECT SKIP: <reason>`, `BYPASS`, or
`CONTINUATION`. If you add a branch that can skip routing, it logs a reason.
There is a test for this; do not delete it to make a change pass.

### Measure interception by BYTES, not by command count

Intercepting half the commands that produce 2% of the output is worth nothing.
Coverage claims in this repo are `bytes of tool output avoided / total bytes`.

Related: eligibility is not interception. The compressor rejects output it
cannot shrink, so of 26 commands that pass the allowlist only 9 actually
intercept. Quote the number after the compression gate, and say which one it is.

### Before claiming a saving, measure the thing that pays

"A local model ran" is not a saving. The measured 2026-09-13 result: handing
Claude a local candidate to review cost **1.86x more Claude tokens** than Claude
doing the task itself (5 of 5 tasks, turns 33 -> 80). Verification is not
cheaper than authorship.

A saving claim needs: Claude tokens before, Claude tokens after, same task,
scored for correctness both ways.

### Wall-clock timings on this machine are not trustworthy

macOS Maintenance Sleep advances `time.time()` and not `time.monotonic()`. One
benchmark task was recorded at 918.6s of which **902s was the laptop asleep**.
Use `time.monotonic()` for durations and `caffeinate -i` for unattended runs.
