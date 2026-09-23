# 28 — 45 drafts, 773 seconds, zero used

Measured 2026-09-23 on this machine, from one working day of real use. Not an
audit of the code — a measurement of what the product did.

---

## The numbers

Source: `~/.llm-router/auto-route-debug.log`, today's lines only;
`scripts/routing_rate.py` for the rate (the canonical parser — CLAUDE.md
forbids writing another).

| | today |
|---|---|
| Real prompts (`prompt_len=`) | **82** |
| `DIRECT SUCCESS` — the hook ran a local model and produced a draft | **45** (56.2%) |
| Drafts the assistant actually used | **0** |
| Local model time spent on them | **773 s** — median **18.7 s**, max **33.0 s** |
| Model | `ollama/qwen3.5:latest` ×44, `qwen3.8:latest` ×1 |
| Calls the assistant made itself via MCP `llm()` | **4** — and 3 of those existed only to clear an enforcement lock |
| `routing_decisions` rows today | 4 |

So the routing rate is real: 56.2% of prompts were offloaded to a local model.
**The work product of that offload was discarded 45 times out of 45.**

Per-prompt cost to the user: **~18.7 s of added latency before the assistant
began**, on the median prompt.

Claude tokens saved: **zero**. The assistant did the work anyway.

---

## Why the drafts were unusable

Every one described the user's repository, and the drafting model cannot see it.
They named tests, files and "Phase 52/53/54" work items that do not exist. A
representative sample, from prompts about this repo's own state, proposed
running `pytest tests/test_bench_grounding_scoring.py::test_durations_use_a_monotonic_clock`
— a test the drafting model invented.

This is the §10 capability gap in `architecture/GAP_ANALYSIS.md`, observed at
scale: **there is no capability filter on the live path, only an ordering.** A
prompt requiring repository access is offered a stateless endpoint, because
nothing checks whether the chosen door can serve it.

It is also the project's own rule, reproduced at n=45. CLAUDE.md:

> "A local model ran" is not a saving. The measured 2026-09-13 result: handing
> Claude a local candidate to review cost **1.86x more Claude tokens** than
> Claude doing the task itself.

---

## The gate already exists. The rescues defeat it.

The obvious fix — "skip drafting when the prompt is context-dependent" — is
**already implemented**, at `hooks/auto-route.py:3907`:

```python
if _direct_enabled and not zero_claude and (
    _is_context_dependent(prompt) or _inherits_context
):
    ... OKF rescue ... session rescue ... tool-loop rescue ...
    else:
        _direct_enabled = False
        _debug_log("DIRECT SKIP: context-dependent prompt")
```

It fired **3 times** today. Attributing today's 45 drafts to the branch that
produced them:

| Path | drafts |
|---|---|
| **No rescue — not detected as context-dependent at all** | **22** |
| OKF rescue fired ("routing WITH context") | 16 |
| Session rescue fired ("the conversation resolves it") | 7 |

Two distinct defects, and they need different fixes:

### D-1 · 22 repo-context prompts were not detected as context-dependent

The detector missed half of them outright. `_is_context_dependent` is the same
predicate S3b improved this week (false positives 5/12 → 0/12) — that work
reduced **over**-detection. This is the other direction, and it is unmeasured:
nobody has counted its false *negatives*.

The hook's own docstring at `:2811` admits it: *"this makes
`_is_context_dependent`'s ~60% false-negative…"* — a number written down and
never acted on.

### D-2 · The rescues fire and the draft is still useless — 23 times

OKF rescue retrieved documents and declared the prompt answerable; session
rescue decided the conversation resolved it. **Both then produced a draft that
was discarded.**

The rescues are measuring the wrong thing. They ask *"can I find material
related to this prompt?"* — and retrieving three OKF docs about the repo does
not make a stateless model able to answer "check if agenticgraphs accepts an
injected runner". The question is not whether context exists; it is whether
**enough** context to answer was assembled.

Nothing measures that. There is no check that the rescue's output was
sufficient, and no record of whether the resulting draft was used — which is
why 23 useless rescues look identical to 23 successful ones in every existing
surface.

---

## What would make this measurable

