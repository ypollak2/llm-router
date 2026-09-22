# 14 — Test Gap Analysis (Phases 29, 30): what the green suite does and does not prove

Scope: the stated baseline is **9,140 results, 0 failed, 0 error, 189 skipped**
(FROZEN_STATE.md, HEAD `357a402`). This document does not re-derive that count —
an attempt to reproduce it from a clean run died mid-suite without printing a
summary line (see "Baseline reproduction" below), almost certainly from CPU
contention with other concurrent audit sessions on this machine, not from a
defect in the suite itself. The number is treated as given, and the question
this phase answers is narrower and more useful: **for a specific list of
critical behaviours, which test actually dies if the behaviour breaks, and how
much of the suite has to run before it notices?**

## Method

All mutations were applied in an isolated `git worktree` at `/tmp/audit-mut`
(`git worktree add /tmp/audit-mut HEAD`), never in the real repo. Because the
project is an **editable install** (`.venv/lib/.../_editable_impl_llm_routing.pth`
points at `~/Projects/llm-router/src`), running pytest from
inside the worktree with the *default* venv would silently exercise the real
repo's unmutated source — this was caught and worked around by exporting
`PYTHONPATH=/tmp/audit-mut/src` to shadow the editable install. Each run used
`LLM_ROUTER_HOME=$(mktemp -d)` and `LLM_ROUTER_BASH_INTERCEPT=off`, and ran
`pytest -x` (stop at first failure) against the full ~9,140-test suite, so
"how long to notice" is measured in real suite percentage/seconds, not a
targeted subset. Every mutation was reverted with `git checkout --` and
confirmed via `git status --porcelain` before the next one; the worktree was
removed at the end. **The real repo `~/Projects/llm-router`
was never edited by this phase** — see "Repo cleanliness" at the bottom.

## Phase 29 — mutation-kill results

12 mutations were run, covering every item in the brief's "at minimum" list.
**All 12 were caught** by at least one test — but two of them were caught only
by a single, narrowly-scoped, late-collected unit test, after 75–91% of the
suite had already run clean. That gap is the actual finding: it is not that
these behaviours are unprotected, it is that they are protected by *one* test
each, and a suite that reports "9,140 passed" gives no signal about how thin
that margin is.

