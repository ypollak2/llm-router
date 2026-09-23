# ROUTING_MODEL.md

How a single graph node becomes a model choice. The graph says *what*;
this says *by whom*. Keeping them separate is what stops a topology change from
silently becoming a cost change.

---

## 1. The pipeline

```
node → capability requirements → CAPABILITY FILTER → candidates
     → outcome estimate (only when n ≥ 30) → ECC → context budget → execute
```

Order is load-bearing. **Filtering precedes optimisation.** Optimising first and
filtering second is how a cheap model that cannot do the job gets chosen.

---

## 2. Capability filter (§10) — the missing gate

Today there is **no filter on the live path, only an ordering**. A vision task
and a text-only local model sit in the same chain with nothing between them.

Both halves already exist and nothing joins them:

| Half | Where | State |
|---|---|---|
| What the **task** needs | `capabilities.detect_capabilities()` | Built, gated off (`LLM_ROUTER_CAPABILITY_ROUTING`), only consumer is a dead path |
| What the **model** offers | `model_registry.ModelMetadata` — `context_window`, capability tuple | Built, **no reader outside the module and its tests** |

```
eligible(m, node) :=
      supports(m, node.capabilities)          # vision, tools, json, …
  AND m.context_window >= node.min_context    # measured, not guessed
  AND known(m)                                # ← the fail-closed clause
```

### The fail-closed clause is the whole point

```
known(m) = m has a capability record AND a context_window
```

A model whose capabilities are **unknown is not eligible**. This repo has
shipped six instances of unknown rendering as the favourable answer:

| Instance | Unknown became |
|---|---|
| provenance NULL | production data |
| table not in the allowlist | column missing |
| unreadable counter | zero |
| absent `avg_latency_ms` | fastest |
| unscored prompt | a confident route |
| unrecorded `classifier_confidence` | low confidence (213 false findings) |

Defaulting an unknown model to "capable" would be the seventh, and it is the
most expensive one because it is on the routing path.

**Anti-vacuity requirement:** before enabling the filter, measure in shadow mode
how often the live chain contains an ineligible model. If the answer is zero,
the filter is not needed and the registry data is the thing to fix. A filter
that never excludes anything is indistinguishable from a broken one.

---

## 3. Outcome estimation — mostly refusing to estimate

```
if n(model, node_kind, scope) < MIN_SAMPLES_FOR_SIGNAL:   # = 30, already exists
    use the static profile order        # NOT a guess, an abstention
```

This is the common branch, not the edge case. Measured: only **2 of the top-10
`(task_type, model)` cells** clear n=30 (`code/gpt-4o` 1057, `code/claude-opus`
330); the rest are 3–77. At **13 rows/week**, a new cell reaches 30 in years.

Maturity ladder — do not skip levels:

| Level | Method | Entry condition |
|---|---|---|
| **L0 (today)** | Static profile order | always |
| **L1 (MVP)** | Per-cell pass rate with a Wilson lower bound | n ≥ 30 in that cell |
| **L2 (V3)** | Beta-Bernoulli posterior per cell, shared prior across scopes | ≥ 200 episodes with a real verdict |
| **L3 (research)** | Contextual bandit / learned ranker | never, on this data volume |

**Wilson lower bound, not the raw rate**, at L1: 3 successes in 3 attempts is
not 100%. Using the lower bound means a model must *earn* its ranking with
volume, which is also a cheap anti-bubble mechanism.

---

## 4. Expected Completion Cost — replacing the brief's formula

The brief proposes:

```
ECC = initial_cost + P(failure)×retry_cost + P(escalation)×escalation_cost
      + context_reconstruction_cost + verification_cost + expected_regression_cost
```

**Five of six terms are unmeasurable today.** `initial_cost` has 2 distinct
values across 1,601 rows; `P(failure)` derives from a success rate that is 99.5%
by construction; `P(escalation)` has no record (the judge gating it wrote 0
rows); `context_reconstruction_cost` has no record; `expected_regression_cost`
needs a later regression linked to an earlier change, which nothing does.
Multiplying unmeasured probabilities by unmeasured costs yields a confident
number that means nothing — the exact failure this repo audited itself for twice
this month.

It is also in the **wrong currency**. Of the 214 rows with trusted provenance,
cost is **0.0 for all of them** — this fleet is local Ollama plus subscription
Claude. Dollars is not what is scarce here.