The missing signal is **draft acceptance**. Today nothing records whether an
injected draft was used, so the system cannot distinguish its best day from its
worst. That is the episode gap from `architecture/KNOWLEDGE_MODEL.md` in its
cheapest possible form:

    draft_offered → draft_used | draft_discarded

One boolean per invocation would turn `DIRECT SUCCESS` from *"a model
responded"* into *"a model helped"*, and would let the rescues be evaluated
instead of trusted. Without it, every improvement to this path is unfalsifiable.

---

## What this is NOT evidence for

Stated because the numbers are striking and easy to over-read.

- **Not evidence that local routing is worthless.** It is evidence that routing
  a *repo-context* prompt to a *stateless* door is worthless. Those are
  different claims, and nothing here measures the second.
- **Not a regression.** The rate (56.2%) is in the historical band and
  `routing_rate.py` shows 51.7% / 65.5% / 56.2% across the last three days.
  What is new is measuring the *value* of the offload rather than its rate.
- **n=1 day, one user, one workload** — an audit-heavy day on this repo, which
  is unusually context-bound. A day of general Q&A would look very different.
  The right follow-up is the same measurement over a week.

---

## Target

The user's stated expectation is **60–80% of prompts routed**, and the *rate*
already meets it. The gap is entirely in usefulness: 56.2% offloaded, 0% used.
A rate target is satisfiable by drafting more, which is precisely what happened
here. **The target should be stated on accepted drafts, not offered ones** —
and that cannot be measured until the acceptance signal above exists.

---

## Addendum — D-1 measured, and the "~60%" figure examined

`hooks/auto-route.py:2811` asserts a rate and cites nothing:

> "This makes `_is_context_dependent`'s **~60% false-negative rate** irrelevant
> to the fabrication risk."

Measured 2026-09-23 over n=1610 real prompts. A single global number turns out
to be the wrong shape — **the detector's failure is concentrated in one class**:

| Objectively context-dependent class | n | `_is_context_dependent` misses |
|---|---|---|
| Names a file path with a slash (`src/x/y.py`) | 114 | **0 = 0.0%** |
| Names any filename (`router.py`) | 222 | **0 = 0.0%** |
| **Short imperative continuation** (`go on more`, `yes, repoint both`) | 45 | **19 = 42.2%** |

The detector is excellent at what a regex is good at — a prompt naming a file is
caught every time — and fails on prompts that name nothing and mean nothing
without the previous turn. Those are the majority of a working session's traffic.

### What this does NOT show

`_is_context_dependent` is only half the gate:

```python
if _direct_enabled and not zero_claude and (
    _is_context_dependent(prompt) or _inherits_context
):
```

`_inherits_context` is `method in ("context-inherit", "code-context-inherit")` —
a *classifier* state, not a property of the prompt text, so a static probe
cannot see it. It demonstrably fires (today's banners show `via:
context-inherit` repeatedly). **The 42.2% therefore overstates the end-to-end
gap and must not be quoted as the gate's miss rate.**

The end-to-end number is the one already in this document, taken from the live
log rather than a probe: **22 of today's 45 drafts** had no rescue and were not
flagged — the OR missed them, start to finish.

### The figure was measured against the wrong harm

The docstring's point is that the false-negative rate is *"irrelevant to the
fabrication risk"*, and for fabrication that is correct: outside zero-Claude a
draft is advisory context, never a turn replacement.

But that is not the only harm. Every false negative produces a draft that is
generated, injected and discarded — **~18.7 s of added latency on the median
prompt and 773 s of local compute today, for zero token saving.** The rate was
assessed against fabrication, found harmless, and never assessed against cost,
which is where it lands.

That is the same shape as the rest of this document: a number that was computed,
recorded, and evaluated against the wrong question.

### Consequence for D-1

Do not "fix the detector" as a single number. Two separate questions:

1. **Continuations** — is the `_inherits_context` half already covering them? It
   is a classifier state and can only be measured live, by counting drafts
   produced for prompts matching the continuation shape. The `draft_acceptance`
   counter added alongside this document makes that measurable for the first
   time.
2. **Everything else** — the file-naming class needs no work at 0/222.

Until (1) has a number, widening the regex is tuning against examples.
