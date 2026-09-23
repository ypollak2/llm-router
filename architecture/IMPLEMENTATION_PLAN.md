# IMPLEMENTATION_PLAN.md

Incremental phases. **Every phase is independently valuable** — if the project
stops after any one of them, what shipped still works. No giant rewrite.

Every gate can fail. A gate that always passes is not a gate.

**Standing baseline for anything that changes routing:**
`scripts/bench_session_replay.py`, 5 real sessions — 76% drafts / 66% acceptable
over 115 prompts. Per the repo's own item N7, run it **3 times and report the
mean and spread** before trusting it to judge anything.

**Standing rule:** every suite run is under `HOME=$(mktemp -d)`. Three tests
passed only on a machine with prior state and left CI red across two releases.

---

## Phase 0 — make the substrate honest

Nothing downstream is measurable without this. No new features.

| # | Work | Files | Gate | Risk / rollback |
|---|---|---|---|---|
| 0.1 | Write `docs/agentic-router.md`, the MGEE spec four docstrings cite and which does not exist | new | The termination argument is readable and matches `engine.py` | None. Doc only |
| 0.2 | **Episode schema + writers.** `episode`, `episode_node`, `episode_event`; `parent_task_id`/`node_id` on routed calls | `cost.py`, `model_tracking.py`, new `episodes.py` | A multi-node run produces one episode with n linked nodes; a single call produces an episode of 1 | Additive tables; rollback = stop writing. **Risk: `cost.py` is 4,287 lines with a migration path that already fail-opens 4× on a fresh DB** |
| 0.3 | Populate `subject`, or remove it from the bandit key | `router.py`, `telemetry.py` | Either non-NULL on new rows, or the bandit no longer keys on a column that is NULL in 1601/1601 | Behaviour change → replay gate |
| 0.4 | Make `corrections` receive rows from its 9 wired writers | `retrospective.py`, `cost.py`, hooks | A rejected/redone route produces a row | Additive |
| 0.5 | Decide `judge_score`: wire it or delete the column and the claim | `judge.py`, `cost.py` | Either non-zero rows, or `judge.py`'s ranking path is deleted | Deleting is safer; P2 escalation currently gates on it |
| 0.6 | Clean the 298 MB OKF store; fix test isolation | `okf.py`, conftest | `knowledge/projects/` holds no test-fixture dirs; a suite run adds none | Back up before deleting |
| 0.7 | Persist P2 escalation events | `router.py` | Escalations appear in `episode_event`, or the "evidence-based escalation" claim is withdrawn | Additive |
| 0.8 | Delete `chain_builder.py`, `provider_registry.py` | those files | Suite green; no production importer existed | `git revert` |

**Phase gate:** replay baseline unchanged within noise (3 runs, mean + spread).

---

## Phase 0.5 — the capability filter  ← **START HERE** (decided)

Independent of everything else. Both halves exist; nothing joins them.

| # | Work | Files | Gate |
|---|---|---|---|
| 0.5a | Run `detect_capabilities()` in **shadow** over the replay corpus | `capabilities.py`, new script | A report: how often the live chain contains a model that cannot serve the task. **This number justifies or kills 0.5b** |
| 0.5b | Wire it as a real `filter()` behind the existing env flag | `router.py:_build_and_filter_chain` | A vision task never lists a text-only model; a 200k-context task never lists an 8k model |
| 0.5c | Give `model_registry`'s `context_window` and capability tuple a reader | `model_registry.py` | R12: data nothing reads is not data |

**Fail-closed, non-negotiable:** a model whose capabilities are *unknown* is not
eligible. Six prior instances of unknown-as-favourable; this would be the seventh.

**Anti-vacuity:** if 0.5a reports zero ineligible models ever appear, **stop** —
the registry data is the thing to fix, not the filter.

**Risk:** `_build_and_filter_chain` is ~900 lines of ordered list mutations with
hand-documented dependencies ("must run before injection", "applied LAST").
Several past bugs came from stages inserted without respecting that order. The
filter goes in at a slot chosen by reading those comments, and the replay gate
runs before and after.

**Rollback:** the env flag already exists; flip it off.

---

## Phase 1 — drive agenticgraphs in-process

The HTTP route is **deferred, not cancelled**.

| # | Work | Gate |
|---|---|---|
| 1.0 | **Verify AGR accepts an injected runner**, not only a base URL | If it does not, in-process collapses into the HTTP path and blockers B1/B2 return. **This is a go/no-go for the phase** |
| 1.1 | `agenticgraphs` as a pinned dependency + adapter building a graph dict and calling `run_graph()` | A hand-written graph runs end to end from llm-router, no HTTP |
| 1.2 | Route each `kind: agent` node through llm-router instead of `AGR_LLM_MODEL` | One run uses ≥2 models; the report says which node got which and why |
| 1.3 | Expose `acceptance.py`'s vocabulary to AGR `verifier` nodes | A verifier node runs a real `cmd` check and **fails on a real failure** |
| 1.4 | Map `RunReport` (`trace`, `frames`, `tool_calls`, `usage`) onto the episode schema | One `run_graph()` produces one episode with n linked nodes |

