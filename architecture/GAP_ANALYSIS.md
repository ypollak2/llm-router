# GAP_ANALYSIS.md

Every capability the brief proposes, classified **Exists / Partial / Missing /
Should not build**, with the reason and the evidence. Evidence lives in
`CURRENT_ARCHITECTURE.md`; this file does not repeat it.

Legend for "Where": **R** = llm-router, **A** = agenticgraphs, **S** = shared
contract, **—** = not built.

---

## §2 Project Semantic Graph

| Capability | State | Where | Why |
|---|---|---|---|
| Code entity index (defs, importers, files) | **Exists** | R | `semantic/store.py`, SQLite |
| Module/package/class/function entities | **Partial** | R | Definitions and importers are indexed; ownership, runtime components, queues, schemas are not |
| Cross-entity semantic relations (Auth → TokenManager → SessionStore) | **Missing** | R | The index answers "who imports X", not "what concept does X belong to" |
| Blast-radius estimation | **Missing** | R | Derivable from the importer graph — cheap, and the highest-value new query |
| Graph **database** | **Should not build** | — | §30. SQLite already holds it; a second store guarantees drift (four modules once disagreed on scope resolution and it cost a contamination bug) |

**Verdict:** extend one existing table. The import graph already present gives
blast radius and verification selection for near-zero cost.

---

## §3 Engineering Experience Graph

| Capability | State | Where | Why |
|---|---|---|---|
| Architectural decisions | **Partial** | R | `Docs/decisions/` exists — but `Docs/` is **entirely untracked** (`.gitignore:111` `/docs/*`), so the corpus lives on one machine |
| Incidents / regressions / fixes | **Partial** | R | Recorded richly in prose (`audit/`, CLAUDE.md, code comments) and **not** in any queryable form |
| Test failures linked to a change | **Missing** | R | Nothing ties a routed call to a later test outcome |
| Human corrections | **Missing** | R | `corrections` table exists with **9 wired writers and 0 rows** |
| Confidence / decay / supersession / contradiction | **Missing** | R | No mechanism anywhere |
| Separate "experience graph" store | **Should not build** | — | It is the same events as §4 grouped differently — see below |

**Verdict:** §3 and §4 are one dataset at two aggregations. "What happened to
TokenManager" and "who is good at Python" are the same rows keyed by project
area versus by model. Two stores guarantee disagreement.

---

## §4 Capability and Outcome Model

| Capability | State | Where | Why |
|---|---|---|---|
| Outcome history per (profile, subject, model) | **Partial** | R | `telemetry.aggregate_stats` + `bandit.py` exist and are wired |
| Real success signal | **Missing** | R | `success` is 1 in 1593/1601. It means "non-empty and not a refusal", not "correct" |
| Graded quality | **Missing** | R | `judge_score` non-NULL in **0 of 1601** — wired end to end, never produced a row |
| Measured cost | **Missing** | R | 2 distinct values across 1601 rows |
| `subject` dimension | **Missing** | R | NULL in 1601/1601 **while the bandit keys on it** |
| Verification outcome | **Missing** | R | Nothing links a call to a test/build result |
| Retries, latency | **Exists** | R | `latency_ms` has 215 distinct values — genuinely measured |
| P(success \| task, model, tools, project, …) | **Should not build (yet)** | — | Only 2 of the top-10 `(task_type, model)` cells clear n=30, and there is **no label** to predict. At 13 rows/week this is years away |
| ML training pipeline | **Should not build** | — | §30, and the data forbids it |

**Verdict:** the model is not starved of algorithm, it is starved of **labels**.
Building a predictor now would fit noise and look authoritative.

---

## §5 Workflow Convention Graph

| Capability | State | Where | Why |
|---|---|---|---|
| Any representation of "how this user works" | **Missing** | R | Nothing anywhere |
| Trigger matching (task_type, scope) | **Partial** | R | `classify_signals` gives task_type and complexity already |
| Convention → graph binding | **Missing** | S | Needs the AGR YAML, which is runtime-constructible |
| Scope, confidence, lifecycle | **Missing** | R | |
| Graph **database** for conventions | **Should not build** | — | Tens of entries. A YAML file the user can open and edit — inspectability is a stated requirement, and a DB is the opposite of it |

