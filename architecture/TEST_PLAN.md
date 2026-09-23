# TEST_PLAN.md

The suite is ~9,550 tests. This plan adds to it under the repo's existing
discipline, which is stricter than usual and exists because of specific
incidents.

---

## 0. Rules every new test obeys

| Rule | The incident |
|---|---|
| **Run under `HOME=$(mktemp -d)`** | Three tests passed only on a machine with prior state; CI was red across two releases while the local suite was green |
| **Red-check with the NARROWEST mutation**, needle left in a comment | A-10: 23 tests were red-checked by reverting whole files, which removed the asserted string along with the call. The evasion was reproduced — put the phrase in a comment, delete the call, 23 tests passed |
| **Assert the premise, not only the conclusion** | R14's rotation test wrote 3,200 records that never crossed the threshold — three "no loss" runs passed against broken code |
| **Assert the REASON, not just the boolean** | A test passed because it handed `guard_command` a string where it takes argv, so everything was refused on `'g'` |
| **Prefer AST assertions over source text** | Comments are not in the AST. `tests/_ast_assert.py` exists for this |
| **A check that finds nothing must be proven on a known positive** | A near-duplicate stage reported "0 collapses" while comparing raw whitespace splits — nothing could ever match |
| **Subprocess isolation needs `env`, not `monkeypatch`** | The child reads its own environment; two separate incidents this week |

---

## 1. Unit

### Capability filter
- Vision task + text-only model → **excluded**, and the exclusion reason is asserted.
- 200k-context task + 8k model → excluded.
- **Model with no capability record → excluded** (fail-closed). This is the test that matters; assert the *reason* string, not just absence.
- Anti-vacuity: a permissive task excludes nobody, so the filter is not simply refusing everything.
- Red-check: invert `known(m)` to default-eligible → the fail-closed test goes red.

### ECC
- Failed episodes appear in **both** numerator and denominator.
- A cheap strategy with a 50% failure rate scores worse than an expensive one at 100%.
- n < 20 → returns "too few to tell", **not a number**.
- Weights are read from config and appear in the output.

### Convention precedence
- Explicit instruction beats a hard convention.
- Hard convention beats a learned one.
- Narrower scope beats wider at the same level.
- Confidence is the **last** tiebreak — a confident general rule loses to a narrow explicit one.
- Red-check: swap safety above explicit instruction → the "skip the audit, it's a typo fix" test goes red.

### Knowledge model
- `verdict='UNCERTAIN'` never aggregates as PASS or FAIL.
- An aggregate over rows with `cost_measured=0` reports `unmeasured_rows`.
- A new `model_version` starts at n=0, not inheriting its predecessor.
- Contradictory experiences are both retained; neither is averaged away.

---

## 2. Graph

- A generated graph passes `validate_graph`.
- **An unconditional back-edge is rejected** (AGR's lint) — assert we never emit one.
- Subgraph nesting beyond depth 3 raises `SubgraphError`; the compiler refuses before that.
- `max_steps` terminates a loop that never converges — assert on a graph designed not to converge, not on one that happens to.
- `validate_graph` failure → **falls back to the template**, never to nothing.
- Expansion with one layer → **no expansion** (refusal is correct behaviour).

---

## 3. Routing

- Node routing is independent of topology: same graph, different candidate sets → different models, same path.
- `n < 30` → static order, and the report says "not ranked".
- Exploration floor fires even when one model dominates.
- `candidates_json` records rejected candidates with reasons.
- **Replay gate**: `bench_session_replay.py` 3 runs, mean and spread, before and after any routing change.

---

## 4. Convention learning

- 3 similar instructions in **1 session** → **not** a candidate (distinct-session rule).
- 3 across 2 sessions → candidate.
- Candidate never auto-promotes past `suggested`.
- 2 consecutive overrides → demoted.
- `confident == False` → **never auto-applies**.
- A graph containing a destructive action → never auto-applies; routes to `kind: human`.
- **The honest gate:** run detection over real transcripts; it surfaces the known convention with ≤1 false candidate. If it cannot, the feature does not work.
- **Observer effect:** an auto-applied convention that succeeds does **not** raise its own confidence.

---

## 5. Token efficiency

- Budget is **enforced**: an over-budget node drops items and **records what it dropped**.
- `verifier` and `audit` nodes do **not** receive the implementer's reasoning (§15 independence, enforced in the context builder).
- An artifact built once is reused by a second node in the same episode; assert tokens saved.
- **Expansion that does not reduce per-node context fails** — the honest test of §8.
- Precision and recall reported together; a test asserts recall is present whenever precision is.

---

## 6. Failure, retry, escalation

- Verifier FAIL → guarded edge fires; the failure class routes correctly.
- **UNCERTAIN escalates the verification, not the model.**
- Bounded attempts: the ladder terminates as COMPLETE or a *surfaced* failure — never silently.
- A flaky check (`reproducible()`) re-runs rather than escalating on noise.
- Every escalation writes an `episode_event`.

---

## 7. Cold start

The most important section, because it is the **steady state**, not a phase.

- **Zero episodes**: the full §28 scenario runs end to end. No crash, no empty ranking, no division by zero.
- Zero conventions → the unopinionated default path.
- Zero capability records → **everything excluded**, and the system says so loudly rather than routing to an arbitrary model.
- One episode → no ranking (n < 30), and the report says why.
- `0 of 0` reads **Unknown**, never a healthy 0%.

---

## 8. Regression, versioning, conflicts

- Model version change resets that cell to n=0.
- Repo version / branch change marks entities stale; stale entities are excluded from retrieval and flagged.
- Two conflicting conventions resolve deterministically and identically across runs.
- A convention and an explicit instruction conflict → instruction wins, override recorded.
- AGR version pin: an upgrade triggers the replay gate (a test asserts the pin is exact, not a range).

---

## 9. Anti-vacuity ratchets

Following `test_lint_unknown_as_number.py` (BASELINE = 113), which ratchets
downward and fails when the count *drops* too — a baseline with slack lets the
next regression land green.

| Ratchet | Guards |
|---|---|
| Counters with no reader | R12 — must stay 0 |
| Source-text assertions | `test_r13_no_source_text_assertions.py`, existing |
| `or 0` reaching a comparison or mean | 113, existing |
| **Nodes with no declared context slots** | New — an undeclared slot is an unbounded prompt |
| **Conventions with no scope** | New — must stay 0; a convention without scope is invalid |

---

## 10. What this plan deliberately does not test

- **Outcome prediction accuracy.** No labels exist. A test asserting the predictor is accurate would assert against fabricated ground truth.
- **That savings are real.** Until a counterfactual run exists, there is nothing to compare; the report prints `NOT MEASURED` and a test asserts *that*.
- **AGR internals.** 501 tests there already. We test the contract, not their scheduler.
