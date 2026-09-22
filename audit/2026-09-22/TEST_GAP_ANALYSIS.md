# Test gap analysis — 2026-09-22

9029 tests collected. 62 deselected by default. 723 test files.

**The headline is not a gap. It is that the pass/fail signal itself is not
trustworthy.**

---

## The signal is unreliable in three independent ways

### 1. Cross-test module-cache contamination — 8 real failures masked

```
accumulation file alone                  ->  8 FAILURES
h08 first, then accumulation             ->  0 failures
accumulation first, then h08             ->  8 FAILURES
full suite (h08 #52, accumulation #377)  ->  0 failures, exit 0
```

`tests/qa/test_h08_gate_admits_only_gradable_tasks.py::test_replay_detection_looks_for_a_real_runner`
sets both `sys.modules["groundtruth.run_matrix"]` and the package attribute
`sys.modules["groundtruth"].run_matrix`, and restores only the first. Any
reordering — `pytest-randomly`, `-x`, xdist, or adding one test — surfaces them.

### 2. Quarantine — ~90 known failing assertions excluded

`_quarantined_tests/TRIAGE_2026-09-15.md` records, when run directly:
`test_deep_reasoning_classifier` 45 failing, `test_budget_envelope` 15,
`test_subscription_local` 14, `test_summary` 7, plus import errors. Disclosed,
but "0 failures" from the default command is not "0 known failures".

### 3. Ambient host state — 3 tests flip

A concurrent `llm-router update` writes to `~/.claude/` regardless of
`LLM_ROUTER_HOME`, which flipped three config tests during this audit
(`BUDGET` vs `BALANCED`). They pass in isolation.

---

## Mutation results — the genuinely good news

Eight deliberate defects injected into load-bearing logic, each restored and the
tree verified clean afterwards:

| Mutation | Caught? | By | Time |
|---|---|---|---|
| Fallback records the original model, not the one that answered | **yes** | `test_route_ledger_integration` (pre-existing) | 7s |
| `record_route()` silently returns False | **yes, 14 failures** | crit01 + 2 pre-existing files | 3.7s |
| `is_evaluable` always True | **yes, 4** | `test_h01_summarize_honours_provenance` | 1.6s |
| Semantic-cache veto always "allow" | **yes, 7** | `test_c03_cache…` | 2.1s |
| Scrubber returns input unchanged | **yes, 11** | c04 + m07 | 2.5s |
| Gateway stops refusing tool calls | **yes, 3** | h03 | 14s |
| `exclusive_lock` becomes a no-op | **yes** | `test_h07_pool_admit_counts` (15/22 lost) | 2s |
| Eligibility admits everything | **yes, 11** | mostly pre-existing accumulation tests | 2.9s |

**8 of 8**, with messages naming the exact broken behaviour. Also: **zero
tautologies** across `tests/`, no `@pytest.mark.skip` anywhere, 2 xfails both
deliberate pins.

---

## Where the coverage genuinely is not

| Behaviour | What should exist | What exists |
|---|---|---|
| Full ledger → quota → pool through a real model call | an e2e test in the default lane | 14 tests behind `requires_ollama`, **deselected by default** — the only full-stack money coverage |
| A rival scrubber's **adoption** at its call site | call the rival function and compare | `test_m07` asserts only the canonical function's coverage — **this is why T-04 shipped** |
| Function-level lazy imports in shipped modules | call public functions from the built wheel | `test_shipped_modules_import` only `import`s modules — **this is why T-11 shipped** |
| `commands/demo.py` arithmetic | any test at all | **none** — which is why T-02 shipped |
| Gateway classification given a system prompt | build a multi-role message list, assert complexity | none — which is why T-03 shipped |
| Terminal paths that write no ledger row | assert row counts per terminal | content-only assertions — which is why T-08 shipped |
| Bandit reward across cost tiers | assert a paid model can outrank a free one | cold-start mechanics only — which is why T-09 shipped |
| Post-cleanup state after a module-cache patch | assert the restore actually restored | none — which is why T-01 shipped |

**Read that column downward.** Seven of this audit's findings map to a test that
asserts the *shape* of a fix rather than its *effect at the call site*.

---

## Weak tests found

| ID | Test | Problem |
|---|---|---|
| **T-22** | `test_h03_gateway_refuses_tool_calls.py` | Not hermetic. Makes real outbound calls — OpenAI auth error, Ollama connect, a live Codex call returning 200 in 8.2s. 14s wall clock, network- and host-dependent |
| **T-01** | `test_h08_gate_admits_only_gradable_tasks.py` | Leaks a module-cache fake; masks 8 failures elsewhere |
| **G-2** | `test_h02_readme_claims_are_traceable.py` | Traceability is a raw substring test: `"105" in "the year 2105"` → True. False-negative-prone |
| **G-3** | `test_m06_wal_pragma_ordering.py` | Pins source line order rather than behaviour. Defensible (the PRAGMA reports failure by returning, so there is no behavioural hook) but brittle to reformatting |

---

## Classification of the 25 tests added 2026-09-21

| Kind | Files |
|---|---|
| Unit with anti-vacuity guards | 19 |
| AST-based invariant | 3 (`m11`, `l03`, `no_import_time_path_binding`) |
| Integration over real HTTP | 2 (`h03` — see T-22, `m08`) |
| Subprocess / CLI | 2 (`m12`, hook isolation) |
| Concurrency stress | 2 (`h06` 2000-read hammer, `h07` 7-thread) |
| Docs consistency | 2 (`h02` — see G-2, `m12`) |

An independent reviewer rated them "unusually high quality" — a specific
regression story per docstring, an anti-vacuity guard, an anti-over-correction
test — and then found the weaknesses above anyway. Both statements are true.

---

## The lesson

The new tests are strong at proving *a function behaves correctly*. They are
weak at proving *the system uses that function*. Eight of 8 mutations to
canonical implementations were caught; two live call sites bypassing a canonical
implementation were caught by none of them.

**A test that asserts a fix exists is not a test that the fix is reached.**
