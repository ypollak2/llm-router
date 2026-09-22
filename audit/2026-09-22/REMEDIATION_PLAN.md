# Remediation plan — 2026-09-22

**Nothing here is implemented.** Discovery only, per the audit brief.

Ordered by what unblocks what. The sequencing principle from the previous round
still holds — *contaminated history cannot be cleaned, only superseded* — with one
added this round:

> **Fix the call site, not the definition. Then write the test against the call
> site.** Every P0 below exists because the previous round did the opposite.

---

## P0 — wrong results, security exposure, or an invalidated core claim

### P0-1 · Make the test signal trustworthy (T-01)
**Objective:** `uv run pytest` reporting 0 means 0.
1. Fix `test_h08_…::test_replay_detection_looks_for_a_real_runner` to restore the
   package attribute, not only `sys.modules[...]`.
2. Fix or update the 8 `test_groundtruth_accumulation.py` tests the H-08 change
   invalidated — they encode the pre-H-08 contract.
3. Make `replay_available()` resilient to module-cache mutation (T-13).
4. Add a session-scoped autouse fixture that snapshots and asserts `sys.modules`
   and key package attributes are unchanged after each test.
**Validation:** the suite must be green under `-p randomly`, under `-x`, and with
the two files in either order. **Do this first** — every other validation below
depends on the suite meaning something.

### P0-2 · Fix scrubber adoption, and test adoption rather than coverage (T-04, S-01, S-02)
**Objective:** no content-persisting path scrubs with a private copy.
1. `library/store.scrub_secrets` and `hooks/agent-route._scrub_agent_prompt`
   delegate to `secret_scrubber.scrub_text`, or are deleted in favour of it.
2. `library/store.write_doc` uses `paths.private_opener` (currently 0644).
3. Correct the false "kept in sync" comment in `hooks/auto-route.py` (S-08) — or
   generate that fallback from the canonical table so it cannot drift.
4. **Rewrite `test_m07_no_second_scrubber.py` to call every rival function** and
   assert it redacts what canonical redacts. The current test asserts only
   canonical's own coverage, which is why this shipped.
**Validation:** a secret battery driven through each real writer, asserting 0
survivals and 0600 at creation.

### P0-3 · Withdraw or fix `llm-router demo` (T-02)
**Objective:** the showcase stops printing arithmetic that cannot be true.
Compute the baseline from each row's real premium-model cost, or remove the
comparison. Never print a negative saving as "cheaper".
**Validation:** a test over `commands/demo.py` asserting savings sign and
magnitude for a batch containing a premium call. There is currently no test file
for this module at all.

### P0-4 · Classify the ask, not the transcript (T-03)
**Objective:** a trivial turn behind a large system prompt routes as trivial.
Pass `system` separately to `route_and_call` rather than concatenating, and
classify the latest user turn. If total context must influence the tier, make it
an explicit input rather than a side effect of string length.
**Validation:** a test constructing a multi-role message list and asserting the
complexity of `"hi"` is unchanged by 2KB of system boilerplate.

### P0-5 · Extend provenance to the five unfiltered money surfaces (T-05)
**Objective:** one definition of "does this row count", used by every reader.
1. `get_team_savings` — add the filter (this is the one that broadcasts).
2. `claude_usage` / `codex_usage` / `gemini_usage` / `savings_stats` — add the
   column and stamp it at write, using the same `detect_synthetic()`.
3. `get_quality_report`, `get_routing_savings_vs_sonnet`, `get_router_efficiency`
   — filter, or route through `attribution.py`, which already exists for this.
4. Delete or correct the false comment at `cost.py:622`.
**Validation:** one synthetic row, then assert **every** money surface reports
zero. A parametrised test over the list of surfaces, so a new surface must be
added to it.

### P0-6 · Stop shipping a function that always raises (T-11)
Exclude `budget_lineage_reconciliation` from the wheel, or remove its
`control_plane.audit` dependency. Then extend `test_shipped_modules_import.py` to
**call public functions from the built wheel**, not merely import modules — and
remove the `conftest.py` auto-skip that hides the one test which would have
caught it.

---

## P1 — material reliability and evaluation problems

### P1-1 · Close the two unrecorded terminals (T-08)
Idempotency dedupe and the exhaustion floor must write a quality-ledger row.
**Validation:** assert row counts per terminal state, not response content.

### P1-2 · Mark degraded answers as degraded (T-10)
The exhaustion floor must set a field on `LLMResponse`, render differently, and
**not** feed `success=True` back to the bandit for a response the router rejected.