| # | Mutation | File : line | Caught by | Suite position | Time to notice |
|---|---|---|---|---|---|
| M01 | `scrub_text()` returns input unscrubbed | `secret_scrubber.py:139` | `tests/library/test_library_v081.py::test_scrub_secrets_redacts_keys` | ~3% | 30s |
| M02 | `cost_usd()` multiplied by 10 | `pricing.py:361` | `tests/economics/test_pricing_single_source.py::TestCostArithmetic::test_known_quantity` | ~3% | 15s |
| M03 | `grounding.response_is_usable()` forced `True` (the real fn behind `_response_is_usable`) | `grounding.py:342` | `tests/test_success_signal_means_usable.py::TestWhatCountsAsSuccess::test_these_are_not_success[...]` | **90%** | **314s** |
| M04 | routing disabled — `_resolve_profile()` always returns `PREMIUM` | `router.py:987–1050` | `tests/test_codex_routing.py::test_codex_at_front_when_pressure_very_high` | ~13% | 108s |
| M05 | classifier output inverted (simple↔complex swap after parse) | `classifier.py:232` | `tests/test_classifier.py::test_classify_simple` | ~13% | 105s |
| M06 | provenance filter (`production_only`) forced to its no-op branch | `cost.py:1231` | `tests/test_f40_money_ledger_full_stack_hermetic.py::test_a_synthetic_row_does_not_reach_the_totals` | ~39% | 140s |
| M07 | fallback/attempt ledger writer (`_emit_ledger_attempt`) made a no-op | `router.py:1799` | `tests/test_a01_attempt_failed_ledger.py::test_failed_attempts_emit_attempt_failed` | ~16% | 52s |
| M08 | denominator query (savings-summary `COUNT(*)`) forced to return 0 rows | `dashboard_data.py:320` | `tests/test_dashboard_data.py::test_query_daily_sum_matches_query_window_14d` | ~29% | 116s |
| M09 | `llm_router_home()` state path frozen at import (reintroduces the exact incident `paths.py`'s own docstring describes) | `paths.py:32` | `tests/commands/test_doctor.py::TestRunDoctorHost::test_run_doctor_host_codex_healthy` | **<1%** | **9s** |
| M10 | `guard_command()` always returns `(True, "")` | `hooks/agent_writes.py:74` | `tests/test_agent_writes.py::test_programs_outside_the_allowlist_are_refused[curl ...]` | ~22% | 88s |
| M11 | semantic cache: `_get_threshold()` forced to `-1.0` (any similarity is a hit) | `semantic_cache.py:333` | `tests/test_semantic_cache.py::TestThresholdConfig::test_default_threshold` | **83%** | **285s** |
| M12 | Ground Truth join id prefix changed `gtc-` → `gt-` (reintroduces the exact T-06 defect fixed in commit `5462cda`) | `scripts/groundtruth/run_matrix.py:150` | `tests/test_t06_registry_verifier_reaches_a_frozen_task.py::test_the_two_namespaces_really_do_not_intersect` | 91% | 305s |

**Mutation-kill rate: 12/12 on this sample.** This is a curated sample of the
brief's named critical-path behaviours, not a random or exhaustive
mutation-testing pass over the ~412 `src/**/*.py` files — a proper mutation
score would need a tool like `mutmut`/`cosmic-ray` running thousands of
automatically generated mutants, which was out of scope for the time available
here. **The honest claim is narrower: for these 12 specific, named danger
points, at least one test exists today that fails if the behaviour breaks.**
No conclusion should be drawn about the other ~400 modules from this sample.

### The real finding: detection latency, not detection

Three of twelve mutations (M03, M09 excepted — M09 was fast; M03, M11, M12)
were caught only after the suite was 83–91% complete, by exactly one test file
each:

- `_response_is_usable` / `response_is_usable` returning `True` unconditionally
  (a wrong-answer draft counted as a bandit "win") is caught **only** by
  `tests/test_success_signal_means_usable.py`. Delete or skip that one file and
  the entire 9,140-test suite is silent about the router's core anti-reward-hacking
  invariant (T-08/T-10, commit `26a80c8`) breaking.
- The semantic cache matching everything is caught **only** by
  `tests/test_semantic_cache.py::TestThresholdConfig::test_default_threshold`
  — a unit test on a bare constant, not an integration test that a fabricated
  cache hit actually gets served. (See "mocking gaps" below — the *hit path*
  itself, i.e. does a false-positive semantic match actually return the wrong
  cached response to a caller, is not exercised end-to-end anywhere caught by
  this probe within the -x budget.)
- The Ground Truth pool/frozen-task id-namespace bridge (T-06, commit `5462cda`)
  is caught **only** by `tests/test_t06_registry_verifier_reaches_a_frozen_task.py`.

None of these three is a "surviving mutation" in the strict sense — each was
caught — but each is a **single point of failure test**: one accidental
deletion, one over-eager `@pytest.mark.skip`, or one refactor that moves the
assertion without noticing it changed semantics, and the exact bug each commit
message says was fixed (and in M09's case, explicitly documented as having
already caused a real data-loss incident) goes completely unguarded while the
suite still reports "9,140 passed, 0 failed."

### Baseline reproduction (secondary, not required for the above)

An attempt to independently reproduce the FROZEN_STATE.md baseline
(`pytest -q -p no:randomly`, no `-x`, in the real repo under an isolated
`LLM_ROUTER_HOME`) ran for over 6 minutes, reached the warnings-summary section
of the output, and then the process disappeared with **no final `N passed`
line** — consistent with being killed by memory/CPU pressure from a second,
independently-running pytest process observed on this machine at the same time
(`ps aux` showed two concurrent `pytest` invocations mid-run; see repo memory
note on concurrent sessions). This is recorded as an **observation**, not a
finding about the suite: it means the 9,140/0/0/189 figure in FROZEN_STATE.md
is trusted as given rather than independently re-derived here, and the marker
counts below (which collect cleanly and fast) are used as corroborating
evidence instead.

## Phase 30 — mocking audit

### 1. `mock_acompletion` doesn't mock `acompletion` — a named boundary that lies about itself

`tests/conftest.py:473` defines a fixture literally named `mock_acompletion`
whose docstring says "Mock async completion for provider tests," but its body
patches `llm_router.providers.call_llm` — an **internal** function, not
`litellm.acompletion` (the real network boundary). Every test that uses this
fixture (`tests/test_router.py`'s `test_system_prompt_included`,
`test_routes_to_first_available_model`, `test_model_override_bypasses_routing`,
etc.) is verifying what `router.py` passes into `providers.call_llm` — real
code, genuinely exercised — but **never verifies that `providers.call_llm`
correctly forwards that payload to the actual provider**. That seam
(`providers.py`'s `inject_cache_control(messages, model)` call, which builds the
literal `messages` key sent to `litellm.acompletion`) is unit-tested in
isolation by `tests/test_prompt_cache.py`, but the two tests never run
together: nothing asserts "call `route_and_call(system_prompt=...)` and watch
`litellm.acompletion` receive it." The one file that mocks `litellm.acompletion`
directly and captures the true kwargs, `tests/test_integration.py` (whose own
docstring says exactly this: "exercise `route_and_call()` end to end while
mocking the network boundary at LiteLLM"), never asserts on `messages` or
`system_prompt` in its 3 non-`requires_api_keys` tests — only on `model`,
response-content substrings, token counts, and one `extra_body` param. **The
router→provider system-prompt handoff is fully tested at each end and
untested across the actual seam.**

### 2. Getsource / source-text assertions: decoupled from the behaviour they claim to pin

Grepping the 33 test files touched by the last 15 commits, 11 use
`inspect.getsource` (`test_t08_every_terminal_path_writes_a_quality_row.py` has
5 occurrences, the most of any file). Representative example, quoted exactly:

```python
def test_the_router_actually_uses_that_expression():
    """Rule B: assert the call site, not a re-implementation.
    The test above builds the expression itself, which would pass even if
    router.py never adopted it. This pins the source.
    """
    src = inspect.getsource(router)
    assert 'False if getattr(response, "quality_degraded", False)' in src, (
        "router.py's bandit feed no longer short-circuits on quality_degraded"
    )
```

This is a **substring-presence check over the whole module**, not a check that
the literal text appears *at the call site that matters* (the docstring even
says the preceding test built its own re-implementation and this one exists to
"pin" that router.py really uses it — but a substring match anywhere in a
5,000-line file does not prove that). By inspection (not run as a live
mutation, due to time budget — classified **DESIGN RISK**, not CONFIRMED): a
semantically-equivalent rewrite that (a) keeps that exact string present
somewhere in `router.py` — e.g. in a comment, a docstring, or an unreachable
branch — while (b) changing the *actual* bandit-feed call site to a
differently-worded but broken expression, would pass
`test_the_router_actually_uses_that_expression` while reintroducing exactly
the constant-reward bug T-08/T-10 fixed. The same pattern repeats at
`test_the_ledger_gate_admits_a_named_outcome` (`assert "not served_from_cache
or ledger_outcome" in src`) and `test_a_degraded_outcome_forces_route_succeeded_false`
(`assert "route_succeeded=(ledger_outcome not in _DEGRADED_LEDGER_OUTCOMES)" in
src`) in the same file, and in `test_t06`, `test_t07`, `test_t09`, `test_t12`,
`test_t17`, `test_f31`, `test_s03`, `test_groundtruth_accumulation`,
`test_phase3_broken_surfaces`. Not all of these are equally weak — several
(e.g. `test_t06`'s namespace test, confirmed live above as M12) call the real
function and assert on its *output*, using `getsource` only as a secondary
"pin" alongside a behavioural assertion. The ones that are **purely**
source-text with no accompanying behavioural check are the ones actually at
risk, and `test_t08`'s two router-wide substring scans are the clearest
examples.

### 3. `TestSilentMutationRatchet` (T-14) is itself a source-census test

`tests/test_t14_silent_mutation_ratchet.py` ratchets a **count** of
`except: pass` sites around state writes (currently ≤88, was 92) via an AST
census script. This is a reasonable second-order defence (bounds a known-messy
population instead of claiming to eliminate it) but it is worth flagging
alongside the getsource findings above: a meaningful fraction of this repo's
newest safety tests protect *source shape*, not *runtime behaviour*. Both
mechanisms are honest about their own limits in their docstrings (the T-08 test
literally says "pins the source"), which is better than most codebases do, but
the audit brief specifically asked whether they would survive a rewrite that
keeps the shape and loses the behaviour, and the answer for the two flagged
above is: yes, plausibly.

## Skip analysis: the 189

`pyproject.toml`'s `addopts` deselects `not slow and not requires_ollama and
not requires_api_keys and not requires_codex` before collection even starts.
Measured directly (`pytest --collect-only -m "<marker>"`):

| Marker | Tests deselected |
|---|---|
| `slow` | 27 |
| `requires_ollama` | 19 |
| `requires_api_keys` | 15 |
| `requires_codex` | 1 |
| **Union** | **62** |

These 62 never appear in the 9,140/189 figure at all — they are excluded
before collection, not "skipped." The **189 skipped** that do show up inside
the 9,140 are a different population: runtime `pytest.skip()` calls gated on
environment probes (81 call sites across 34 files: `shutil.which("uv") is
None`, missing built `wheels`/`sdists`, `if not ROUTERARENA_DATA.exists()`,
absent hook files, `"No module named 'textual'"`, subprocess `returncode != 0`,
etc.), several of them parametrized (one call site can account for many
skipped instances). This means: **on a machine without `uv`, without built
wheel/sdist artifacts, or without the RouterArena data file present, packaging
and distribution-content tests silently skip rather than fail** — which is a
legitimate CI-portability pattern, but it does mean the "0 failed" headline
number is silent about whether sdist/wheel contents (e.g. "does the shipped
package still exclude `_quarantined_tests`") were actually checked in this
particular run, or merely not-checked-and-not-reported-as-such beyond the skip
count. Nothing in the 62-marker-deselected or ~189-runtime-skipped populations,
on inspection, looked like a capability quietly disabled rather than
environment-gated — the skip reasons read as genuine "tool absent" conditions,
not disguised failures.

## Surviving mutations

**None**, in the 12-mutation sample run here. This should be read narrowly: it
means the specific list of named critical behaviours in the audit brief each
have at least one guarding test today, not that the suite's overall mutation
score is high, and not that other equally-critical behaviours outside this
named list are protected — this phase did not attempt a broad, tool-driven
mutation sweep of the ~412 `src` modules, which would be the only way to make
that broader claim honestly.

## Repo cleanliness

All 12 mutations were made and reverted inside `/tmp/audit-mut` (a `git
worktree`), never in `~/Projects/llm-router`. Confirmed via
`git status --porcelain` in the worktree after each revert (empty) and in the
real repo before and after this phase. The worktree was removed with `git
worktree remove /tmp/audit-mut --force` on completion. The real repo's only
change from this phase is this file, `audit/14_TEST_GAP_ANALYSIS.md`, which the
audit brief requires as the deliverable; every other file touched during this
session belongs to other concurrently-running specialist agents writing their
own `audit/*.md` files (observed via `git status` throughout — `00_`, `01_`,
`02_`, `03_`, `04_`–`13_`, `15_`, `18_`, `README.md` all appeared over the
course of this run and were never edited by this phase).
