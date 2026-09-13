# Does routing to a local model cost quality?

Measured 2026-09-12 with `scripts/bench_backend_quality.py`.

Cost was already proven. Quality was assumed — and the routing ledger could not
settle it: of 17,195 rows in `~/.llm-router/routing_quality.jsonl`, 16,869 carry
`verification_attempted=false`, and the most common `final_model` values are
`test/mock-model` and `ollama/badmodel`. It is mostly test-suite output. The
ledger records that a route happened, not whether the work was any good.

## Method

Two suites of mechanically-scored tasks, each run against a freshly rebuilt
throwaway project, once per backend.

* **easy** (25 tasks) — single-file, single-hop: answer a question about the
  project, or make a contained edit.
* **hard** (10 tasks) — the cause sits in a different file from the symptom, or
  the correct edit depends on a caller the model has to go and find.
* **brutal** (11 tasks, `scripts/bench_brutal_suite.py`) — the obvious fix is
  wrong, and the reason is in the repo but not in the prompt.

No model judges another model. Every task is scored by a verifier that either
greps the resulting files or **imports the edited code and asserts on its
behaviour**, so a backend cannot pass by echoing the prompt back.

Two things are scored, not one:

| metric | meaning |
|---|---|
| `correct` | the verifier passed — the work was done |
| `clean` | no file outside the task's declared blast radius changed |

Q&A tasks declare an empty blast radius: editing anything to answer a question
is collateral damage.

Before the run, every task was checked both ways — the pristine project must
FAIL it (no task is free) and a hand-written reference solution must PASS it (no
task is impossible). All 35 passed that screen.

## Results

### easy — no signal

| backend | model | correct | clean | median |
|---|---|---|---|---|
| local | qwen3-coder:30b | 24/25 | 25/25 | 3s |
| codex | gpt-5.5 | 24/25 | 22/25 | 24s |
| claude | sonnet | 24/25 | 25/25 | 12s |

A three-way tie. For contained work the local model is indistinguishable from
both cloud agents at 4-8x the speed and zero marginal cost.

### hard — the gap appears

| backend | model | correct | qa | edit | clean | median |
|---|---|---|---|---|---|---|
| local | qwen3-coder:30b | **6/10** | **0/3** | 6/7 | 9/10 | 22s |
| codex | gpt-5.5 | 10/10 | 3/3 | 7/7 | **4/10** | 43s |
| claude | sonnet | 10/10 | 3/3 | 7/7 | 10/10 | 19s |

### brutal — built to reject the obvious fix

The hard suite separates local from the cloud agents but leaves Codex and
Claude tied at 10/10. The brutal suite works on a different principle: the
obvious implementation is wrong, and what makes it wrong is discoverable **in
the repository** but never stated in the prompt — a class docstring declaring an
invariant, an existing passing test pinning a return value, a local variable
whose name contains the string you were told to rename.

Every task was screened three ways before the run: the pristine project must
fail it, a **naive** fix must also fail it, and a careful fix must pass. Eight
of the nine edit tasks reject a plausible naive implementation. A task whose
obvious fix passes separates nothing, so it does not belong here.

| backend | model | correct | qa | edit | clean | median |
|---|---|---|---|---|---|---|
| local | qwen3-coder:30b | 8/11 | 1/2 | 7/9 | 11/11 | 21s |
| codex | gpt-5.5 | 10/11 | 2/2 | 8/9 | **3/11** | 50s |
| claude | sonnet | 10/11 | 2/2 | 8/9 | 10/11 | 18s |

**Nobody scored full marks** — the ceiling is gone. What each backend did:

* `br-collect-errors` **caught all three cloud-class attempts and neither local
  one**. The prompt says "report every problem it finds, as a list"; the obvious
  implementation returns `[]` for a valid row, which breaks
  `tests/test_validate.py::test_valid_row_returns_none`, a test that was already
  passing. Codex and Claude both failed it and both left `tests/test_validate.py`
  modified in the recorded run. A re-run of Claude took a different path — it
  left the test alone and simply broke it (`1 failed`). Local passed. This is the
  only task where the local model beat both cloud agents, and it is one sample,
  so read it as "the trap is real", not as "local is more careful".
