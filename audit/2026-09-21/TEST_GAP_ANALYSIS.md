# Test gap analysis

v14.1.0 · 2026-09-21 · 680 test files. The question is not "do tests pass" but
**"if I break important behaviour, which test catches it?"**

---

## The headline: mutation probes — 6 of 6 caught

Six deliberate defects were introduced into the real tree, the real suite was
run, and every edit reverted (tree verified clean).

| Mutation | Result | Caught by |
|---|---|---|
| `net_saved` sign flipped | **CAUGHT** | 6 tests, incl. `test_net_saved_does_not_round_a_sub_cent_loss_away` |
| `summarize()` denominator `>= 2` → `== 2` | **CAUGHT** | 8 tests across two files |
| `scrub_text()` → no-op passthrough | **CAUGHT hard** | 23 tests, all 8 secret patterns |
| `get_model_chain()` → always length 1 | **CAUGHT hard** | 25 tests, incl. one literally named `test_every_profile_yields_a_chain_with_a_fallback` |
| `is_evaluable()` → always True | **CAUGHT, thin** | **only 2 tests, one file** |
| `assess()` → always eligible | **CAUGHT** | 9 tests |

**This is the strongest result in the entire audit.** The safety-critical logic
is genuinely defended — not by coincidence, but by tests written to fail when
specific behaviour breaks. The denominator regression fixed yesterday would be
caught if reintroduced.

**The weak one is `is_evaluable()` (2 tests, one file).** No test anywhere checks
that a contaminated ledger produces a *wrong published number* — which is
precisely how finding H-01 survived: the primitive is tested, its absence in
every consumer is not.

---

## The caveat that undercuts "all tests pass"

```
addopts = "-m 'not slow and not requires_ollama and not requires_api_keys
              and not requires_codex' -q --tb=short"
```

**The default run excludes every marked integration test.** So "the suite passes"
means the *mocked* suite passes. Live provider failure, timeout, retry against a
real backend — none of it runs in the command everyone uses, including the
pre-release gate.

57% of test files (389/680) use mocks. `tests/e2e/conftest.py` says so plainly:
*"most tests are unit/mocked, and that is the entire point."* Honest — but it
means integration confidence is lower than the green tick suggests.

---

## Confirmed tautology

`tests/test_gateway_service.py:53`:

```python
assert not dest.exists() or True  # write=False must not create it
```

The `or True` makes it pass unconditionally — the one thing
`test_install_write_false_does_not_touch_disk` claims to prove is unproven. If
`install_gateway_service(write=False)` began writing to LaunchAgents, nothing
here would notice.

**Design risk, not a tautology:** `tests/test_zero_claude_bypass.py:226` —
`assert all(len(t["content"]) < 20000 for t in hist) or len(hist) == 1`. If
trimming misbehaves so only one oversized turn survives, the size check is
skipped — exactly the scenario a token-cap bug produces.

---

## Coverage gaps on critical paths

| Behaviour | Coverage |
|---|---|
| **Tool-definition survival through the gateway** | **NONE.** No test sends `tools=`. A known live defect (H-03) with no regression guard |
| Live provider failure / timeout / retry | Only in `tests/e2e/`, **deselected by default** |
| Quota exhaustion | Thin — one file for logic spanning several modules |
| **Contaminated ledger → wrong published number** | **NONE.** The gap that let H-01 through |
| **Cache correctness** (vs hit rate) | **NONE.** The 0.99-cosine collisions have no test |
| **Failed route produces a ledger row** | **NONE.** Would have caught C-01 |
| Malformed/truncated JSONL, concurrent ledger writes | Good — four dedicated files |
| Price-table miss for unknown model | Covered |
| Hook wall-clock timeout | Covered |

---

## Implementation-coupled tests

`tests/commands/test_{team,doctor,config,budget,...}.py` assert
`mock_run.assert_called_once_with(...)` — that the dispatcher forwards correctly,
not that anything works. Defensible for a thin shim; they would break on a
harmless refactor while missing a real bug downstream. `test_codex_routing.py`
asserts only `mock_codex.called`, weaker than its own docstring claims.

---

## What to add, in priority order

1. **A test that a failed route writes a ledger row** — would have caught C-01,
   the audit's most serious measurement finding.
2. **A test that synthetic rows do not move a published metric** — the missing
   downstream half of the `is_evaluable` coverage.
3. **Adversarial cache pairs as regression tests** — the three measured
   collisions, asserting they miss.
4. **A gateway test that sends `tools=`** and asserts either pass-through or an
   explicit rejection.
5. **Delete the `or True`** in `test_gateway_service.py:53` and let the
   assertion mean something.
6. **Run the marked tests in CI**, even if not in the default developer command.

---

## Honest assessment

This suite is **better than the rest of the audit would lead you to expect**. The
mutation results are real evidence, not a coverage percentage. Where the project
decided a behaviour mattered, it defended it properly.

The gap is not test quality — it is that **the tests defend the primitives and
not their adoption**. Every major finding in this audit lives in that space: a
correct function, tested, that its consumers don't call.
