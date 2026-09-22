# Ground Truth audit — 2026-09-22

**Declared conflict:** this subsystem and its tests were written by the author of
this document. The findings below come from an agent that never saw that work and
was briefed to attack it; the decisive ones were re-reproduced by hand.

---

## Can this produce a trustworthy automatic quality baseline without human review?

**No — and the reason has moved since the last audit.**

The previous answer was "the instrument underneath cannot record a failure."
That is now fixed. The new answer is worse in one way and better in another:

> **The rigorous half of the pipeline is disconnected from the half that produces
> labels. A candidate can pass eligibility, get a replay envelope, enter the pool,
> receive a proposed verifier, survive mutation validation, be approved by a human
> and reach ACTIVE — and still contribute nothing, ever.**

---

## The disconnection, precisely

| | accumulation pipeline | labelling pipeline |
|---|---|---|
| ID scheme | `gtc-<content-hash>` (`accumulate.py`) | `gt-<seq>` (`author_tasks.py`) |
| Source | live captured traffic | `extract_corpus.py`'s legacy corpus |
| Verifier | `propose` → `mutants` → `registry` | hand-authored, "irreducibly human" per its own docstring |
| Reaches `freeze.py`? | **no** | yes |

`run_matrix`'s only bridge is `--use-registry` → `reg.active_for(t.task_id)`.
Live check:

```
reg.active_for('gtc-a854745f…')  -> the ACTIVE record
reg.active_for('gt-0001')        -> None
```

Two further breaks behind that one:

* `--use-registry` reads only `proposal["verifier_snippet"]`, populated for
  schema and file-state strategies alone. The pytest and mutation-tested
  strategies — the ones meant for real code tasks — populate `proposed_files`,
  which `run_matrix` never reads.
* `freeze.py` freezes `author_tasks.py`'s output, which has no code path from the
  pool or the registry at all.

**Every label that exists today came from the older, fully manual path.**

---

## HARD vs SOFT

The split is **correctly enforced where the label is emitted, and now enforced one
layer out too**.

| | |
|---|---|
| `label.py` | never emits `cheapest_acceptable_model` for a task mixing subjective and deterministic methods. Sound |
| `discriminate.policy_score` | now filters on `verification_type` and fails closed on an unrecorded one. Previously pooled everything — harmless only by accident |
| `contract.py` | flags vague acceptance criteria `unclear` and blocks rather than guessing |
| Three-state PASS/FAIL/AMBIGUOUS | never silently collapsed; the label is withheld when an ambiguous cell sits below the cheapest pass |

**HARD ground truth available today:** deterministic assertions, schema checks,
sandbox execution, existing test suites.

**SOFT ground truth:** correctly refused rather than approximated.

---

## Correlated failure — the premise does not hold here

The brief assumed one model generates the interpretation, the verifier, the
mutants and the judgment, making agreement non-independent.

Measured, by grepping every model-invocation path across the subsystem: **exactly
one call site** — `run_matrix.call_model()` — and it invokes the model *under
test*. `eligibility`, `classify`, `propose` and `mutants` are deterministic regex
and templates. The mutation library is hand-authored and mutates the **target**,
never the test.

**This is a real independence property and the strongest thing in the subsystem.**

The honest counterpart: `propose.py` calls itself "the verifier authoring
assistant". There is no assistant. It is a template engine, and the docstring
oversells it.

---

## What mutation validation cannot do — demonstrated

An agent constructed a verifier that is demonstrably discriminating and checks
entirely the wrong thing:

```
verifier:     assert len(BENCH_ANSWER) > 5
good answer:  "The capital of Portugal is Lisbon."
bad answers:  ["no", "na", "x"]
result:       baseline_passed=True, detected 3/3, weak_probe_set=None -> HIGH
then ACTIVE:  run_verifier(..., "I am extremely confident the capital is
              Madrid, definitely.") -> accepted=True
```

A wrong answer passes because it is long enough. **Mutation validation provides
zero protection against a semantically wrong but discriminating verifier.** The
design says so explicitly — "only a human catches that" — so this confirms a
documented limit rather than exposing a hidden one.

The gate enforcing that human is `require_human_actor()`, which is documented as
a plausibility heuristic and not authentication: any actor whose chosen name
avoids ~26 substrings passes.

---

## Replay — still absent, and now known to be worse than absent

Zero `git checkout` / `git apply` / worktree call sites. The H-08 gate correctly
refuses repo-bound tasks because of it.

But the gate only covers repo state. Two gaps behind it:

1. **`dataset.Task` carries no commit, diff or external field at all.** Whatever
   `freeze.py` writes carries no state.
2. **`run_matrix.py:203` calls `run_verifier(...)` with no `cwd`.** The verifier
   subprocess inherits the caller's working directory, and the preamble's
   `read()`, `run()` and `pytest_passes()` all operate on `os.getcwd()` — i.e.
   **today's checkout, never the captured commit.**

There is no equivalent gate for external evidence: a FACTUAL task can be admitted
once evidence is frozen, and nothing ever feeds that frozen content back into
grading.

**A hash is recorded. It is never dereferenced.**

---

## Defects found in this subsystem

| ID | Finding |
|---|---|
| **T-06** | The verifier pipeline is orphaned — ID namespaces never intersect (CRITICAL) |
| **T-13** | `replay_available()` resolves through a package attribute, so any code that sets `groundtruth.run_matrix` flips the gate permanently for the process — no error, no log, no counter |
| **T-01** | The test written to guard T-13's gate is itself the contaminator: its cleanup misses the package attribute, masking 8 real failures in a sibling file |
| **T-12** | `Pool.admit` has a second unlocked race on first-arrival admission; the H-07 fix locked only the duplicate-increment branch. 20 threads → 2 canonical rows where 1 was correct |
| **T-17** | `propose.select_strategy` has no branch for the FACTUAL/checkable-question shape that `eligibility` is proudest of admitting; it falls through to `no_reliable_verifier` |
| **F9** | `prompt_capture` fires only on the successful non-cache-hit terminal, so the corpus is survivorship-biased and structurally cannot contain "the router sent this somewhere that failed" |

---

## What is genuinely sound

* Three-state outcomes with AMBIGUOUS never collapsing, and label withholding.
* Eligibility defaults ineligible and must be earned; three-pass assess → capture
  → re-assess overrides on captured truth.
* Mutation validation mutates the target with an independent fixed library.
* `scrub.py` refuses to fall back to a weaker local copy — and verifiably does.
* `prompt_capture` fails closed end to end: a scrubber import failure means no
  record, not an unscrubbed one.
* No generative step anywhere in the pipeline.

---

## Verdict

Zero-human operation is not merely unsafe today; it is **unreachable by this
architecture as built**, and that is partly by design and partly by defect.

By design: the code states that a verifier can be demonstrably discriminating and
still check the wrong thing, and that only a human catches that. Honest, and
demonstrated above.

By defect: even the human-gated path produces nothing, because it does not
connect to the labelling path (T-06).

**The first useful work is not more pipeline. It is a bridge and a replayer** —
a shared ID scheme plus a converter from an ACTIVE `VerifierRecord` into a
`dataset.Task`, and a `cwd` that points at a checkout of the captured commit.
Until both exist, this subsystem can at best make a human-reviewed baseline
cheaper to produce. Today it does not deliver even that.