### P1-3 · Fix the bandit reward (T-09)
`success_rate / max(avg_cost, 1e-9)` gives a free model ~1e8× any paid model.
Use a bounded cost-quality tradeoff, and do not let a coarse
"non-empty and not a deferral" signal stand in for quality.
**Validation:** a paid model at 0.99 success must be able to outrank a free model
at 0.50.

### P1-4 · Give the fail-open counter a reader (T-07)
Surface `snapshot()` in `doctor` and `status`. Raise the fallback log above
`DEBUG`. Consider a second channel that does not depend on the store it is
reporting about.

### P1-5 · Bridge or retire the Ground Truth verifier pipeline (T-06)
Either give pool candidates and frozen tasks one ID scheme plus a converter from
an ACTIVE `VerifierRecord` into a `dataset.Task`, **or** delete
`propose`/`mutants`/`verifier_registry` and say plainly that labelling is manual.
Shipping a rigorous pipeline that cannot contribute is worse than either.

### P1-6 · Give `run_verifier` a `cwd`, or gate external evidence too
Replay is absent for repo state (correctly gated) and absent-but-ungated for
external evidence. `run_verifier` currently inherits the caller's cwd, so
`read()`/`pytest_passes()` evaluate today's tree.

### P1-7 · `route_server` auth parity (S-03)
Apply the gateway's opt-in token. Same class of exposure, currently less
mitigated — and the file's own comment already acknowledges the gap.

### P1-8 · Honour `LLM_ROUTER_HOME` in host-integration commands (S-04)
`install`, `update`, `dev-refresh` write to the real `~/.claude/` regardless.
This is a test-isolation bug with real-world blast radius.

---

## P2 — architecture and maintainability

| # | Item |
|---|---|
| **P2-1** | Reduce the 84 mutation-wrapping silent swallows. Each should count via `failopen` (once P1-4 makes that visible) or propagate |
| **P2-2** | Fix `commands/profile` and `commands/dev-refresh` (T-18); give `tui` a graceful message |
| **P2-3** | Document the 28 undocumented subcommands or remove them (T-19) |
| **P2-4** | Make `test_h03_gateway_refuses_tool_calls.py` hermetic (T-22) — it currently calls live providers |
| **P2-5** | Move the 14 `requires_ollama` e2e money tests into a required CI lane (T-24) |
| **P2-6** | Fix `direct_diagnostics`' timeout mislabelling, which makes `doctor` advise a 0-second timeout (T-23) |
| **P2-7** | `quota_tracker` provider key mismatch — `gemini` vs `google` (T-20) |
| **P2-8** | Surface the provenance cutover's excluded-row count so "my lifetime savings went to $0" has an answer (T-21) |
| **P2-9** | Adopt `safe_subprocess`'s env allowlist in `verifiers.run_verifier` (S-07) — least privilege before the next caller arrives |
| **P2-10** | Resolve the quarantine: ~90 known-failing assertions should be fixed, deleted, or reported by the default command |

---

## P3 — cleanup

`session_store`'s `private_opener` adoption (S-09) · `SECURITY.md` 6→7 (S-10) ·
`env_registry` scripts/ scope (T-28) · `calls` denominator in
`get_savings_by_period` (T-26) · the three unparseable scripts ·
`propose.py`'s "verifier authoring assistant" docstring, which oversells a
template engine · release HEAD so the published package contains the dashboard
token fix (T-31).

---

## What not to do

* **Do not add another canonical implementation.** Every P0 here is a call site
  that bypasses one that already exists and is already correct.
* **Do not write another test that asserts a canonical function's behaviour.**
  Seven findings in this report map to exactly that test shape.
* **Do not trust a green suite until P0-1 lands.** It currently reports 0 while 8
  tests fail.
* **Do not publish any savings figure until P0-5 lands** — five of six readers
  have no provenance filter, including the one that posts to a shared channel.
* **Do not build more Ground Truth pipeline before P1-5.** The rigorous half
  already cannot reach the labelling half.

---

## Effort and risk

| Phase | Size | Risk if skipped |
|---|---|---|
| P0 | days | Secrets persisted in cleartext and re-injected into prompts; a published figure that is arithmetically impossible; systematic over-routing on the flagship path; and no reliable way to tell whether any fix worked |
| P1 | 1–2 weeks | Degradation stays invisible; the bandit keeps preferring free models regardless of quality; Ground Truth keeps producing nothing |
| P2 | weeks | Accumulating unobservable failure paths and undocumented surface |
| P3 | days | Ordinary drift |

---

## The one-line framing

Last round's framing was *"finish adopting the fix that already exists."* It was
right, and it was applied to the definitions instead of the call sites.

**This round: fix the call site, and write the test against the call site.**
Seven of the findings above would have been caught by a single test that asked
"does the thing that persists content actually call the thing that scrubs it?"
