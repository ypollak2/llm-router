# Remediation plan

v14.1.0 · 2026-09-21. **Nothing in this document has been implemented.** Per the
audit brief, discovery does not fix. This is the sequenced plan for a later
decision.

Ordered by *what unblocks what*, not by severity alone. Several CRITICALs are
deliberately not first, because fixing them before their prerequisite would
produce a corrected number over an uncorrectable population.

---

## The sequencing principle

> Contaminated history cannot be cleaned. It can only be superseded.

The fixture rows in `usage.db` are not separable after the fact — 1,813 of them
wear real model names. So the order is always: **stamp provenance at write time
→ start a clean window → only then recompute and republish.** Any fix that
recomputes first produces a more precise wrong answer.

---

## Phase 0 — Stop the bleeding (hours, no design needed)

These are containment, not correctness. Each is small, independent, and safe.

| # | Action | Finding |
|---|---|---|
| 0.1 | **Withdraw the published savings figure and the README "105/76%/72%" triple.** Shipping without the claim is always available | C-02, H-02 |
| 0.2 | **Raise the semantic cache threshold or disable it by default**, and add a per-request bypass plus a single-entry eviction tool | C-03 |
| 0.3 | **`chmod 0600` on `intercepts.jsonl` and `trace.jsonl`** via the private opener that already exists in `auto-route.py`; add both to `commands/gc.py` | C-04 |
| 0.4 | **Delete `error_sanitization.py`.** It has 0 callers, misses five secret classes, and its name invites the wiring that reintroduces the leak | M-07 |
| 0.5 | **Add `rich` to install requirements** so `llm-router status` does not crash on a clean install | M-11 |
| 0.6 | **Move the 4.8 GB RouterArena checkout out of `~/.llm-router`** | M-09 |

0.1 is listed first on purpose. Every hour the number stays published is an hour
someone may act on it.

---

## Phase 1 — Make the instrument able to record reality (the gate)

Nothing downstream is worth doing before this lands.

### 1.1 · Let the quality ledger record failure — **C-01**
Call `record_route()` from the terminal-failure path and from the cache-hit path,
not only from `_finalize_successful_route`. Add `route_outcome` with an explicit
enum (`success` / `failed` / `cache_hit`) rather than a boolean that can only be
true.

**Acceptance:** a forced provider failure produces a row with
`route_succeeded=False`. A test asserts the count of representable outcomes is 3,
not 1.

**Guard against the obvious regression:** a test that fails if any terminal
branch can return without writing exactly one ledger row — the same invariant
this repo already enforces on `auto-route-debug.log`, applied one layer up.

### 1.2 · Count the losses on the measurement ledger — **H-09**
Apply `failopen.record` at `router.py:2043`, exactly as the sibling at
`router.py:1821` already does after the documented "66 dropped events, no error,
no log, no counter" incident.

### 1.3 · Write provenance into `usage.db` at insert time — **C-02**
Populate `is_simulated` in the single `INSERT INTO usage`, using
`detect_synthetic()`. Then **stop filtering by model name** — delete
`_is_test_model()` from the savings path rather than improving it.

**Acceptance:** a test-suite run followed by a savings query returns $0.00, not a
filtered approximation.

### 1.4 · Fix the `classification_method` key mismatch — **H-04**
One-line change; the value is already computed. Add a test asserting the field is
non-empty on a fresh row — the CLAUDE.md rule about a check that finds nothing
applies directly.

### 1.5 · Make `summarize()` use `is_evaluable()` — **H-01**
And give `attribution.py` its consumers, or delete it. Two modules each claiming
to be the single source of truth is worse than one that is honestly ad hoc.

**End of Phase 1 test:** enable accumulation, run the full test suite, and
confirm the reported savings is $0.00 and the reported success rate is undefined
rather than 100%.

---

## Phase 2 — Start a clean measurement window

Only reachable after Phase 1.

| # | Action |
|---|---|
| 2.1 | Mark all pre-Phase-1 rows as provenance-unknown. Do **not** delete — the ledger is append-only by design |
| 2.2 | Make `is_evaluable()`-style fail-closed semantics the default for every reader: unknown provenance is excluded, not assumed real |
| 2.3 | Collect a real window. Do not publish a number before the window has enough real prompts to clear the repo's own ~50-prompt floor |
| 2.4 | Republish savings and quality figures **with N, window and source file**, per the house rule |

---

## Phase 3 — Concurrency correctness

Each of these has a correct in-repo reference implementation. The work is
adoption, not invention.

