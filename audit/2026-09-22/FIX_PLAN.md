# Fix plan — everything found in both audits

**Scope:** all open findings from the 2026-09-21 audit (2 still parked) and all
32 findings from the 2026-09-22 audit. 38 tasks.

**Status values:** `todo` · `running` · `done` · `done (repaired)` · `parked`

---

## Two rules this plan is built around

Both come from how the last round failed.

> **Rule A — the gate is written before the task runs, and it must be RED first.**
> Carried forward; it worked. 8 of 8 mutation probes were caught because of it.

> **Rule B — the gate asserts the CALL SITE, not the definition.**
> New, and the reason this plan exists. Seven findings this round map to a test
> that asserted a canonical function behaves correctly while a live caller
> bypassed it entirely. A test that proves a fix exists is not a test that the
> fix is reached.

A corollary worth stating because it bit twice: **a source-text assertion is not
a call-site assertion.** Tokenize, or call the function.

---

## Phase 0 — make verification mean something

Nothing below Phase 0 can be validated until Phase 0 lands. The suite currently
reports 0 failures while 8 tests fail.

| ID | Task | Gate |
|---|---|---|
| F01 | Fix `test_h08_…::test_replay_detection_looks_for_a_real_runner` cleanup — restore the package attribute, not only `sys.modules[...]` | The 8 accumulation tests fail identically whether that file runs before or after them |
| F02 | Fix or update the 8 `test_groundtruth_accumulation.py` tests invalidated by H-08 | File passes alone; full suite passes in both orderings |
| F03 | Make `replay_available()` robust to module-cache mutation (T-13) | Setting `groundtruth.run_matrix` to a stub does not permanently flip the gate |
| F04 | Autouse fixture snapshotting `sys.modules` + key package attributes, failing on leak | Deliberately leaking a module in a probe test fails that test, naming it |
| F05 | Resolve `_quarantined_tests/` — fix, delete, or report ~90 known-failing assertions | `uv run pytest` output states the quarantined count, or the directory is gone |
| F06 | Make the suite independent of ambient host state | Suite result identical with and without a concurrent `llm-router update` |

**Phase 0 exit:** suite green under `-p randomly`, under `-x`, in both file
orderings, and with a concurrent CLI process running. **Until then, treat every
other gate in this plan as unverified.**

---

## Phase 1 — P0: wrong results, exposure, invalidated claims

| ID | Task | Gate |
|---|---|---|
| F07 | `library/store.scrub_secrets` delegates to `secret_scrubber.scrub_text` (S-01) | A secret battery through `library-harvest` writes 0 survivals |
| F08 | `library/store.write_doc` uses `paths.private_opener` | `stat` shows 0600 at creation, not after chmod |
| F09 | `hooks/agent-route._scrub_agent_prompt` delegates to canonical (S-02) | Battery through `_log_agent_call` → 0 survivals in `agent_calls.json` |
| F10 | `hooks/auto-route._FALLBACK_SECRET_RES` generated from canonical, or its false "kept in sync" comment corrected (S-08) | Fallback and canonical redact the same classes, asserted by comparison not by comment |
| **F11** | **Rewrite `test_m07_no_second_scrubber.py` to call every rival scrubber** | Reverting F07 or F09 makes it fail. **This is Rule B's reference implementation** |
| F12 | `commands/demo.py` — baseline from each row's real premium cost, or remove the comparison (T-02) | A batch containing a premium call never prints a negative saving as "cheaper" |
| F13 | First test file for `commands/demo.py` | Reverting F12 fails it |
| F14 | Gateway passes `system` separately; classify the latest user turn (T-03) | `"hi"` classifies SIMPLE with and without 2KB of system boilerplate |
| F15 | `get_team_savings` provenance filter (T-05) | One synthetic row → $0.00 |
| F16 | Provenance column + write stamping on `claude_usage`, `codex_usage`, `gemini_usage`, `savings_stats` | Same |
| F17 | `get_quality_report`, `get_routing_savings_vs_sonnet`, `get_router_efficiency` filter or use `attribution.py` | Same |
| F18 | Delete or correct the false `is_real` comment at `cost.py:622` | No comment claims a filter that grep cannot find |
| **F19** | **Parametrised test over EVERY money surface** | One synthetic row → all six report zero. A new surface must be added to the list or the test fails |
| F20 | Exclude `budget_lineage_reconciliation` from the wheel or drop its `control_plane.audit` dependency (T-11) | Import + call from the extracted wheel succeeds |
| F21 | `test_shipped_modules_import.py` calls public functions from the built wheel; remove the `conftest.py` auto-skip hiding it | Reverting F20 fails it |

---

## Phase 2 — P1: reliability and evaluation

