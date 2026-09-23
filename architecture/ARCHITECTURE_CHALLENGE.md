# ARCHITECTURE_CHALLENGE.md

Attacking the design in the other eleven documents. Not defending it.

---

## 1. What is unnecessarily complicated?

**The Task Graph Compiler's expansion stage (§8).** It is the only component
with no existing implementation, no evidence it is needed, and a plausible
failure mode: expanding `implement` into five layer-nodes that each need the
same context adds five model calls and saves nothing. I wrote a rule to catch
that (`TOKEN_EFFICIENCY.md` §7) — but a component that needs a rule to stop it
being harmful should probably be V2, not MVP. **Cut it from MVP.** The
hand-written template alone delivers §28.

**Three currencies in ECC.** `w_t · tokens + w_q · quota + w_l · seconds`
requires three weights nobody knows how to set. On this fleet, quota is the only
one that binds. A single-currency ECC with the other two *reported but not
optimised* is honest and has no tuning surface.

**The derived-view layer.** I claimed §3 and §4 are one dataset and then wrote
two SQL views. Two views over one table is fine, but "Experience Graph" and
"Capability Model" as named architectural components is grandeur for
`SELECT … GROUP BY`. They should be described as queries, not systems.

---

## 2. Which assumptions are unsupported?

| Assumption | Status |
|---|---|
| AGR accepts an injected runner rather than only a base URL | **Unverified, and Phase 1 collapses without it.** Flagged as a go/no-go but not checked |
| The user's workflow is stable enough to be a convention | Unverified. It may be that each task differs and "the convention" is an artefact of my reading three prompts |
| Graph orchestration beats one strong model with a good prompt | **Unverified. This is the whole thesis** — see Q15 |
| 13 episodes/week continues | Extrapolated from one 7-day window. It could be an artefact of an audit-heavy fortnight |
| Blast radius from the import graph improves verification selection | Plausible, untested. Python's dynamic imports may make the graph much less complete than I assume |
| Per-node context budgets reduce total tokens | **Possibly backwards.** More nodes with tight budgets may need more total tokens than one node with a large one, because shared context is re-sent |
| `nomic-embed-text` clusters procedural instructions well | Untested. It was chosen for prompt similarity, not for clustering imperative clauses |

That last one is load-bearing for Phase 3 and I assumed it without evidence.

---

## 3. What will fail at scale?

Scale here means *repo size*, not traffic — traffic is 13/week.

- **The import graph on a large monorepo.** `semantic/store.py` indexes this
  repo (412 files). At 50k files the transitive closure for blast radius is not
  bounded by depth 2 in any useful way.
- **The OKF store already failed at scale**: 298 MB, 12,448 project subdirs,
  many from test fixtures. A new store with the same write discipline repeats it.
- **`_build_and_filter_chain` at ~900 lines** gains a stage. It has
  hand-documented ordering dependencies and a history of bugs from stages added
  without respecting them. This is the riskiest edit in the plan.
- **Episode rows per graph run.** One episode with 20 nodes × 3 attempts = 60
  rows where there was previously 1. Storage is fine; what breaks is every
  existing query that assumes `routing_decisions` ≈ user requests. **Those
  queries are not enumerated anywhere in my plan.**

---

## 4. Where could incorrect historical knowledge poison future decisions?

- **`success = 1` in 1,593 of 1,601 rows.** Any learner treating this as a
  quality signal learns that everything works. If Phase 4 ever runs on
  pre-Phase-0 rows, it learns exactly that.
- **A convention learned from a period of unusual work.** This month has been an
  audit remediation. "Always audit" may be learned from a fortnight that is not
  representative of the next year — and decay does not fix it, because the
  evidence is dense and recent, which is precisely what decay privileges.
- **A stale project index asserting a module still exists.** Mitigated by
  `content_hash`, but only for files that still exist; a deleted module's
  entities linger unless something reaps them, and nothing in my design does.
- **An anti-pattern learned from one expensive success.** `verdict_delta ≤ 0`
  guards it, but with n=3 that delta is noise.

---

## 5. Where could convention learning become annoying?

**This is the most likely reason the feature gets turned off**, and auto-apply
(the chosen mode) maximises it.

- A 12-node graph fires on a typo fix because `complexity` was misclassified —
  and 49.8% of real prompts are classified by a *default* rather than a score.
  I gated auto-apply on `confident == True`, which means **auto-apply is
  unavailable for half of all prompts**. That is either a good safety property
  or a feature that rarely fires; I do not know which, and neither does the plan.
- The undo exists but is per-convention. There is no "not this time" — the user
  must disable the convention to skip it once. **That is a real gap.**
- Suggestions during focused work are interruptions. Nothing in the design
  throttles them.
- The destructive carve-out routes to a human gate — correct, but a user who
  wanted speed now gets a prompt they did not have before.

---

## 6. Where could routing create a self-reinforcing bias?