**Verdict:** entirely new, and **the cheapest of the four** — it needs 5–20
observations, not thousands.

---

## §6 Learning conventions

| Capability | State | Where | Why |
|---|---|---|---|
| Prompt corpus to learn from | **Exists** | R | `scripts/groundtruth/sources.py` with documented drop rules; n=1571 real prompts after filtering |
| Semantic similarity | **Exists** | R | Ollama `nomic-embed-text` already used by two subsystems |
| Occurrence tracking, promotion thresholds | **Missing** | R | |
| Hard vs learned vs contextual distinction | **Missing** | R | Must be separate fields, not one confidence |

---

## §7–§9 Task graph compiler, dynamic construction, graph of graphs

| Capability | State | Where | Why |
|---|---|---|---|
| NL → structured plan | **Partial** | R | `agentic/planner.py` does exactly this for milestones, with the check vocabulary constrained and validated |
| Plan → executable topology | **Exists** | A | YAML is data; `validate_graph` exists for generated candidates |
| Template + task-specific expansion | **Missing** | R | The expansion step is genuinely new work |
| Subgraph composition | **Exists** | A | `kind: subgraph`, depth ≤ 3, acyclic |
| Inspectable before execution | **Exists** | A | The graph is YAML; printing it is free |
| One enormous DAG | **Should not build** | — | §9 says so and AGR already enforces depth ≤ 3 |

---

## §10 Execution-aware routing

| Capability | State | Where | Why |
|---|---|---|---|
| Task capability requirements | **Exists, shadowed** | R | `capabilities.detect_capabilities:165`, off by default, only consumer is a dead path |
| Model capability data | **Exists, unread** | R | `model_registry.ModelMetadata` — `context_window` and the capability tuple have **no reader outside the module and its tests** |
| **Capability filter** | **Missing** | R | Ordering only. A vision task and a text-only model sit in the same chain ungated |

**Verdict:** the single cheapest high-value item in the brief. Both halves are
built; nothing joins them.

---

## §11 Expected completion cost

| Capability | State | Where | Why |
|---|---|---|---|
| Per-call cost | **Partial** | R | Recorded, but a flat placeholder on 87% of rows |
| Cost of a *task* (all attempts) | **Missing** | R | No task identity exists |
| `P(failure)`, `P(escalation)`, regression probability | **Missing** | R | No labels; escalation is unobservable |
| The six-term formula as specified | **Should not build** | — | Five of six terms unmeasured. See `ROUTING_MODEL.md` for the replacement |

---

## §12–§13 Token efficiency and context reuse

| Capability | State | Where | Why |
|---|---|---|---|
| Compression | **Exists** | R | `context_optimizer.py` |
| Recency truncation | **Exists** | R | |
| Retrieval **with ranking** | **Missing** | R | No scoring anywhere; OKF injects at a fixed `limit=3` |
| Per-node token budget | **Missing** | R | |
| Context artifact reuse by fingerprint | **Missing** | R | Three content hashes exist, none for this |
| Semantic cache | **Exists** | R | With a discriminator after the passport incident |
| Provider prompt caching | **Missing** | R | Schema columns exist, **no writer**. A standing claim gap |
| Dedicated vector DB | **Should not build** | — | §30. Embeddings already available locally |

---

## §14–§15 Verification and independence

| Capability | State | Where | Why |
|---|---|---|---|
| Executable acceptance checks | **Exists** | R | `agentic/acceptance.py` — `cmd`/`lint`/`diff`/`canary`, never self-report |
| Flaky detection | **Exists** | R | `reproducible()` |
| PASS / FAIL / **UNCERTAIN** | **Partial** | R | The engine has pass/fail; UNCERTAIN as a first-class third state is new |
| Verifier nodes in a graph | **Partial** | A | `kind: verifier` exists but **61 of 83 graphs let the model grade itself** |
| Independent auditor ≠ implementer | **Missing** | R | Nothing enforces it |
| Human gate a model cannot sign | **Exists** | A | The runner refuses by default |