* `br-money-total` — local fixed the visible symptom but kept summing through
  `float`, which the module docstring forbids in its first line.
* `br-dedupe-order` — local reached for `dict.fromkeys`, the exact naive fix the
  screen predicts: order is preserved, and it raises `TypeError: unhashable
  type: 'dict'` on the input the prompt warned about.
* `br-trace-today` — local answered `['a $1.50', 'b $1.50', 'c $0.07']`: the
  order the code is *supposed* to produce rather than the order it produces
  today, plus a `$` that `format_cents` never emits.
* Codex's 3/11 cleanliness is again almost entirely unrequested test files — it
  wrote one for nearly every module it touched.

The separation this suite produces is not a single ranking. On **correctness**
local sits clearly below both cloud agents (8 vs 10). Codex and Claude are level
on correctness and separate on **discipline**: 3/11 clean versus 10/11, at 2.8x
the wall-clock.


## What actually failed

**The local model's failures are investigation failures, not editing failures.**
It scored 6/7 on hard EDITS and 0/3 on hard QUESTIONS — the reverse of the
intuition that reading is the easy half. Every hard question requires following
a thread across files before answering, and that is where it broke:

* `hd-name-the-bug` and `hd-swallows` — both returned *"Agent reached maximum
  iterations. Partial work may have been done."* The loop ran out of turns while
  still looking. On the first it also edited `src/pipeline.py` and
  `src/store.py` while answering a question that asked for no edits at all.
* `hd-paginate-count` — answered `7`, the number you get by assuming
  `paginate()` is correct, rather than reading
  `range(0, len(items) - per_page, per_page)` and seeing that it drops the last
  partial page. The answer to the code as written is `6`.
* `qa-report-output-v2` — answered `[20, 60]` for `report([10, 60])`. It applied
  `scale(v, 2)` but did not follow `scale` into `clamp`, so it missed that
  `120` clamps to `100`. Correct answer `[20, 100]`.
