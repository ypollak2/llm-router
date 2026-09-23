# Proposal — from "pick a model" to "pick and improve an execution strategy"

Status: **architecture reviewed; four decisions taken; implementation not started.**
Written 2026-09-23 against `main` @ `cebff84`.

## Decisions taken (2026-09-23)

| Question | Decision |
|---|---|
| How does llm-router drive agenticgraphs? | **In-process via `run_graph()`.** Blocker B1 is sidestepped, not solved — see §3 and Phase 1 |
| First thing built | **Phase 0.5, the capability filter** |
| Convention autonomy | **Auto-apply above a confidence threshold**, with a mandatory one-command undo — see §6 |
| Red CI | **Fix the three tests** — done, `dd9f6f8` |

The in-process choice accepts a hard dependency from llm-router on
agenticgraphs, which is tighter coupling than §1 of the brief asked for. That is
a deliberate trade for shipping speed and is recorded here so it is not later
mistaken for an oversight. The HTTP path remains available and B1 remains a real
blocker *for that path*; nothing below depends on it.

Scope note: the brief this responds to was truncated mid-sentence at item 30
("Avoid Architecture Astronautics… we may not need… complicated knowledg"). The
anti-astronautics argument below is my own reconstruction and may not match what
was intended. Items after 30, if any, are unaddressed.

---

## 0. The verdict, before the reasoning

**Build it. Not in the proposed order, and not with four graphs.**

Three findings decide the shape:

1. **The outcome substrate the design assumes does not exist.** `judge_score` is
   populated in **0 of 1601** routing decisions. `cost_usd` has **2 distinct
   values** across all 1601 rows. `subject` — which the bandit *indexes on* — is
   NULL in **1601 of 1601**. `corrections` has **0 rows** against 9 wired writers.
   Success is recorded as 1 in **1593 of 1601** (99.5%), which discriminates
   nothing. Every learning component in the brief (§4, §11, §16, §20, §21, §29)
   consumes this substrate.

2. **There is no notion of a task.** No table in the 16-table schema has a
   `parent_task_id`, `workflow_id` or `node_id`. `routing_decisions.session_id`
   and `prompt_sequence` are populated in 0 of 1601 rows. Every record is one
   isolated model call. **Multi-node workflow learning has nothing to attach to.**

3. **Recent volume is 13 rows in 7 days**, 214 in 30. A single
   `(task_type, model, project_area)` cell reaches n=30 in years, not months.

Consequence: **cold start (§25) is the steady state, not a phase.** Any design
whose critical path requires learned outcomes will never leave the fallback
branch. The architecture must be excellent with zero history, and treat learning
as a slow improvement that is always optional.

That is not a reason to abandon the design. It is a reason to inverting its build
order: **the instrumentation that makes outcomes real is deliverable one**, not a
by-product of deliverable four.

---

## 1. What already exists (do not rebuild)

Archaeology on both repos. Every row verified in code, not inferred from docs.

### llm-router

| Capability the brief asks for | Already exists as | State |
|---|---|---|
| §14 verification first-class | `agentic/acceptance.py` — objective **executable** checks; a milestone is DONE only on `cmd`/`lint`/`diff`/`canary`, **never** the model's self-report. `reproducible()` detects flaky. | Live |
| §7 task graph compiler | `agentic/planner.py` — a model proposes the milestone breakdown, every acceptance check is **constrained to the validated vocabulary**; a milestone proposing a subjective check is rejected | Live |
| §16 adaptive escalation | `agentic/engine.py` — MGEE: monotonic escalation over a finite tier ladder, bounded attempts per (milestone, tier) ⇒ **provably terminates** as COMPLETE or surfaced failure | Live |
| §13 context reuse / checkpoints | `agentic/ledger.py` — `TaskLedger`, frozen done-frontier, escalation resumes at first pending milestone handing frozen artifacts as read-only context | Live |
| §2 Project Semantic Graph | `semantic/store.py` — SQLite code-entity index: `find_definitions`, `find_importers`, `known_files` | Live |
| Project knowledge | `okf.py` — Open Knowledge Format, per-project, **never stores model prose** ("the hallucination amplifier") | Live, on by default |
| Scope resolution | `semantic/scope.py:resolve_scope()` — single canonical resolver; `resolve_scope_or_none()` returns None rather than guessing | Live |
| §4 outcome ranking | `bandit.py` + `telemetry.py`, `MIN_SAMPLES_FOR_SIGNAL = 30` | Live, **starved** |
| Savings integrity | `savings.py` — `canonical_savings()`, `SURFACES` registry, hardcoded `_baseline_model()` so no surface picks its own baseline | Live |
| Routing baseline | `scripts/bench_session_replay.py` — 5 real sessions, **76% drafts / 66% acceptable** over 115 prompts | Live |