- **It already has**: gpt-4o 1057 + claude-opus 330 = 87% of 1,601 rows.
- **The subtlest form is in my own design**: a convention that auto-applies,
  succeeds, and counts that success as evidence for itself. I wrote the rule
  against it (`LEARNING_SYSTEM.md` §8) but an implementer who has not read that
  section will wire it the obvious way.
- **The capability filter can create a bubble.** Models with no capability
  record are excluded (fail-closed, deliberately). They therefore accumulate no
  data, so nothing ever justifies adding their record. **Fail-closed and
  exploration are in direct tension and I did not resolve it.** The exploration
  floor operates over *eligible* candidates only.
- **Wilson lower bound favours the incumbent** by construction — the model with
  volume wins ties. That is the intended anti-noise property and also a
  rich-get-richer mechanism.

---

## 7–10. What should be deterministic / LLM / embeddings / statistics?

| Deterministic | LLM | Embeddings | Statistics |
|---|---|---|---|
| Capability filtering | Plan/milestone proposal (constrained vocabulary — the existing `planner.py` pattern) | Convention clustering | Pass rates with Wilson bounds |
| Precedence resolution | Failure classification into a fixed taxonomy | Context ranking **second** to graph proximity | Decay |
| ECC arithmetic | Audit findings | Retrieval when no exact signal exists | Exploration ε |
| Blast radius | | | |
| Acceptance execution | | | |
| Budget enforcement | | | |

**The boundary that matters:** a model may *propose*; only a constrained,
validated vocabulary may *decide*. `agentic/planner.py` already does this — a
model proposes milestones, and any milestone with a subjective acceptance check
is rejected. Every new LLM use should copy that shape. Nowhere should a model's
free text become a routing decision.

---

## 11–12. What belongs where?

**agenticgraphs:** topology, scheduling, guards, cycles, retries, parallel
groups, subgraph composition, journal/resume, human gates.

**llm-router:** classification, capability filtering, model selection,
token/cost accounting, context building, episodes, conventions, **and
verification**.

The inversion is the interesting claim: AGR admits **61 of 83** graphs verify by
letting the model grade itself, and that `edit_files`/`run_suite`/`rollback`/
`execute_step` are *declared but not bound*. llm-router's `acceptance.py`
refuses self-report categorically. **The stronger implementation owns it.**

**Counter-argument I cannot dismiss:** this makes llm-router a dependency of
AGR's core value proposition, which is the coupling §1 warned against, in the
direction nobody noticed. If AGR's verification improves independently, this
inversion becomes a liability.

---

## 13. What should not be built?

Four graph databases · a vector database · an ML training pipeline · distributed
orchestration · **runtime graph mutation** (destroys the termination proof; every
§17 example is a router node with guarded edges) · **the six-term ECC formula**
(five terms unmeasurable) · **outcome prediction before the entry condition** ·
the `.llm-router/knowledge/*.graph` parallel tree (OKF-SCOPE-04) · **the
expansion stage in MVP** (Q1) · a second classifier table.

---

## 14. The smallest MVP that demonstrates real value

Smaller than Phase 0–2. **Three things:**

1. **The capability filter**, fail-closed. Stops the one catastrophic failure
   mode — routing to a model that cannot do the job.
2. **The episode record.** Without it nothing else is measurable, ever.
3. **One hand-written graph, run in-process, with per-node routing and
   `acceptance.py` verification.**

That is §28 working, with zero learning, zero conventions, zero expansion, zero
prediction. **If that does not demonstrate value, nothing later will** — because
everything later is an optimisation of it.

Explicitly not in MVP: conventions, expansion, context reuse, anti-patterns,
outcome learning, blast radius.

---

## 15. What experiment could falsify the entire thesis?

> Run §28 twice: once through the graph with per-node routing, once as a single
> strong model given the same prompt **and the same acceptance checks**. Compare
> verified completion and total tokens.

If one Opus call with *"implement this, run the tests, fix what fails, audit
it"* reaches the same verified completion for fewer total tokens than an 8-node
graph, **the orchestration layer is overhead** and the correct architecture is a
better prompt plus objective acceptance checks.

Note what this isolates: the acceptance checks are in **both** arms. So the
experiment does not test whether verification is valuable — that is
near-certain, and is the cheapest thing in this entire document. It tests
whether *decomposition and per-node routing* add anything beyond it.

**My honest prior: the graph loses on small tasks and wins above some
complexity threshold.** If so, the finding is not "graphs work" but *"graphs
work above a threshold"* — and that threshold becomes the convention trigger,
which is a more useful result than a yes.

It is cheap and runnable as soon as Phase 2 exists. **It should be run then**,
before Phases 3 and 4 are built on the assumption it passes.

---

## The three things most likely to be wrong

1. **The whole orchestration premise** (Q15). Untested, and everything else
   assumes it.
2. **That conventions are learnable from this user's prompt history**
   (Q2, Q5). Untested, and the clustering method was chosen for a different job.
3. **That per-node budgets reduce total tokens** (Q2). Plausibly backwards, and
   it is the mechanism by which the design keeps llm-router's original purpose.

Each has a cheap falsifying test. None has been run.