* `hd-mutable-default` — diagnosed the bug correctly in prose ("default
  arguments are evaluated only once when the function is defined") **and then
  did not change the code**. A perfect explanation scores zero.

**Codex is accurate but expansive.** It got 10/10 on both halves, and was the
only backend to answer a question wrong on the easy suite (`qa-clamp-lower`:
said `clamp` enforces a lower bound; it does not). Its 4/10 cleanliness is
almost entirely *unrequested test files* — `tests/test_page.py`,
`tests/test_tags.py`, `tests/test_query.py` and others it wrote on its own
initiative. That is defensible engineering, not damage, but it is scope the
task did not ask for, and on `hd-strict-validate` it also edited a second source
file (`src/schema.py`). Budget for a bigger diff than you asked for.

**Claude was clean everywhere** — 10/10 correct and 10/10 clean on hard, with a
median of 19s, faster than Codex.

## Does owning the whole task make the local model better? No.

`llm_local_task` (shipped 2026-09-13) hands a whole objective to the local model
instead of a prompt: it reads, edits and runs commands locally, and returns one
typed result. The architectural win is real — one Claude turn instead of one per
tool call. The question here is whether it also makes the WORK better.

Four local configurations, same 11 brutal tasks:

| run | correct | qa | edit | clean |
|---|---|---|---|---|
| raw loop, broken tools | 8/11 | 1/2 | 7/9 | 11/11 |
| raw loop, tools fixed | 8/11 | 1/2 | 7/9 | 10/11 |
| `llm_local_task`, no acceptance check | 8/11 | 1/2 | 7/9 | 10/11 |
| `llm_local_task` + "the suite must still pass" | 8/11 | 1/2 | 7/9 | 10/11 |
| codex gpt-5.5 | **10/11** | 2/2 | 8/9 | 3/11 |
| claude sonnet | **10/11** | 2/2 | 8/9 | 10/11 |

**Identical. Same score, same three failures, every time.** A 5x larger budget
changes nothing. An acceptance check changes nothing. Fixing two broken tools
changed nothing. `br-money-total`, `br-dedupe-order` and `br-trace-today` fail in
all four, for the same reasons each time — the model keeps `float` against an
explicit docstring, reaches for `dict.fromkeys` on input the prompt says contains
dicts, and reports the order the code is *supposed* to produce rather than the
order it does.

Three failures repeating across five independent runs is not variance. It is a
ceiling, and it is a property of the model, not of its harness.

The local model also beat both cloud agents on `br-collect-errors` in all four
runs — the hidden-test trap, where returning `[]` for a valid row breaks a test
that was already passing. That is now four samples, not one.

**Read this as a scoping rule, not a disappointment.** Give the service work
whose failure you would catch, and expect the architecture to save turns rather
than raise quality. Nothing measured here suggests local gets better with more
rope.

### Two tools were broken for every earlier measurement

`agent_loop.execute_tool` reported results relative to the caller's UNRESOLVED
project root while `_resolve_path` validated against the resolved one. `/tmp` is
a symlink to `/private/tmp` on macOS, so in every sandbox this suite creates,
`list_files` and `search_files` returned "is not in the subpath of" for
directories the model was entitled to read. It could not tell a broken tool from
a wrong approach, so it retried until its iterations ran out.

Every local score recorded before 2026-09-13 was measured that way. Re-running
with the tools fixed produced **the same 8/11** — so the finding stands, but it
stood for the wrong reason until it was checked.

### The wall-clock numbers in this document are not trustworthy

A traced run recorded `br-retry-contract PASS 918.6s`. The execution trace
disagreed with itself: 909s of wall clock against 12.7s of monotonic time.
`pmset -g log` settled it — macOS entered "Maintenance Sleep" for 902s in the
middle of the task. Three stalls in that run map one-for-one onto sleep windows.

The harness now measures `time.monotonic()`. Durations recorded before that
change include however long the laptop was asleep, so treat every second in this
document as an upper bound and run unattended benchmarks under `caffeinate -i`.
Correctness figures are unaffected — a sleeping Mac does not change an answer.


## Two findings that are not about model quality

1. **The local agent loop cannot edit files in its default configuration.**
   `LLM_ROUTER_AGENT_WRITES` defaults to `propose`, so the loop computes a diff
   and writes nothing; the model reports *"the edit wasn't applied due to system
   restrictions."* Routing an edit task to local in a default install produces a
   proposal, not a change, no matter how capable the model is. The benchmark
   sets `apply` in its disposable sandbox so the comparison measures quality
   rather than permissions.

2. **A provider that refuses to answer is not a provider that answered badly.**
   The first Claude run scored 1/25 because the CLI had hit a subscription spend
   limit and returned the same quota message 25 times. The second scored low on
   command-requiring tasks because `--permission-mode acceptEdits` allows file
   edits but not shell commands, so it could not run `pytest` and asked for
   approval that a non-interactive run can never give. Both read as a quality
   collapse and neither was one. The harness now detects quota, auth and
   approval refusals and aborts the run instead of scoring them.

## How to read this for routing policy

Local is not worse at *doing* the work. It is worse at *finding* the work.
Routing on task difficulty will mislead here — "rename this function" is a safe
local route and "which method causes this test to fail" is not, even though the
second sounds lighter. The signal to route on is how many files the answer has
to cross, and the escalation trigger the loop already has —
`Agent reached maximum iterations` — is a reliable one: it fired on two of the
four local failures and should escalate rather than return.

## Caveats

* One pass per cell. Single failures are not separable from flakiness; the local
  loop in particular is stochastic.
* 46 tasks on three synthetic projects. Real repos are larger and messier, which
  should widen the investigation gap, not narrow it.
* The cloud CLIs were given write and test-run permission to match the local
  loop's `apply` mode. That is the correct comparison for quality and the wrong
  one for safety.
* `qa-report-output` on the easy suite is a badly worded question — it never
  says the function is in this project, and Claude reasonably answered that no
  such function was in context. It is kept verbatim as run;
  `qa-report-output-v2` is the anchored rewrite, and local fails that too.

## Reproduce

```bash
python3 scripts/bench_backend_quality.py --backend local  --suite brutal
python3 scripts/bench_backend_quality.py --backend local  --suite hard
python3 scripts/bench_backend_quality.py --backend codex  --suite hard
python3 scripts/bench_backend_quality.py --backend claude --suite hard
BENCH_REPORT_SUITE=brutal python3 scripts/bench_backend_quality.py --report
```