| ID | Task | Gate |
|---|---|---|
| F22 | Idempotency-dedupe terminal writes a quality-ledger row (T-08) | Two identical keyed calls → 2 rows, second `route_outcome` distinct |
| F23 | Exhaustion-floor terminal writes a row | A fully gate-rejected route produces exactly 1 row |
| F24 | Mark degraded answers degraded on `LLMResponse` and in the rendered output (T-10) | A floor-served response is distinguishable from a clean one by field and by display |
| F25 | Floor responses do not feed `success=True` to the bandit | Bandit stats unchanged by a floor-served turn |
| F26 | Bounded bandit reward (T-09) | Paid at 0.99 success can outrank free at 0.50 |
| F27 | Quality signal stronger than "non-empty and not a deferral", or the reward stops calling it quality | A plausible-but-wrong answer does not score success |
| F28 | Surface `failopen.snapshot()` in `doctor` and `status`; raise the fallback above DEBUG (T-07) | A forced fail-open appears in `doctor` output |
| F29 | Second failure channel for `failopen` that does not depend on its own store | With the store unwritable, the loss is still visible |
| F30 | Bridge or retire the GT verifier pipeline (T-06) | Either an ACTIVE verifier grades a frozen task end to end, or the three modules are gone and the docs say labelling is manual |
| F31 | `run_verifier` receives a `cwd`; gate external evidence as repo state is gated | A repo-bound task either replays against its commit or is refused with a stated reason |
| F32 | `Pool.admit` first-arrival race (T-12) | 20 threads, same new prompt → exactly 1 canonical row |
| F33 | `propose.select_strategy` handles the FACTUAL/checkable shape (T-17) | An admitted checkable question does not fall to `no_reliable_verifier` |
| F34 | `route_server` auth parity with the gateway (S-03) | Unauthenticated request → 401 when a token is configured |
| F35 | `install` / `update` / `dev-refresh` honour `LLM_ROUTER_HOME` (S-04) | With it set to tmp, `~/.claude/` is byte-identical and mtime-unchanged after each |

---

## Phase 3 — P2: architecture and maintainability

| ID | Task | Gate |
|---|---|---|
| F36 | Reduce the 84 mutation-wrapping silent swallows — count via `failopen` or propagate (T-14) | A forced write failure at a sampled site is visible somewhere |
| F37 | Fix `commands/profile` and `commands/dev-refresh`; graceful message for `tui` (T-18) | All three exit 0 or print an actionable message |
| F38 | Document or remove the 28 undocumented subcommands (T-19) | Every dispatchable command is in `--help` or gone |
| F39 | Make `test_h03_gateway_refuses_tool_calls.py` hermetic (T-22) | Passes with the network down |
| F40 | Move the 14 `requires_ollama` money e2e tests into a required lane (T-24) | CI runs them; they are the only full-stack ledger coverage |
| F41 | `direct_diagnostics` timeout mislabelling (T-23) | `doctor` stops advising a 0-second timeout |
| F42 | `quota_tracker` provider key `gemini` vs `google` (T-20) | A real Gemini row is counted |
| F43 | Surface the provenance cutover's excluded count (T-21) | "Why did my lifetime savings drop" has an in-product answer |
| F44 | Adopt `safe_subprocess`'s env allowlist in `verifiers.run_verifier` (S-07) | The subprocess no longer receives live API keys |

---

## Phase 4 — P3: cleanup

`session_store` → `private_opener` (S-09) · `SECURITY.md` 6→7 (S-10) ·
`env_registry` scripts/ scope (T-28) · `calls` denominator (T-26) · three
unparseable scripts · `propose.py`'s "authoring assistant" docstring ·
release HEAD so the published package carries the dashboard token fix (T-31).

---

## Carried forward, still parked

| ID | Task | Why |
|---|---|---|
| P-01 | Collect a clean measurement window | Needs elapsed real traffic, not code. Blocked on F15–F19 |
| P-02 | Republish figures with n, window and source | Blocked on P-01 |

---

## Dependency order

```
F01→F06  (Phase 0)   everything else's validation depends on this
   │
   ├─ F07→F11   scrubber adoption      ─┐
   ├─ F12→F13   demo arithmetic         │  independent of each other,
   ├─ F14       gateway classification  │  can run in parallel
   ├─ F15→F19   money provenance        │
   └─ F20→F21   wheel integrity        ─┘
         │
         ├─ F22→F29   ledger completeness + observability
         ├─ F30→F33   Ground Truth
         └─ F34→F35   auth + isolation
               │
               └─ Phase 3, Phase 4, then P-01/P-02
```

**F19 and F11 are the load-bearing tasks.** They are the two call-site tests. If
only those two shipped, the next audit would find the next bypass instead of
re-finding these.

---

## What would make this round different from the last

Last round: 34 tasks, all gated, suite green after every one — and the gates
were aimed at the definitions, so two live scrubber bypasses and five unfiltered
money surfaces survived untouched.

This round the test for "did it work" is not "does the canonical function behave
correctly" but **"does the code that persists content call the code that scrubs
it, and does the code that reports money call the code that filters it."**

If F11 and F19 exist and are red before the fix, the class closes. If they are
written the way last round's were, it does not.
