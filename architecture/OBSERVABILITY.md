# OBSERVABILITY.md

Every execution must be explainable. The hard part is not rendering — it is
**refusing to print numbers that were not measured**.

---

## 1. The report

`llm-router explain <episode_id>`, and printed inline after an auto-applied
convention runs.

```
Episode ep_8f21 · "Implement the plan." · llm-router · 2026-09-23 14:02

WORKFLOW  implementation_with_verification_loop        [auto-applied]
  why     task_type=implementation, complexity=moderate, repo=llm-router
  evidence 17 occurrences across 11 distinct sessions, confidence 0.86
  rejected quick_fix_no_audit — scope: max_diff_lines 50 < 300
  undo    llm-router convention disable implementation_with_verification_loop

GRAPH     8 nodes, 2 expanded (implement → api, service), 4 guarded back-edges
          validate_graph OK · max_steps 60 · terminated at step 23

NODES
  validate_plan   ollama/qwen3-coder:30b   1.2k tok    4.1s   ok
      why  capability filter left 4 candidates; no cell ≥ n=30, static order
  architecture    anthropic/claude-opus    8.4k tok   22.0s   ok
      why  requires: reasoning, 32k context. 2 of 6 candidates eligible
  implement.api   codex/gpt-5.5           12.1k tok   41.3s   ok
  implement.svc   codex/gpt-5.5            9.8k tok   33.7s   ok
  tests           local (no model)             0 tok   18.2s   PASS
      check  cmd: pytest -q tests/test_api.py  exit 0  deterministic
  audit           anthropic/claude-opus    6.2k tok   19.4s   2 findings
      why  independent: implementer was codex/gpt-5.5 (risk=medium)
  fix             codex/gpt-5.5            3.1k tok   11.0s   ok
  tests (retry)   local                        0 tok   17.9s   PASS
  docs            ollama/qwen3.5           2.0k tok    8.8s   ok

CONTEXT
  artifact arch_7c21 (8.4k tok) built once, reused by implement.api, implement.svc
  dropped  3 items at implement.svc — over budget (16k)
           [service/legacy_adapter.py, 2 older failures]

TOTALS
  models 4 · calls 9 · retries 1 · tokens 42.8k
  wall clock 3m 16s · quota: 2 premium calls
  cost $0.00 measured on 9 of 9 calls (local + subscription)
  token baseline: NOT MEASURED — no counterfactual run

VERIFICATION  PASS (2 objective checks, both deterministic)

LEARNING
  convention implementation_with_verification_loop: no change
      (an auto-application is not evidence for its own selection)
  capability  codex/gpt-5.5 · implement · llm-router: n=7 (below 30, not ranked)
```

---

## 2. Six rules the report obeys

### 1. A baseline nobody ran is not a measurement

The brief's §19 example prints *"Estimated naive baseline: 240K · Token
reduction: 65%"*. **This report prints `NOT MEASURED` instead**, unless a
counterfactual actually ran.

`savings.py` has a `SURFACES` registry and a hardcoded `_baseline_model()`
precisely because ~20 user-facing surfaces each invented their own baseline.
Any reduction figure goes through `canonical_savings()`, carries its n, or is
omitted. Omitting it is always available and is often right.

### 2. Measured and estimated cost never blend

`cost $0.00 measured on 9 of 9 calls` — the denominator travels with the figure.
agenticgraphs already does this (`usd_measured: bool`); llm-router should match
it. Today 1,387 of 1,601 rows carry a flat $0.01 placeholder that no surface
distinguishes from a real price.

### 3. "Not ranked" is printed, not hidden

`n=7 (below 30, not ranked)` says the static order was used. A report that
silently omits the reason implies a measurement that did not happen — and with
13 episodes/week this is the *common* case, so hiding it would misrepresent
almost every run.

### 4. The rejected alternatives are shown

Both the rejected convention and the filtered-out models. This is the
counterfactual; without it nobody can ask why the other option did not fire, and
§29's bubble becomes undiagnosable after the fact.

### 5. Dropped context is named

`dropped 3 items at implement.svc — over budget` with the list. Otherwise a
failure caused by a budget is indistinguishable from a model failure.

### 6. Learning updates are shown, including *no change*

`no change (an auto-application is not evidence for its own selection)` — the
observer-effect rule from `LEARNING_SYSTEM.md` §8, made visible so that a
confidence that never moves is understood rather than assumed broken.

---

## 3. Surfaces

| Surface | Content |
|---|---|
| `llm-router explain <id>` | The full report above |
| `llm-router explain --last` | Most recent episode |
| Inline, after auto-apply | Workflow block + undo line only |
| `llm-router conventions` | List with scope, kind, confidence, occurrences, overrides |
| `llm-router doctor` | Counter registry — every counter has a reader (R12) |
| JSON | `--json` on all of the above; the episode row is the source |

**Everything renders from `episode`/`episode_node`/`episode_event`.** No surface
recomputes its own version of a number — that is how ~20 savings surfaces
drifted apart, and the registry exists to stop it happening again.

---

## 4. Counter registry

New counters join `counter_registry.REGISTRY`, which `doctor` renders. The R12
rule holds: **a counter with no reader is not instrumentation.**

Proposed additions, each with a reader and an alarming condition on the *share*:

| Counter | Makes visible |
|---|---|
| `graph_runs_terminated_by_step_cap` | Workflows hitting `max_steps` — a guard that fires often is a graph that does not converge |
| `capability_filter_exclusions` | The filter doing something. **Zero means it is not needed, or broken** |
| `convention_overrides` | Auto-apply annoying the user (§29/§6) |
| `context_budget_drops` | Budgets set too tight |
| `verifier_uncertain` | Checks that cannot decide — the signal that verification needs strengthening |

`0 of 0` reads **Unknown**, never a clean 0% — the denominator-disappearance
rule already enforced elsewhere in the registry.

---

## 5. What must never appear

| Never | Because |
|---|---|
| A savings figure without n and window | Four rates in this project's history were noise and all four were briefly reported as real |
| "Estimated baseline" presented as measured | §1 above |
| A confidence without its occurrence count | 0.86 from 3 observations and from 300 are different claims |
| A pass rate over rows with placeholder cost, unqualified | 87% of current rows are placeholders |
| An aggregate whose denominator is zero, rendered as a healthy 0 | The denominator-disappearance defect, hit three times in this repo |
