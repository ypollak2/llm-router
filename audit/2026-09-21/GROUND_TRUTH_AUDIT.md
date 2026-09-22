# Ground Truth system audit

v14.1.0 · 2026-09-21. **Written by the author of this subsystem, using an audit
briefed to be harder on it than on a stranger's code.** Treat that as a declared
conflict of interest rather than a resolved one.

Empirical status: the production candidate pool is **empty**. Accumulation was
enabled during this session and no row exists yet. Every finding below is from
code reading and control-flow tracing unless marked otherwise.

---

## Can this produce a trustworthy automatic quality baseline without human review?

**Not today, and not for the reason I expected.**

The blocker is not the pipeline's sophistication — it is that the **instrument it
reads from cannot record a failure** (finding C-01). Ground Truth exists to
answer "was this routing decision good?" against a ledger where
`route_succeeded=False` has never appeared in 16,869 rows, where 97% of routes
are unverified, and where the field explaining *why* a model was chosen is 0%
populated.

Building more pipeline on top of that is premature.

---

## HARD vs SOFT ground truth

The split is **real where it matters and unguarded one layer out**.

| | |
|---|---|
| **Correctly enforced** | `label.py:81-84` detects any task mixing `SUBJECTIVE_METHODS` with `DETERMINISTIC_METHODS` and never emits `cheapest_acceptable_model` for a subjective task. `run_matrix` only labels `verifier_kind == MECHANICAL`. This is the one place the headline label is produced, and it is honest |
| **Unguarded** | `discriminate.py::policy_score` pools every cell's `accepted` boolean with **zero** verification-type filtering — it never imports `DETERMINISTIC_METHODS`. Harmless today only because `generate_snippet()` has no branch for judge/rubric strategies, so those always yield `None` and get skipped. **That is an accidental barrier, not a designed one** — extending `generate_snippet()` for judges (a natural next step) would silently pool judge verdicts with mechanical ones |

**HARD ground truth available today:** deterministic assertions, schema
validation, sandbox execution, existing test suites — all real, all supported by
`verifiers.py` and validated by `mutants.py`.

**SOFT ground truth:** correctly refused. `contract.py` flags vague acceptance
criteria as `unclear` and blocks the contract from being actionable rather than
guessing. `label.py` withholds a label when an ambiguous cell sits *below* the
cheapest pass — the conservative direction.

---

## Correlated failure — the premise does not apply

The audit brief assumed the same LLM generates the contract, the verifier, the
mutants and the judgment, making agreement non-independent.

**That is not this code.** Repo-wide grep: only `run_matrix.py` calls a model, and
that is the model *under test*. `contract.py` and `propose.py` are deterministic
regex and templates. The mutation library is 10 hand-authored bug shapes applied
identically to every candidate, and `mutants.py` mutates the **target**, never the
test. `generate_test()` emits `pytest.fail("UNIMPLEMENTED")` placeholders, so a
generated pytest verifier structurally cannot rate above UNUSABLE until a human
writes the assertion.

**This is a genuine independence property and the strongest thing in the
subsystem.**

The honest counterpart: my docstrings call it a *"verifier authoring assistant"*.
It is a template engine. That oversells what is implemented.

**The one real self-validation risk** is the snippet path: `bad_answers` for
`validate_snippet_verifier` are supplied ad hoc on the CLI. A rushed operator
picking a trivially-wrong bad answer gets `detected > 0` and reaches HIGH
confidence for a check that barely discriminates. The pytest path has a floor;
the snippet path does not.

---

## Wiring — what is real and what is not

| Transition | State |
|---|---|
| capture → scrub → eligibility → envelope → pool | **Wired.** `capture()` is called from `router.py:1989` inside `_finalize_successful_route`, reachable from 5 production sites. Verified end-to-end this session |
| envelope → **replay** | **Does not exist.** Zero `git checkout` / `git apply` / worktree call sites. `run_matrix.call_model()` sends the prompt as a single-turn completion and grades raw text |
| model matrix → outcome → label | Wired, but only reachable for QA-shaped tasks |
| verifier proposal → validation → lifecycle | Wired; mutation gate genuinely discriminates |

**The consequence of the replay gap:** the eligibility gate is tuned to admit
EDIT/code tasks — repo state, patches, test commands — and the harness cannot
grade them. A model with no file access cannot satisfy a pytest file importing
from a tree that was never checked out. The envelope captures replay state
correctly and nothing consumes it.

---

## Defects in my own code

| ID | Finding |
|---|---|
| **M-01** | `completeness()` — the function its own docstring calls "the load-bearing part" — returns `(True, [])` for an envelope whose patch was never stored, while `reconstructable` on the same object returns `False`. This is exactly the "hash treated as reconstructable state" failure the module was written to prevent. Masked today by a redundant correct check in `accumulate.py`: **correct by luck, not by design** |
| **M-02** | `detect_synthetic()` checks only two env signals. The sandbox-path and fixture-session-id detectors written for `sources.py` are not consulted, and **no `bench_*.py` sets `LLM_ROUTER_SYNTHETIC`**. With accumulation enabled, a benchmark run would capture fixture prompts as production — reopening the documented contamination class through the mechanism added to close it |
| **H-07** | `Pool.admit()` loses increments under concurrency — measured 19 where 21 was expected. `accumulate.py` constructs a fresh `Pool()` per call, so the production path hits it |
| **H-10** | The human-approval gate is `actor == "assistant"`. Any other string passes |
| **F-03** | `scrub_safe=False` → `R_PRIVACY` is **never invoked by any caller**. The docstring's "Privacy wins over evaluation, always" is aspirational; `residual_risk()` flags downgrade priority but never block admission |

---

## What is genuinely sound

- Three-state PASS/FAIL/AMBIGUOUS, with AMBIGUOUS never silently collapsing to
  either — and label withholding when an ambiguous cell sits below the cheapest
  pass. Both are real safeguards against biasing labels toward "the router looks
  good".
- Eligibility defaults ineligible and must be earned; the three-pass
  assess → capture → re-assess genuinely overrides on captured-state truth.
- Mutation validation mutates the target with an independent fixed library.
- The frozen-dataset manifest is unusually honest about its own limits (census
  not sample, timestamp gaps, denylist-only PII coverage).
- `is_evaluable()` fails closed on rows predating the provenance field.

---

## What would have to change for zero-human operation

| | |
|---|---|
| **Already mechanical** | Mutation kill-rate evidence. No human judgment needed |
| **Genuinely blocked** | Whether the *contract* captures what the task actually asked. `contract.py` checks internal consistency only. The module's own design says a verifier can be demonstrably discriminating and still check the wrong thing — and only a human catches that |
| **Merely unimplemented** | Real identity on the approval gate. Could be hardened without changing the philosophy |

**Verdict:** zero-human operation is unsafe now, for the reason the design itself
states — and separately, the human-in-the-loop requirement is enforced more
weakly than documented.

---

## Recommendation

**Stop extending this subsystem** until C-01 lands. Then the first useful work is
not more pipeline — it is either implementing replay, or narrowing the
eligibility gate to admit only tasks the harness can actually grade. Shipping a
gate that admits work the runner cannot execute is how the pool fills with
candidates that will never be labelled.