### The replacement: measure one thing

```
ECC(strategy, node_kind, scope) =
      Σ over ALL attempts in episodes that reached PASS, of
        w_t · tokens + w_q · quota_units + w_l · wall_clock_seconds
    ───────────────────────────────────────────────────────────────
      count(episodes ATTEMPTED with this strategy)
```

Properties that matter:

- **No estimated probabilities.** Everything is observed from `episode_node`.
- **Failed and abandoned episodes are in the numerator** (their cost was real)
  **and the denominator** (they were attempted). That is what makes a
  cheap-but-failing strategy look expensive — the brief's stated goal — with no
  `P(failure)` term at all.
- **Three currencies, weighted, declared.** `w_q` dominates on a subscription
  fleet; `w_t` when paying per token; `w_l` when a human is waiting. The weights
  are configuration, visible in the observability report, not a hidden constant.
- **Reported with n, or not reported.** Below n≈20, print "too few to tell".
  A rate without its denominator is not a measurement — the project has already
  reported four such rates and all four were noise.

Decompose into the brief's six terms **only as each earns its own
measurement**. `verification_cost` is the first that will, because verifier
nodes are separately recorded in `episode_node` from day one.

---

## 5. Escalation (§16)

MGEE already provides the mechanism: monotonic escalation over a finite tier
ladder, bounded attempts per (milestone, tier), with a termination proof.

What is missing is **evidence for the ordering**. Until L1:

- Escalate on a **verifier FAIL**, which is objective — not on a judge score.
  Today's P2 path escalates below `LLM_ROUTER_ESCALATE_THRESHOLD = 0.4` on a
  `judge_score` persisted in **0 of 1601 rows**, so whether it has ever fired is
  unknowable. Escalation must be observable or it is not evidence-based.
- **UNCERTAIN escalates the verification, not the model.** A weak check that
  cannot decide is a reason to check harder, not to spend more on generation.
  Collapsing UNCERTAIN into FAIL is how verification cost silently becomes
  model cost.
- Record every escalation as an `episode_event`, with the failure class. That
  is the dataset from which a better ladder is eventually derived.

---

## 6. Independent verification (§15)

```
implementer(node) ≠ auditor(node)   when   risk ≥ medium
                                     or   the change touches its own tests
```

The second clause is the one that matters and is easy to miss: a model that
wrote both the code and the test that checks it has verified nothing. AGR's
human gate already encodes the principle — the runner **refuses to sign by
default**, so a model may never approve its own work — and this is the same rule
one level down.

Cost of independence is bounded and known in advance: one extra call on an audit
node. Apply it by risk, not universally; on a typo fix it is pure overhead.

---

## 7. Exploration (§29) — the bubble is already here

`final_model` across 1,601 rows: **gpt-4o 1057, claude-opus 330** — 87% of all
history is two models. Until this week the bandit also trained on 1,387
placeholder rows (87% of its input) whose latency had a single distinct value.

Four defences, none requiring contextual bandits:

1. **Keep the `provenance = 'runtime'` filter** (landed 2026-09-23).
2. **Refuse to rank below n=30** rather than ranking on noise.
3. **A mandatory exploration floor** that cannot be configured to zero. With 13
   episodes/week, ε=0.1 is ~1 exploratory episode a week — slow, but the only
   thing that ever produces data about an unused model.
4. **Record the counterfactual** (`episode_node.candidates_json`): which models
   were eligible and rejected, and why. Without it no later analysis can recover
   what was never tried, and §29 becomes unfixable after the fact.

And the distinction the brief draws correctly, which must live in the **schema**
and not merely in prose: *"Yali usually does this"* (`convention.confidence`)
and *"this empirically performs better"* (`capability_outcome.pass_rate`) are
**separate fields**, never summed into one number.

---

## 8. What stays deterministic

| Deterministic | LLM-reasoned |
|---|---|
| Capability filtering | Milestone/plan proposal (already: `agentic/planner.py`) |
| Precedence resolution between conventions | Failure classification into a taxonomy |
| ECC arithmetic | Audit findings |
| Blast radius from the import graph | — |
| Acceptance check execution | — |

The planner is already the right shape: **a model proposes, a constrained
vocabulary validates.** Extend that pattern; do not invent a second one where a
model's free-text output becomes a routing decision.