MGEE is reached via `tools/agentic.py` → `service.run_delegation` → `engine.Agent`.

**Its design doc does not exist.** Four docstrings cite `docs/agentic-router.md`
for the guarantees; there is no such file. The termination proof is unreadable.

### The live pipeline, corrected

Worth stating because a plausible reading of the module names is wrong.
`chain_builder.py` is **dead** — self-documented since 2026-09-15: *"NOT the live
chain builder… every repository reference to this module's `build_chain()` is a
test."* `provider_registry.py` is dead too, despite a docstring claiming the
routing path reads it.

The real path is `router.py:_build_and_filter_chain` (~900 lines), a sequential
list-mutation pipeline with hand-documented ordering dependencies ("must run
before injection", "applied LAST", "re-applied after injection because it wasn't
re-checked"). Several past bugs came from stages inserted without respecting that
order. **This is the riskiest seam in the codebase and the one a naive
implementation of this brief would reach for first.**

Two further facts that bear on the design:

- **P2 quality-gated escalation already exists** (`router.py:2538-3107`): a judge
  scores a cheap model's answer and escalates below `LLM_ROUTER_ESCALATE_THRESHOLD`
  (0.4). But judge scores are persisted in **0 of 1601** rows — so *we cannot tell
  whether this has ever fired.* Evidence-based escalation is implemented and
  unobservable, which is the same defect class as an unread counter.
- **There are two hand-duplicated classifier tables** — `classify.py:_SIGNALS` and
  `hooks/auto-route.py:SIGNALS` — an explicit "Option B, keep in sync manually"
  decision. The hook does **not** call `classify_signals()`. Any new pre-routing
  signal added at the clean seam therefore **does not reach the hook**, which is
  the surface the user actually interacts with most.

### agenticgraphs (`vitruvian-graphs` 0.10.0, AGR spec v1.9)

| Capability | State |
|---|---|
| Graphs as pure YAML, JSON-Schema validated | Yes — **runtime-constructible**, which is what §8 needs |
| Node kinds | `agent`, `verifier`, `human`, `router`, `subgraph`, `search` |
| **Guarded cycles** | Yes. The lint **rejects unconditional back-edges**; every loop carries a `when` guard plus `termination.max_steps` |
| Typed failure taxonomy | 10 categories (`parse_failures`, `timeouts`, `gate_refused`, `assert_failures`, `command_failures`, `budget_exhausted`, `deadlocked`, …) |
| Error handling | `kind: error` and `kind: compensate` edges; per-node `retries.max`; non-idempotent abilities must declare `reissue_effects` |
| Parallelism | ThreadPoolExecutor for same-`parallel_group` nodes. `fan_out` shards run **sequentially** |
| Human gate | `kind: human`; the runner **refuses to sign by default** — a model may never approve its own work |
| Composition | `kind: subgraph` inline expansion, depth ≤ 3, **acyclic** across refs |
| Checkpoint/resume | JSONL journal replay; caller owns the file |
| Observability | `RunReport` with `trace`, `frames`, `tool_calls`, `usage` carrying **`usd_measured: bool`** |
| Library | 83 graphs, 17 motifs, ~501 tests |

**Provider abstraction: none.** Three env vars (`AGR_LLM_BASE_URL`,
`AGR_LLM_MODEL`, `AGR_LLM_API_KEY`) pointing at one OpenAI-compatible endpoint.
No registry, no routing, no cost logic. **Zero duplication risk with llm-router**
— the separation the brief asks for is already structural.

---

## 2. The three blocking facts

### B1 — agenticgraphs cannot run through llm-router's gateway today

AGR's `ToolRunner` drives an OpenAI tool-calling loop. llm-router's gateway calls
`_refuse_tools_if_present` and returns **HTTP 400** whenever `tools` or
`tool_choice` is present — deliberately (H-03: better a clean 400 than silently
dropping the tools). So the obvious integration works **only for graphs whose
nodes use no abilities**.

Compounding it: `gateway_service.py` has **zero callers** anywhere in `src/`. The
gateway process is not started by `cli.py`, `install_hooks.py`, `onboard.py`,
`quickstart.py` or `commands/serve.py`. It must be hand-run as a module.

**This is a prerequisite, not a detail.** Nothing in the brief is reachable until
llm-router can serve a tool-calling request and the gateway is startable.

### B2 — the episode does not exist

See §0.2. Until a routed call can say which task and which node it belonged to,
there is no unit of learning for workflows, conventions, or expected completion
cost. This is one schema change plus writers, and it gates almost everything else.

### B3 — there is no capability filter, only a preference ordering

The brief's §10 ("never choose a cheap model that cannot actually complete the
task") describes a **filter**. No filter exists on the live path.

- `model_registry.py` holds `ModelMetadata` with `context_window` and a
  capability tuple (vision / function-calling / json / reasoning), hardcoded per
  model. **Nothing outside that module and its tests reads either field.**
  `router.py` imports exactly one constant from it, `GOOGLE_PROVIDERS`.
- `capabilities.py` describes what a *task* needs, not what a *model* can do. It
  is gated off by default (`LLM_ROUTER_CAPABILITY_ROUTING`, "shadow mode"), its
  only production consumer is `hooks/chain_builder.py` — itself a dead path — and
  `cost.py` merely **logs** it, annotated "never read by live routing".

So a vision task and a text-only local model appear in the same chain with no
gate between them. Ordering can put the incapable model second; nothing stops it
being reached when the first fails.

**The good news:** the machinery is built and shadowed. Turning §10 from absent to
real is *wiring an existing detector into a filter*, not new design. That makes it
the cheapest high-value item in the whole brief.

### B4 — verification is strong in one repo and weak in the other

agenticgraphs states plainly that **61 of 83 graphs** sit at its weakest
verification depth (the model's own assert-graded account of itself), only **20 of
83** reach executable `command` depth, and that `edit_files`, `run_suite`,
`rollback`, `execute_step` are **declared but not bound to real endpoints** —
"narrated, not executable."

llm-router's `acceptance.py` refuses self-report categorically.

This inverts the naive split. **agenticgraphs should not own verification.**

---

## 3. Ownership split

| Concern | Owner | Why |
|---|---|---|
| Graph topology, scheduling, guards, cycles, retries, parallel groups, subgraph composition, journal/resume, human gates | **agenticgraphs** | Built, tested, spec'd. Reimplementing is pure waste |
| Task classification, capability filtering, model selection, token/cost accounting, context building | **llm-router** | Built, and it is the product's reason to exist |
| **Acceptance / verification checks** | **llm-router** | `acceptance.py` is materially stronger than AGR's. AGR `verifier` nodes and `verification[].command` should call it |
| Episode log, conventions, experience | **llm-router** | It is the only side that sees the user across projects and sessions |
| The contract between them | **shared, tiny** | A node-execution request/response and an acceptance-check request/response. Two JSON shapes, versioned |

**The high-value integration nobody has built:** let AGR's `verifier` nodes
delegate to llm-router's constrained check vocabulary. That upgrades 61 of 83
graphs from self-report to executable verification, and it benefits agenticgraphs
independently of this project. It is also the smallest useful thing that makes
the two repos worth connecting at all.

Coupling direction: **llm-router depends on agenticgraphs, never the reverse.**
agenticgraphs keeps working with a bare OpenAI endpoint and no llm-router present.

---

## 4. Cutting four knowledge systems down to three stores

The brief proposes four graphs. Three of the four are the same data at different
aggregation levels, and none of them needs a graph database.

| Brief | Proposal | Substrate |
|---|---|---|
| §2 Project Semantic Graph | **Extend `semantic/store.py`** | Existing SQLite code-entity index |
| §3 Experience Graph | **Derived view over the episode log** | No separate store |
| §4 Capability & Outcome Model | **Derived view over the episode log** | No separate store |
| §5 Workflow Convention Graph | **A small, human-editable file** | YAML, tens of entries |

§3 and §4 are not two systems. "What happened before" and "who is good at this"
are the same events grouped differently — by project area versus by model. Storing
them twice guarantees they disagree, and this repo has already paid for exactly
that (`_column_exists` drifting against a hand-maintained table; four modules
disagreeing on scope resolution, which cost a cross-project contamination bug).

**One append-only episode log. Two derived views. No new graph DB, no vector DB.**

Embeddings already exist (Ollama `nomic-embed-text`, used by `semantic_cache.py`
and `semantic_classify.py`). Reuse them; do not add a vector store.

Storage follows `paths.state_path(...)` with per-project subdirectories keyed by
`project_slug()`/`scope_key()`. **Do not invent a second `~/.llm-router-*` tree** —
that is the OKF-SCOPE-04 lesson, already paid for once.

### The keystone: the Episode

Everything downstream is a query over this. Nothing else is worth building first.

```
episode        id, started_at, ended_at, intent_text, project_scope,
               convention_id, outcome (PASS|FAIL|ABANDONED|SURFACED),
               human_corrected (bool)
episode_node   episode_id, node_id, graph_ref, node_kind, attempt_n,
               model, tier, tokens_in, tokens_out, cost_usd, cost_measured (bool),
               latency_ms, verdict (PASS|FAIL|UNCERTAIN), check_kind, check_detail
episode_event  episode_id, ts, kind, payload   -- corrections, escalations, gates
```

Three properties, each bought with a scar this repo already has:

- **`cost_measured` is a boolean, not an assumption.** 1387 of 1601 existing rows
  carry a flat $0.01 placeholder. AGR already does this (`usd_measured`); match it.
- **`verdict` has three values, not two.** UNCERTAIN must not collapse into either
  PASS or FAIL. (S9, this month: a NULL confidence coerced to 0.0 produced 213
  "High"-confidence `CLASSIFIER_ERROR` findings out of a column nobody wrote.)
- **Absence is never zero.** `scripts/lint_unknown_as_number.py` now ratchets this.

---

## 5. Where the brief is wrong

### §17 runtime graph mutation — do not build it

AGR computes its node/edge list once before the run loop; there is no mid-run
mutation API. Adding one would cost a great deal and **would destroy the
termination proof** that makes both MGEE and AGR safe.

It is also unnecessary. Every example in §17 — syntax error → cheap model,
architectural mismatch → return to Architecture, unknown dependency → Research,
repeated failure → stronger model — is a **router node whose outgoing edges are
guarded by the failure taxonomy**. AGR already has `kind: router`, guarded
back-edges, `kind: error` edges, and a 10-category typed failure classification.

The topology is static; the *path* is dynamic. That is the whole point of guards.

**Recommendation: express §17 as static topology with guarded edges.** If a case
genuinely cannot be expressed that way, that is a finding worth having — record it
rather than pre-emptively building a mutation engine for it.

### §11 Expected Completion Cost — the formula cannot be evaluated

```
ECC = initial_cost + P(failure)×retry_cost + P(escalation)×escalation_cost
      + context_reconstruction_cost + verification_cost + expected_regression_cost
```

Six terms. Today: `initial_cost` has 2 distinct values across 1601 rows;
`P(failure)` derives from a success rate that is 99.5% by construction;
`P(escalation)` has no record; `context_reconstruction_cost` has no record;
`expected_regression_cost` requires linking a later regression to an earlier
change, which nothing does. **Five of six terms are unmeasured**, and multiplying
unmeasured probabilities by unmeasured costs produces a confident number that
means nothing — the exact failure this repo audited itself for twice this month.

It is also wrong in units. Of the 214 rows with trusted provenance, **cost is 0.0
for all of them** — this fleet is local Ollama plus subscription Claude. Dollars
is the wrong currency. What is actually scarce is **subscription quota, wall-clock,
and the user's attention.**

**Replacement — measure one thing, directly:**

```
ECC(strategy) = Σ(tokens, wall_clock, quota) over ALL attempts in episodes
                that reached PASS, ÷ episodes attempted
```

One observed quantity per strategy. No estimated probabilities. Failed and
abandoned episodes are in the numerator (their cost was real) and the denominator
(they were attempted) — which is precisely what makes a cheap-but-failing strategy
look expensive, the brief's stated goal, **without estimating anything**.

Decompose into the six terms only once each has its own measurement. Report it
with n, and say "too few to tell" below n≈20 rather than printing a number.

### §29 the self-reinforcing bubble is not hypothetical — it is already here

`final_model` across 1601 rows: **gpt-4o 1057, claude-opus 330.** 87% of all
history is two models. Until this week the bandit also trained on 1387 placeholder
rows (87% of its input) whose latency had a single distinct value.

Concrete defences, all cheap, none requiring contextual bandits:

- Keep the `provenance = 'runtime'` filter (landed this week).
- Keep `MIN_SAMPLES_FOR_SIGNAL = 30` and **refuse to rank** below it rather than
  ranking on noise — with 13 rows/week this is the common case, not the edge case.
- A **mandatory exploration floor** that cannot be tuned to zero.
- Record the **counterfactual**: which candidates were considered and rejected.
  Without it, no amount of later analysis can recover what was never tried.

The brief's own distinction is the right one and deserves enforcement in the
schema: *"Yali usually does this"* and *"this empirically performs better"* must
be **separate fields**, never summed into one confidence.

### §30 architecture astronautics — the concrete cut list

Not needed, at any maturity level yet reached: a graph database, a vector
database, an ML training pipeline, distributed orchestration, a learned ranker, a
policy network. With 13 episodes/week, a hand-written rule with an evidence
counter outperforms any learned model and is inspectable, which the brief requires.

---

## 6. Why conventions (§5/§6) can ship before outcomes (§4)

They need different amounts of evidence by two or three orders of magnitude.

- Recognising *"this user wants an audit before done"* needs **5–20 observations**.
- Estimating `P(success | task, model, tools, project, context)` needs **hundreds
  per cell**, and only 2 of the top-10 `(task_type, model)` cells clear n=30.

At 13 episodes/week, conventions are reachable **this quarter**; outcome
prediction is not reachable this year. Shipping them together means shipping
neither.

Convention lifecycle, with the brief's stages and explicit thresholds:

```
Observed (1)  →  Candidate (3, similar, ≥2 sessions)  →  Suggested (5, user sees it)
              →  Accepted (user confirms once)        →  Default
              →  Continuously evaluated (demoted on 2 consecutive overrides)
```

**Decision: auto-apply above a confidence threshold.** The system builds and runs
the convention's graph without asking, and says so in the report.

I recommended suggest-only; the call went the other way, and it is a reasonable
one — it is the only option that delivers §28's experience in full. It changes
what the design must carry, and the change is not optional:

- **A one-command undo is mandatory, not a nicety.** The report names the
  convention that fired and the exact command that disables it and drops its
  confidence below threshold. Without that, a convention learned from a
  coincidence runs real work with no brake, and the only remedy is editing a
  store the user did not write.
- **An override is evidence.** Cancelling or overriding an auto-applied
  convention is the strongest negative signal available (§20/§21) and must be
  recorded as such — two consecutive overrides demote below threshold
  automatically.
- **The threshold is a floor, not the whole gate.** Auto-apply requires the
  confidence AND a matching scope (§22) AND no conflicting hard convention
  (§23). Confidence alone must never be sufficient.
- **Never auto-apply where the graph does destructive work** — migrations,
  releases, force-pushes, anything outward-facing. Those route to `kind: human`,
  whose runner already refuses to self-sign. This is the one place where the
  brief's §18 and §23 safety concerns bite, and it is a hard carve-out rather
  than a confidence question.

Other non-negotiables:
- **Three kinds, stored distinctly**: hard (user said "always"), learned
  (inferred), contextual (scoped). A learned convention may never outrank a hard one.
- **Scope is mandatory** (§22): global / user / repo / task-type / language / risk.
  A convention with no scope is a bug, not a default.
- **Conventions are a file the user can open, edit and delete.** If the only way
  to correct one is through the system that inferred it, it is not inspectable.

Precedence (§23) — the brief's ordering is nearly right; one change:

```
explicit instruction in THIS request
  > safety/security policy
  > hard convention ("always…")
  > task-specific contextual convention
  > repo convention
  > user convention
  > general optimisation
```

The brief puts safety above explicit instruction. **Wrong for a developer tool**:
the user must be able to say "skip the audit, this is a typo fix" and be obeyed.
Safety outranks *learned* conventions and *optimisation*, never a live human
instruction. What safety may do is **require the override to be explicit and
recorded** — which is a different mechanism from overruling it.

---

## 7. Migration plan

Every phase has a gate that can fail. Phases are ordered by dependency, and each
is independently valuable — if the project stops after any phase, what shipped
still works.

### Phase 0 — make the substrate honest (prerequisite for everything)

| # | Work | Gate |
|---|---|---|
| 0.1 | Write `docs/agentic-router.md`, the missing MGEE spec its four docstrings cite | The termination argument is readable and matches `engine.py` |
| 0.2 | Episode schema + writers; `parent_task_id`/`node_id` on routed calls | A multi-node run produces one episode with n linked nodes; a single call produces an episode of 1 |
| 0.3 | Populate `subject` (NULL in 1601/1601) or delete it from the bandit key | Either non-NULL on new rows, or the bandit no longer indexes a column that is always NULL |
| 0.4 | Make `corrections` actually receive rows from its 9 wired writers | A rejected/redone route produces a row |
| 0.5 | Decide `judge_score`: wire it for real or remove the column and the claim | Either non-zero rows, or `judge.py`'s ranking path is deleted |
| 0.6 | Clean the 298 MB OKF store; fix test isolation | `knowledge/projects/` contains no test-fixture directories; a test run adds none |
| 0.7 | Persist the P2 escalation judge's score, or state that escalation is unobservable | Either escalation events appear in the episode log, or the claim that escalation is evidence-based is withdrawn |
| 0.8 | Delete `chain_builder.py` and `provider_registry.py` | Suite green after removal; no production importer existed |
| 0.9 | **Gate under a clean HOME.** Three tests passed only on a machine with prior state and were red on CI for two releases (`dd9f6f8`) | `HOME=$(mktemp -d)` on every local suite run; `pre-release-verify.sh` does the same |

Gate for the phase: `scripts/bench_session_replay.py` unchanged within noise
(3 runs, report mean and spread — per the repo's own N7 rule).

**A verification lesson, paid for on 2026-09-23.** The local suite was green
while CI was red across 15.0.1 and 15.1.0, and 15.1.0 shipped over a red CI
because `pre-release-verify.sh` runs the suite *locally* and never consults CI.
The three failures all asserted conditions a developer machine satisfies by
accident. Any gate in this plan that runs only on a machine with 298MB of
accumulated history is not a gate — this is the same "passes for the wrong
reason" class the 2026-09-22 audit spent itself on, arriving from the
environment instead of from the code.

### Phase 0.5 — the capability filter (cheapest high-value item in the brief)

Deliberately its own phase: it is independent of everything else, it is the
brief's §10, and the machinery is already written and shadowed.

| # | Work | Gate |
|---|---|---|
| 0.5a | Run `detect_capabilities()` in shadow on the replay corpus | A report of how often the live chain contains a model that cannot serve the task — the number that justifies (or kills) the work |
| 0.5b | Wire it as an actual `filter()`, behind the existing env flag | A vision task never lists a text-only model; a task needing 200k context never lists an 8k model |
| 0.5c | Give `model_registry`'s `context_window`/capability tuple a reader | The R12 counter-registry rule, applied to model metadata: data nothing reads is not data |

Fail-closed rule, non-negotiable: **a model whose capabilities are unknown is not
silently eligible.** Unknown is not "capable". This repo has now shipped six
instances of unknown rendering as the favourable answer; this would be the seventh.

### Phase 1 — drive agenticgraphs in-process (decided)

The HTTP route (tool-calling on the gateway + making `gateway_service.py`
startable) is **deferred, not cancelled**. In-process avoids both blockers.

| # | Work | Gate |
|---|---|---|
| 1.1 | `agenticgraphs` as a dependency; a thin adapter that builds a graph dict and calls `run_graph()` | A hand-written graph runs end to end from llm-router with no HTTP involved |
| 1.2 | Route each `kind: agent` node's model through llm-router instead of `AGR_LLM_MODEL` | A single run uses ≥2 different models, and the report says which node got which and why |
| 1.3 | Expose `acceptance.py`'s check vocabulary to AGR `verifier` nodes | A verifier node runs a real `cmd` check and **fails on a real failure** |
| 1.4 | Map AGR's `RunReport` (`trace`, `frames`, `tool_calls`, `usage`) onto the episode schema | One `run_graph()` call produces one episode with n linked nodes |

Two things to watch, both consequences of the in-process choice:

- **AGR's `LLMRunner` posts to an OpenAI-compatible endpoint.** In-process, that
  has to be replaced by an injected callable, not a URL. Confirm AGR permits
  injecting a runner before committing to 1.2 — if it only accepts a base URL,
  the in-process path collapses back into the HTTP path and B1 returns.
- **Version coupling.** llm-router pins an `agenticgraphs` version; AGR is at
  0.10.0 with spec v1.9 and still moving. Pin exactly, and treat an AGR upgrade
  as a change that re-runs the replay baseline.

1.3 is the one to do even if the rest is dropped: it is a standalone improvement
to agenticgraphs' weakest area (61 of 83 graphs at self-report depth).

### Phase 2 — static workflow templates, no learning

| # | Work | Gate |
|---|---|---|
| 2.1 | Express the user's known workflow as an AGR graph, by hand | `implementation_with_verification_loop.yaml` passes `validate_graph`, loops are guarded, terminates |
| 2.2 | Route each node through llm-router | The observability report names a different model per node with a reason |
| 2.3 | Per-node token budget and context slice | An implementation node does not receive the whole repo; budget is enforced, not advisory |

**After Phase 2 the headline scenario in §28 works** — "Implement the plan" builds
and runs the right graph — driven by a hand-written template and zero learning.
Everything after this is improvement, not function.

### Phase 3 — conventions

| # | Work | Gate |
|---|---|---|
| 3.1 | Convention store + scope + precedence | Two conflicting conventions resolve deterministically and the resolution is explained |
| 3.2 | Candidate detection from repeated instructions | Replaying real session history surfaces the known convention and ≤1 false candidate |
| 3.3 | Suggest / accept / demote, human-in-the-loop | Nothing reaches Accepted without an explicit act; 2 overrides demote |

3.2's gate is the honest one: run it over the existing transcripts and see whether
it finds the convention the user knows is there. If it cannot, the feature does
not work, regardless of how good the schema is.

### Phase 4 — outcome learning, only if the data arrived

Entry condition, checked before any work starts:

> ≥ 200 episodes with a real `verdict`, spread over ≥ 3 models, with ≥ 30 in each
> of ≥ 4 `(task_type, model)` cells.

**If that condition is not met, do not start Phase 4.** At 13 episodes/week it
will not be met for a long time, and that is a finding, not a failure — Phases 0–3
deliver the brief's headline scenario without it.

---

## 8. Observability (§19) — one rule

The brief's example report is right in shape and dangerous in detail: it prints
"Estimated naive baseline: 240K, Token reduction: 65%".

**A baseline nobody ran is not a measurement.** This repo already has the scar
(`savings.py`'s `SURFACES` registry exists because ~20 surfaces each invented
their own baseline) and already has the fix (`canonical_savings()`, a hardcoded
`_baseline_model()`). Any token-reduction claim in the new report must come
through the same canonical path, carry its n, and be omitted when unmeasured.

Shipping the report without the savings line is always available and is often
right.

---

## 9. Open questions for the user

1. The brief is truncated at item 30. What was cut?
2. Phase 1.1 (tool-calling on the gateway) is a substantial piece of work that
   exists to unblock the integration. Is that worth it, or should llm-router drive
   AGR graphs **in-process** via `run_graph()` instead, leaving the gateway alone?
   The in-process path avoids B1 entirely and is much cheaper — but it couples the
   repos more tightly than the brief wants.
3. Phase 0.5: wire `judge_score` for real, or delete it? It has been wired and
   empty long enough to be a standing false claim.
4. Is dollars or quota/wall-clock the currency to optimise? The measured answer on
   this fleet is quota and wall-clock; the brief assumes dollars.
5. The hook keeps its own classifier table, separate from `classify.py`. Every
   new pre-routing signal has to be built twice or the hook does not get it.
   Reconciling them is already backlog item N8 and is now a **dependency of this
   whole design**, not a tidy-up. Promote it?

---

## 10. What I did not verify

Stated so the plan is not read as more certain than it is.

- **No graph was run end to end** through llm-router. B1 is inferred from reading
  `_refuse_tools_if_present` and AGR's `ToolRunner`, not from a failed request.
- **The convention-detection gate (3.2) is unproven.** Whether repeated
  instructions in this user's real transcripts are separable from one-off
  requests is an open empirical question. It could fail.
- **`scripts/bench_session_replay.py` was not re-run.** The 76%/66% baseline is
  quoted from `Docs/BACKLOG_NEXT.md`, which also warns the number needs 3 runs
  with a reported spread before it is trustworthy (its own item N7).
- **Episode volume is assumed to continue at ~13/week.** If the workflow changes,
  Phase 4's entry condition could be met much sooner — or never.