| # | Action | Reference | Finding |
|---|---|---|---|
| 3.1 | Atomic write for `quota_tracker` (temp file + rename) — **10 hooks consume it at a 32–38% read failure rate** | `budget_backend.py` | H-06 |
| 3.2 | Make `Pool.admit()` increment safely; stop constructing a fresh `Pool()` per call in `accumulate.py` | — | H-07 |
| 3.3 | Set `busy_timeout` **before** WAL in `result_cache` | this project's own documented fix | M-06 |
| 3.4 | Stop swallowing `usage.db` migration failures | — | M-05 |

---

## Phase 4 — Ground Truth

**Do not start before Phase 2 produces a clean window.** Building more pipeline
on an instrument that cannot record failure is the mistake this audit exists to
prevent.

Then, a fork in the road that needs a decision rather than a fix:

| Option | Consequence |
|---|---|
| **A — Implement replay** (worktree checkout + patch apply in `run_matrix`) | Unlocks EDIT tasks, which the eligibility gate is already tuned to admit. Larger, and the correct end state |
| **B — Narrow the eligibility gate** to admit only what the harness can grade today | Small. Stops the pool filling with candidates that can never be labelled. Reversible |

**Recommended: B first, A later.** B is cheap and immediately stops a known
silent failure; A is the real answer but should not block a clean window.

Also in this phase:

| # | Action | Finding |
|---|---|---|
| 4.1 | Fix `completeness()` so it cannot disagree with `reconstructable` on the same object. Remove the redundant check in `accumulate.py` that currently masks it — correct-by-luck is a latent break | M-01 |
| 4.2 | Extend `detect_synthetic()` to consult the sandbox-path and fixture-session detectors in `sources.py`; make `bench_*.py` set the flag | M-02 |
| 4.3 | Give `discriminate.policy_score` real verification-type filtering **before** anyone extends `generate_snippet()` for judges. The current safety is accidental | GT audit |
| 4.4 | Replace the `actor == "assistant"` approval gate with real identity | H-10 |
| 4.5 | Put a discrimination floor on the snippet validation path, as the pytest path already has | GT audit |

---

## Phase 5 — Surface honesty

| # | Action | Finding |
|---|---|---|
| 5.1 | Restore tool definitions and real `finish_reason` in the gateway, or document plainly that function calling is unsupported | H-03 |
| 5.2 | Add per-request auth to the gateway and `route_server`, copying `commands/sse.py` | M-08 |
| 5.3 | Fix or unship `control_plane/api` — and unquarantine the test that was hiding the `ImportError` | M-10 |
| 5.4 | Remove `health` and `gain` from the docs, or implement them. Same for the two rejected host integrations | M-12 |
| 5.5 | Delete the dead clusters: `derive_trace_id` from `__all__`, the 9 orphan `cost.py` reporters, the 5 tested-but-unwired modules, `context_signal.py`'s decoy docstring | L-01→L-06 |
| 5.6 | Count the bandit reorder's silent failures | L-07 |
| 5.7 | Remove the tautology at `tests/test_gateway_service.py:53`; make the default-excluded marker set (`slow`, `requires_ollama`, `requires_api_keys`, `requires_codex`) visible in CI output | L-12, L-13 |

---

## What not to do

- **Do not improve `_is_test_model()`.** Name matching cannot see 1,813 rows
  wearing real names. Improving it produces confident wrongness — it already
  moves the figure *away* from truth by $4.47.
- **Do not rewrite or delete the append-only ledgers.** Supersede with
  provenance; never edit history.
- **Do not build more Ground Truth pipeline before C-01.**
- **Do not recompute and republish any number before Phase 2.** A corrected
  computation over a contaminated population is a worse artefact than no number,
  because it looks audited.

---

## Effort and risk

| Phase | Size | Risk if skipped |
|---|---|---|
| 0 — containment | Hours | Wrong answers served (C-03); secrets accumulating world-readable (C-04); a published false figure acted on (C-02) |
| 1 — instrument | Days | Everything downstream stays unmeasurable |
| 2 — clean window | Days of elapsed time, little work | No trustworthy number can ever be published |
| 3 — concurrency | Days | Silent data loss continues at a known rate |
| 4 — Ground Truth | Weeks (A) or days (B) | The pool fills with ungradable candidates |
| 5 — surface honesty | Days | Docs keep promising a surface that does not ship |

---

## The one-line framing for whoever picks this up

Most of this plan is not new engineering. It is **finishing the adoption of fixes
this repository already wrote**: `failopen.record` exists but guards the wrong
ledger; `budget_backend`'s atomic write exists but `quota_tracker` does not use
it; the 0600 opener exists but `intercepts.jsonl` does not use it;
`is_evaluable()` exists but `summarize()` does not call it; `attribution.py`
exists and nothing calls it at all.

The codebase already knows how to do almost every one of these things correctly,
once.