**Verdict and the key inversion:** llm-router's verification is **stronger** than
agenticgraphs'. Verification must be owned by R and *called* by A — not the
naive split where A owns everything execution-shaped.

---

## §16–§17 Escalation and runtime mutation

| Capability | State | Where | Why |
|---|---|---|---|
| Monotonic escalation with a termination proof | **Exists** | R | `agentic/engine.py` (MGEE) |
| Quality-gated escalation | **Exists, unobservable** | R | P2 fires on a judge score persisted in 0 rows |
| Evidence-based ladder ordering | **Missing** | R | Needs labels |
| **Runtime graph mutation** | **Should not build** | — | AGR freezes topology pre-run; adding mutation destroys the termination proof. Every §17 example is a `kind: router` node with guarded edges — static topology, dynamic path |

---

## §18–§19 Human interaction and observability

| Capability | State | Where | Why |
|---|---|---|---|
| Stop-and-ask node | **Exists** | A | `kind: human` |
| Rule for *when* to ask | **Missing** | S | |
| Per-node execution trace | **Exists** | A | `RunReport.frames`, `tool_calls` |
| Measured vs estimated cost flag | **Exists** | A | `usd_measured: bool` — R should copy this |
| Why-this-model explanation | **Missing** | R | |
| "Estimated naive baseline / token reduction" | **Partial, dangerous** | R | `savings.py` has `canonical_savings()` and a hardcoded `_baseline_model()` precisely because ~20 surfaces each invented their own baseline. Any new claim goes through it or is omitted |

---

## §20–§24 Corrections, negative learning, scope, conflicts, forgetting

| Capability | State | Why |
|---|---|---|
| Correction capture | **Missing** | Table exists, 0 rows, 9 wired writers |
| Anti-patterns | **Missing** | Nothing records what not to do |
| Convention scope | **Missing** | Must be mandatory — a convention without scope is a bug |
| Conflict precedence | **Missing** | See `KNOWLEDGE_MODEL.md`; the brief's ordering needs one change |
| Decay / supersession / model-version awareness | **Missing** | |

---

## §25 Cold start

**Exists by accident, and it is the steady state.** At 13 rows/week a
`(task_type, model, project_area)` cell reaches n=30 in years. The correct
reading of §25 is not "work well until data arrives" but **"assume data never
arrives; treat learning as a bonus."** Any design whose critical path needs
learned outcomes never leaves the fallback branch.

---

## §26 Privacy and locality

| Capability | State | Why |
|---|---|---|
| Local-only storage | **Exists** | Everything under `paths.state_path()` |
| Scope isolation | **Exists** | `resolve_scope_or_none()` returns None rather than guessing |
| Identity scrubbing | **Exists** | Audit output redacts `Path.home()` → `~` |
| **The proposed `.llm-router/knowledge/*.graph` layout** | **Should not build as specified** | A parallel tree is exactly the OKF-SCOPE-04 mistake. Use the existing root and slug convention |
| Test isolation of the knowledge store | **Broken** | `knowledge/projects/` holds 12,448 subdirs, many named after test fixtures |

---

## Summary — what to build, in order of value per unit of work

1. **Capability filter** (§10) — both halves exist, nothing joins them.
2. **Episode record** (§4/§11 substrate) — one schema change; unlocks everything.
3. **Convention store** (§5) — new, but needs 5–20 observations, not thousands.
4. **Blast radius from the import graph** (§2) — one query over an existing table.
5. **Verification service for AGR** (§14) — standalone value to the other repo.

Not built: four graph databases, a vector database, an ML pipeline, runtime
graph mutation, the six-term ECC formula, and any outcome predictor until the
entry condition in `IMPLEMENTATION_PLAN.md` is met.