**1.3 is worth doing even if the rest is dropped** — it upgrades 61 of 83 AGR
graphs from self-report to executable verification, standalone value to that repo.

**Risks:** version coupling (pin AGR exactly; an upgrade re-runs the replay
baseline); AGR's `fan_out` is sequential, so a parallel-looking graph may not be;
AGR's abilities `edit_files`/`run_suite`/`rollback`/`execute_step` are
**declared but unbound** — do not build on them without checking.

**Rollback:** the adapter is additive; existing routing paths untouched.

---

## Phase 2 — static workflow templates, zero learning

| # | Work | Gate |
|---|---|---|
| 2.1 | Hand-write `implementation_with_verification_loop.yaml` | Passes `validate_graph`; every back-edge guarded; terminates |
| 2.2 | Task Graph Compiler: analyze → convention(hand-written) → template → `validate_graph` → run | `validate_graph` failure falls back to the template, never to nothing |
| 2.3 | Per-node context budget + declared slots | An `implement` node does not receive the whole repo; budget **enforced**, and drops recorded |
| 2.4 | The observability report | Renders entirely from the episode tables; prints `NOT MEASURED` where it is |

**After Phase 2, §28 works.** "Implement the plan" builds and runs the right
graph, from a hand-written template with **no learning at all**. Everything
after is improvement, not function.

**Risk:** the compiler becomes a new way to fail. Mitigated by 2.2's fallback.

---

## Phase 3 — conventions

| # | Work | Gate |
|---|---|---|
| 3.1 | Convention store, scope, precedence | Two conflicting conventions resolve deterministically **and the resolution is explained** |
| 3.2 | Candidate detection from repeated instructions | **Replay real session history: it surfaces the known convention with ≤1 false candidate.** If not, the feature does not work |
| 3.3 | Suggest → accept → demote, with auto-apply above threshold | Nothing reaches `accepted` without an explicit act; 2 overrides demote; the undo command works and is printed |
| 3.4 | Destructive carve-out | A graph containing a migration/release/force-push **never** auto-applies; routes to `kind: human` |

**3.2's gate is the honest one** and can be run before building 3.3.

**Risk:** annoyance. Mitigated by the undo, by overrides counting as evidence,
and by `confident == False` blocking auto-apply — which is **49.8% of real
prompts**.

**Rollback:** delete `conventions.yaml`; the system falls back to Phase 2.

---

## Phase 4 — outcome learning

**Entry condition, checked before any work starts:**

> ≥ 200 episodes with a real `verdict`, over ≥ 3 models, with ≥ 30 in each of
> ≥ 4 `(node_kind, model)` cells.

**If unmet, do not start.** At 13 episodes/week it will not be met for a long
time. That is a finding, not a failure — Phases 0–3 deliver §28 without it.

| # | Work | Gate |
|---|---|---|
| 4.1 | Beta-Bernoulli per cell, Wilson lower bound | A model must earn rank with volume; 3/3 does not read as 100% |
| 4.2 | Mandatory exploration floor, not configurable to zero | Exploration occurs even when one model dominates |
| 4.3 | Counterfactual recording already in place from 0.2 | `candidates_json` populated on every node |

---

## Cross-cutting

**Data migration:** all new tables are additive; no existing column changes
meaning. The one risky edit is 0.3 (`subject`), which either starts being
written or stops being a key — both are behaviour changes and both go through
the replay gate.

**Sequencing:**

```
0.5 (independent, start now)
0.1 → 0.2 → {0.3, 0.4, 0.5, 0.6, 0.7, 0.8}
0.2 → 1.0 → 1.1 → {1.2, 1.3, 1.4}
1.x → 2.1 → 2.2 → {2.3, 2.4}
2.x → 3.1 → 3.2 → {3.3, 3.4}
0.2 + volume → 4.x
```

**Files most at risk**, all already flagged as technical debt: `router.py`
(~5,300 lines), `cost.py` (4,287), `hooks/auto-route.py` (4,653). Item N17 in
the existing backlog proposes splitting them; this plan does **not** depend on
that, but every edit into `_build_and_filter_chain` should be read against its
ordering comments first.

**The duplicated classifier is a dependency, not a tidy-up.** `classify.py`
`_SIGNALS` and `hooks/auto-route.py` `SIGNALS` are hand-kept in sync ("Option
B"), and the hook does **not** call `classify_signals()`. Every new pre-routing
signal must be built twice or the hook — the surface the user touches most —
does not get it. Existing backlog item N8; promote it.
