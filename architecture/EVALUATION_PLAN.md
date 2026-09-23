# EVALUATION_PLAN.md

Proving the architecture is actually better — or finding that it is not.

**The uncomfortable constraint first:** at **13 episodes/week**, a two-arm A/B
with adequate power is not available. This plan is built around that fact
instead of pretending otherwise. A plan that requires 400 episodes per arm is
not an evaluation plan, it is a wish.

---

## 1. The primary metric, and why the obvious one is wrong

The brief proposes:

```
Verified Successful Completions / Total Execution Cost
```

Right instinct, two problems.

**Problem 1 — the denominator is in the wrong units.** Of the 214 rows with
trusted provenance, cost is **0.0 for all of them**. This fleet is local Ollama
plus subscription Claude. A ratio whose denominator is zero for the entire
measured population is not a metric.

**Problem 2 — a ratio can be improved by shrinking the denominator.** Doing less
work per task improves it, and so does attempting only easy tasks.

### The metric

```
Primary:  VSC-cost  =  Σ(w_t·tokens + w_q·quota + w_l·seconds) over ALL attempts
                       ──────────────────────────────────────────────────────────
                                    episodes reaching verified PASS

Guardrail (must not regress):  verified completion rate = PASS / attempted
```

**Two numbers, always reported together.** The guardrail is what stops token
savings compensating for lower completion quality — the brief's explicit
prohibition. A VSC-cost improvement with a completion-rate regression is a
**failure**, not a trade.

Weights `w_t`, `w_q`, `w_l` are configuration, printed in every report. On this
fleet `w_q` dominates.

---

## 2. Full metric set

| Metric | Definition | Direction | Trustworthy at n=? |
|---|---|---|---|
| **Verified completion rate** | PASS ÷ attempted | ↑ guardrail | ~40 |
| **VSC-cost** | above | ↓ primary | ~40 |
| First-attempt success | PASS with no retry | ↑ | ~40 |
| Tokens per verified success | tokens ÷ PASS episodes | ↓ | ~30 |
| Retry rate | retries ÷ nodes | ↓ | ~30 |
| Human correction rate | episodes with a correction event | ↓ | ~60 |
| Wall-clock per verified success | | ↓ | ~30 |
| Routing regret | ECC(chosen) − ECC(best in hindsight) | ↓ | **~200, defer** |
| Regression rate | later failures traced to an earlier episode | ↓ | **~200, defer** |
| Graph overhead | non-productive nodes ÷ total nodes | ↓ | ~30 |
| Context precision | sent context actually referenced | ↑ | ~30 |
| Context recall | failures attributable to *missing* context | ↑ | ~60 |

**Precision and recall are reported together, always.** Precision alone is
trivially maximised by sending nothing. This project has already been misled by
a one-sided measure: interception looked healthy by command count until it was
measured in bytes, and eligibility turned out not to be interception — 26
commands passed the allowlist, 9 actually intercepted.

---

## 3. How to evaluate with 13 episodes/week

Four methods, in order of strength.

### Method A — replay (strongest, available today)

`scripts/bench_session_replay.py`: 5 real sessions, 115 prompts, currently
**76% drafts / 66% acceptable** (80%/70% over the 105 routable).

Offline, deterministic, repeatable, **no new data required**. Both arms run over
the same prompts.

**Before it judges anything:** run it 3 times and report mean and spread. The
repo's own backlog item N7 says the number is not yet trustworthy. Evaluating
against an unvalidated baseline is how a 4.25-point proxy error happened before.

Limit: replay measures *routing*, not *task completion*. It cannot evaluate the
graph layer at all.

### Method B — a task corpus (must be built)

20–40 real tasks from this repo's own history, each with an **objective**
acceptance check (`cmd`/`lint`/`diff`/`canary` — the existing vocabulary). Run
both arms over the same corpus.

This is the only method that can measure verified completion, and it is the main
new investment in this plan. Both arms must be gradeable by the same check, or
the comparison is between graders rather than systems.

**Contamination rule:** corpus tasks must not be tasks whose conventions were
learned from. Tune on one split, evaluate on another — a proxy split has already
misled this project by 4.25 points.

### Method C — paired within-task comparison

For each task, run the current arm and the new arm on the **same task**, same
starting commit, in separate worktrees (`agentic/worktree.py` exists). Paired
comparison removes task difficulty as a variable and is far more powerful per
sample than an unpaired A/B — which is what makes n≈40 workable.

### Method D — live shadow (weakest, but free)

Run the new pipeline in shadow: compile the graph, select the models, build the
context, **execute nothing**. Compare the intended plan to what actually
happened. Measures agreement, not quality. Useful for catching a filter that
excludes everything, useless for proving benefit.

---

## 4. The arms

| Arm | Description |
|---|---|
| **A0** | Current llm-router, unchanged |
| **A1** | + capability filter (Phase 0.5) |
| **A2** | + graph execution with a hand-written template (Phase 2) |
| **A3** | + conventions (Phase 3) |

**Evaluate each increment separately.** Comparing A0 to A3 tells you the bundle
works and nothing about which part. Given how few samples exist, an ambiguous
bundle result is a wasted quarter.

Expected honest outcomes:

- **A1** should show *no* completion-rate change and a small reduction in failed
  attempts. If A1 changes nothing measurably, the filter's value is safety
  against a rare catastrophic mis-route, not throughput — say that rather than
  inflating it.
- **A2** will likely cost **more** tokens per task (more nodes, more calls) and
  should raise verified completion. **That is the trade to state explicitly.**
  If A2 raises cost without raising verified completion, A2 has failed.
- **A3** should reduce human corrections and prompt length, with completion flat.

---

## 5. Falsification

Each phase has a result that kills it. Written before running, so the outcome
cannot be reinterpreted afterwards.

| Phase | Killed if |
|---|---|
| Capability filter | Shadow mode shows an ineligible model in the chain **< 1% of the time** — the registry is the thing to fix, not the router |
| Graph execution | A2 costs ≥ 2× A0's tokens with verified completion within noise |
| Conventions | Detection over real transcripts cannot surface the known convention, **or** produces > 1 false candidate per true one |
| Outcome learning | Entry condition (≥200 episodes, ≥30 in ≥4 cells) not met — do not start |
| **The whole thesis** | See §7 |

---

## 6. What must not be claimed

| Never | Because |
|---|---|
| A token reduction without a counterfactual run | A baseline nobody ran is not a measurement. Print `NOT MEASURED` |
| A rate without n and window | Four rates in this project's history were noise, all four briefly reported as real |
| An improvement below the "trustworthy at n" column | Say "too few to tell" |
| A VSC-cost win alongside a completion regression | The brief forbids it; the guardrail exists to catch it |
| A saving measured on placeholder-cost rows | 87% of current rows carry a flat $0.01 |

---

## 7. The experiment that could falsify the entire thesis

> **Run the §28 scenario with the graph, and with a single strong model given
> the same prompt and the same acceptance checks. Compare verified completion
> and total tokens.**

If one Opus call with "implement this, run the tests, fix what fails, audit it"
achieves the same verified completion for fewer total tokens than an 8-node
graph with per-node routing, **then the orchestration layer is overhead** and
the right architecture is a better prompt plus the acceptance checks.

This is cheap, needs no learning, and can be run as soon as Phase 2 exists —
which is exactly why it should be run **then**, before Phases 3 and 4 are built
on the assumption that it passes.

The honest prior: the graph should win on **hard, multi-file tasks** and lose on
small ones. If that is the result, the finding is not "graphs work" but *"graphs
work above a complexity threshold"* — and that threshold becomes the convention
trigger, which is a better outcome than a yes.
