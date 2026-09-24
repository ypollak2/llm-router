# Domain 11 — Tests, Error Handling, Observability, Logging

Auditor: Domain 11 (TST- / ERR- / OBS-). Baseline: worktree `llm-router-forensic` @ 3c96d23.
All commands run with `HOME=$(mktemp -d) PYTHONPATH=src <venv>/python ...`; no full-suite run
performed (baseline: `00_baseline_pytest.log`, 9,640 tests, 0 failed, 0 errors, 200 skipped —
independently corroborated by domain-12's `12_docs_claims.md`). Targeted file/pair runs only.
`<repo>/CLAUDE.md` (git-ignored, read per coordinator instruction)
supplied the measurement discipline this domain leans on throughout: denominators, "unknown ≠
favourable", narrowest-mutation red-checks, and the R13/S9/S9b ratchet pattern.

## Overview

This is an unusually mature test/error/observability layer for a repo this size. The dominant
pattern across ~9,600 tests is an incident-driven regression test named after its bug ID
(`GH#NN`, `CHZ-*`, `R##`, `S##`, `A##`, `T-##`) with a docstring that states the original defect,
why it was missed, and what property is now pinned — not generic "test the function" coverage.
The stratified sample (73 files, §30) found **zero** DUPLICATE / LOW-VALUE / OBSOLETE files in the
live `tests/` tree; that debt is instead concentrated and tracked in `_quarantined_tests/`, which
is itself actively triaged (9 files remain, down from 15 on 2026-09-15, via two commits that
restored one test, deleted two after finding the modules they covered are *shipped but broken*,
and replaced a third after discovering it had silently skipped for a month).

Two real, evidence-backed problems sit alongside that strength:

1. **TST-01** — the test-isolation guard that is supposed to catch module-stub leaks produces a
   reproducible **false positive** (a spurious `ERROR`, not merely a skip) on any test that is
   first-in-process to import one of six namespace packages (`ui`, `hooks`, `policies`, `static`,
   `rules`, `commands` — none has `__init__.py`). This makes `pytest tests/<single_file>.py`
   order-dependent for at least the `ui` package, confirmed by direct execution.
2. **OBS-01 / ERR-01** — the fail-open accounting system (`failopen.py`) is well-designed but only
   reaches roughly 6-16% of the 1,020 broad `except Exception` handlers in `src/`; the rest still
   swallow silently, and 270 of them are bare `pass` with no log line, no comment reference, and no
   `failopen.record()` call at all. Telemetry is written to at least 16 independently-designed
   JSONL/log stores (no single event model), with one fact (`savings_log.jsonl`) written by six
   different modules that each re-implement the same path-lookup helper rather than importing the
   one canonical version in `cost.py`.

---

## §30 — Test suite

### Counts

| Metric | Value | Source |
|---|---|---|
| Test files, `tests/` top-level (`test_*.py`) | 653 | `find tests -maxdepth 1 -name 'test_*.py' \| wc -l` |
| Test files, `tests/` total (incl. subdirs) | 762 | `find tests -name 'test_*.py' \| wc -l` |
| Subdirs contributing the other 109 | `qa`(19) `economics`(12) `audit`(9) `commands`(9) `semantic`(10) `security`(6) `storage`(6) `telemetry`(6) `lineage`(5) `okf`(4) `reliability`(4) `routing`(4) `e2e`(3) `library`(3) `scenarios`(3) `agentic`(2) `docs-private`(2) `install`(2) | `find tests -mindepth 2 -name 'test_*.py'` grouped by parent dir |
| `_quarantined_tests/*.py` | 9 | see quarantine table below |
| Production LOC (`src/`) | 131,781 | `find src -name '*.py' \| xargs wc -l` |
| Test LOC (`tests/` top-level) | 141,575 | `find tests -name '*.py' \| xargs wc -l` |
| Test LOC (quarantine) | 1,777 | `find _quarantined_tests -name '*.py' \| xargs wc -l` |
| Test : production LOC ratio | ~1.09 : 1 | Both sides are inflated by this repo's very long narrative docstrings (see Overview) — a raw LOC ratio overstates "how much is asserted" relative to "how much is explained." Not a defect, a methodology caveat. |
| Files using `monkeypatch` | 398 / 762 (52%) | targeted, restorable substitution — the dominant mocking style |
| Files using `MagicMock`/`@patch`/`Mock(` | 56 / 762 (7.3%) | heavy-mock style is the minority |
| `conftest.py` files | 5 | |
| Files using `hypothesis` | 3 | light property-based testing footprint |
| Files matching `snapshot` | 52 | mostly "snapshot of a rendered/serialized shape," not golden-file snapshot testing |
| Files with `pytest.mark.skip` | 14 | |
| Files with `xfail` | 9 | |
| Slowest tests / per-test timings | **not measured** | the saved baseline log (`00_baseline_pytest.log`) was captured without `--durations`; running the full suite again to get timing is against the audit rules for this task. Flagged as a gap (TST-06), not fabricated. |

### Quarantine (`_quarantined_tests/`)

Currently 9 files: `test_audit.py`, `test_budget_envelope.py`, `test_classify.py`,
`test_deep_reasoning_classifier.py`, `test_routerarena_submit.py`, `test_signals.py`,
`test_subscription_local.py`, `test_summary.py`, `test_team.py`. This is **not** a dumping ground —
`README.md` names three distinct reasons per file and an explicit rule ("upstream has a
similarly-named file" ≠ "upstream asserts the same behaviour — diff before deleting"), and
`TRIAGE_2026-09-15.md` records what happens when each file is actually run today.

Verified via `git log`, two commits since the triage resolved 6 of the original 15:

- `9011bad` (2026-09-15): `test_audit_routing.py` restored as `tests/test_misroute_audit_behaviour.py`
  (37 passing tests) after discovering its "upstream replacement," `test_misroute_audit.py`, had
  exactly one test asserting only that the old name is gone — the scoring/precedence/env-gate logic
  had zero real coverage for four weeks. `test_cp_audit.py` / `test_cp_sse_policy_events.py` were
  **deleted**, but only after the investigation found the real defect the quarantine was hiding: the
  *shipped* `control_plane.api` module cannot be imported at all (unconditional `from
  llm_router.control_plane import audit`, and `audit.py` is deliberately excluded from the
  distribution). That finding is now pinned by `tests/test_shipped_modules_import.py`, a ratchet
  over every module in the package, not a guess at a fix.
- `9485a43` (2026-09-15): `test_hook_equivalence.py` — the file the triage doc flagged as **silently
  skipping instead of failing for a month** (`parents[2]` path bug) — replaced by
  `tests/test_hook_classifier_equivalence.py`. Verified present. The investigation it triggered
  measured the real hook/classifier divergence at 200 real prompts × 4 task types (800 comparisons):
  74% agree, 0% hook-more-expensive, 26% hook-cheaper (all from one flag, `apply_floor`) — correcting
  an earlier parked claim (A4) that had used 3 hand-picked prompts against the wrong reference
  policy. `HOOK_LIVE_POLICY` was added as the true equivalence target (1000/1000 agreement) and the
  new test does not skip when the hook fails to import.

**Recommendation for the remaining 9**: `KEEP`, unchanged from the triage doc's own conclusion —
the required "diff assertions against the upstream replacement" step has not been done for any of
them. Deleting on file-name-similarity alone would repeat the exact mistake the process just caught
twice.

### R13 source-text-assertion ratchet — verified baseline

Ran the actual detector in `tests/test_r13_no_source_text_assertions.py` (`_source_text_assertions()`)
rather than trusting a description of it:

```
count: 1
['test_s03_route_server_auth_parity.py:181']
```

`MAX_SOURCE_TEXT_ASSERTIONS = 1`, and the detector is currently **exactly at** its ceiling — not
below it, but that is because the one survivor is deliberately named in `REMAINING_BY_DESIGN` with a
>60-char justification (it checks a specific stale docstring sentence is gone, which
`_ast_assert.string_constants` deliberately can't do since it excludes docstrings). This is a
well-run ratchet, not a stalled one: it started at 62, is now 1, and the 1 is accounted for.
`tests/_ast_assert.py` (AST-based `assert_calls`/`assert_guarded_by`) is the replacement primitive
and is itself tested against a synthetic "comment survives, call site removed" evasion — the exact
class of bug the 2026-09-22 audit found defeating 23 of its predecessors.

A broader, cruder grep (`assert.*\bin\b.*(source|src|code)` or `.read_text()`, no AST filtering)
turns up 193 hits — that number is **not** the right measurement; it's exactly the over-broad
detector shape the R13 docstring warns against (a fixture string containing "in src/a.py" reads as
a hit). Use the repo's own detector, not a crude grep, when re-measuring this.

### Isolation artefact — TST-01, reproduced live

The brief flagged "some tests leave `llm_router.ui` / `llm_router.hooks` module stubs and error when
run alone" as a known pre-existing issue. Traced to root cause and reproduced:

- `tests/conftest.py`'s autouse `_no_module_state_leak` fixture (T-01, added after a real masked
  8-test failure) flags any **new** attribute hung on `sys.modules["llm_router"]` or
  `sys.modules["groundtruth"]` whose value has `__file__ is None` — its heuristic for "this is a fake
  stub, not a real import."
- `src/llm_router/{ui,hooks,policies,static,rules,commands}/` all have **no `__init__.py`**
  (confirmed: `find src/llm_router -maxdepth 1 -type d` against presence of `__init__.py`). They are
  namespace packages, and a namespace package's module object genuinely has `__file__ is None` on
  first import — indistinguishable, by this heuristic, from a mock.
- Reproduced: `pytest tests/test_first_forty_w2.py -q` alone →
  `ERROR at teardown of test_quota_absent_is_not_reported_as_zero`:
  `"this test left a fileless module stub on a package object: llm_router.ui"` — even though the
  test only does `from llm_router.ui import status_premium as sp` inside its body, a completely
  ordinary import.
- Confirmed order-dependence: `pytest tests/test_session_summary_xaxis.py tests/test_first_forty_w2.py -q`
  (the first file imports `llm_router.ui.session_summary` at **module** level, i.e. at collection
  time, before any test's fixtures run) → **passes clean**. The `ui` attribute is already present on
  `llm_router` before `_no_module_state_leak`'s "before" snapshot is taken for the second file's
  first test, so nothing looks "new."
- One file (`test_s7_rendered_output_is_rendered.py`) already documents this exact mechanism in a
  comment and works around it correctly with an opt-in `importing_a_submodule` fixture that snapshots
  and restores `vars()` on every `llm_router.*` module, not just top-level `llm_router`. That fixture
  is the right fix and exists; it is simply not used by every test that imports one of the six
  namespace packages (`test_first_forty_w2.py`, `test_statusline_truthfulness.py` do not use it).

This means: which tests show red depends on pytest's collection order and on unrelated files'
import statements, not on anything the failing test itself does wrong. Running a single suspect
file in isolation — the normal first move when investigating a report — reproduces a failure that
disappears in full-suite runs and reappears/disappears depending on `-k`/file-subset selection.
That is a textbook false-confidence generator (§65).

### Stratified classification — 73 files across 19 areas

Selected by category-stratified random sample (seed 42) across routing/classify, fallback,
accounting/cost/savings/budget, host integration (hooks/codex/cursor/gemini/windsurf), security,
migrations, concurrency, config, MCP, CLI, observability/telemetry, cache, dashboard, agentic,
zero-Claude, session/context, providers, and the R-/S-series audit ratchets, plus a general pool.

| Classification | Count | What distinguishes it here |
|---|---:|---|
| CRITICAL CONTRACT | 27 | Protects an invariant with wide blast radius if broken: migration framework itself (`test_migrations.py`), config singleton thread-safety, the v3 telemetry join contract (`test_groundtruth_traceability.py`) + its hash determinism (`test_g025_auto_trace_id.py`), "every completion endpoint refuses what it cannot serve" via self-discovering route scan (`test_r10_refuse_what_cannot_be_served.py`), success/failure-signal integrity (`test_r15_finish_reason.py` — a censored answer must not read as a win), cross-project cache leak prevention, session-scoping of `set-enforce`, host installer byte-for-byte idempotency, sdist/wheel completeness. |
| HIGH-VALUE REGRESSION | 22 | Named incident (`GH#41`, `GH#49`, `GH#53`, `CHZ-AUD-A-02/04/29`, `R16`, `S2-5b`, `A30`) with a docstring stating the original defect and why it was missed; mostly real-object assertions, monkeypatch used narrowly. |
| USEFUL UNIT | 22 | Ordinary, well-scoped unit tests of one function/module with no incident reference — e.g. streaming-event field shape, timeout-config defaults, provider-quirk registry no-ops. |
| IMPLEMENTATION-COUPLED | 2 | `test_session_context_wiring.py` (60 mock references over 325 lines, several assertions on internal call *shape* — `builds_and_threads_session_context_into_execute_chain` — rather than observable output) and `test_context_capture_hook.py` (41 mocks; mixes genuine fail-open behavioral tests with one call-shape assertion). |
| DUPLICATE | 0 | None found in this sample — the suite's duplication risk lives in the quarantine/upstream-sync boundary (see above), not inside the live tree. |
| LOW-VALUE | 0 | — |
| OBSOLETE | 0 | — |

Full 73-file list with per-file classification is in the working notes; the table above is the
reportable rollup. No file was marked HIGH severity for being ugly — several CRITICAL CONTRACT
files (`test_migrations.py`, `test_config_thread_safety.py`) are plain and short.

### Coverage gaps observed while sampling

- **Fallback chains**: covered narrowly (`test_router_block_providers.py` pins one guard in
  `_build_and_filter_chain`), not exhaustively — no file in the sample enumerates the fallback
  ladder end-to-end under "all providers unavailable but one."
- **Concurrency**: `test_config_thread_safety.py` and `test_provider_registry_persistence.py`
  (SQLite cross-instance) are solid; genuine multi-process race coverage for `savings_log.jsonl`'s
  documented AC-5 dual-writer race (see OBS-01) was **not found** in the sample — the mitigation
  (atomic claim-file rename) exists in code but a test exercising two concurrent drainers racing on
  the same file was not located.
- **Migrations**: strong (framework + two concrete migrations tested, including idempotency and
  rollback-refusal-when-down-missing).
- **Security**: strong on the specific, named CVE-style regressions (statusline injection, prompt
  injection detection, direct-execution blocklist coverage pinned against `SECURITY.md`'s own
  claimed ratio) — the R10 route-refusal test is the standout for structural (not just
  regression) security coverage.
- **Config precedence**: `test_config_thread_safety.py` covers the singleton; a dedicated
  defaults/env/.env/YAML/CLI-flag precedence-ladder test was not seen in the sample (may exist
  outside it; not claiming absence, only non-observation).

---

## §21 — Error taxonomy

| Metric | Value | Method |
|---|---:|---|
| Broad `except` handlers in `src/` (bare, `Exception`, or tuple containing `Exception`) | 1,020 | AST walk over every `.py` in `src/llm_router`, counting `ast.ExceptHandler` nodes |
| ...of which body is `pass` only (fully silent — no log, no record, no re-raise) | 270 (26.5%) | same AST scan, `len(body)==1 and isinstance(body[0], ast.Pass)` |
| ...of which the handler's own body directly calls `failopen.record(...)` | 61 (6.0%) | same scan, source-text check on the unparsed handler node |
| Grep-wide count of `failopen.record(` call sites (incl. inside helper functions called from except blocks, not just inline) | 162 | `grep -rn "failopen.record("` |
| Distinct fail-open codes registered | 59 | unique literal strings passed as the first arg across all call sites |
| Bare `except:` (no type at all) | 3 | `grep -c "except:"` |

**Reading this honestly**: even crediting every indirect call (a broad except whose body calls a
helper that itself calls `failopen.record`), the 162 total call sites cannot cover more than ~16%
of the 1,020 broad-except sites. The 270 pure-`pass` handlers are a lower bound on what's still
fully silent — some of the remaining ~750 non-`pass`, non-`record`-calling handlers do log via
`structlog`/`logger.warning` etc. (not itself a defect), but a meaningful population swallows with
neither a log line nor an accounting call. `failopen.py`'s own docstring says this directly: "the
codebase carries ~810 broad `except Exception` handlers" and the tool exists to instrument the ones
"in the money, routing, verification and telemetry paths" — it was explicitly scoped to a subset,
not the whole population, which this measurement confirms (ERR-01).

**`failopen.py` itself is well-engineered** (KEEP, do-not-change candidate): `record()` never
raises; a write failure falls back to an in-process counter (`_unpersisted`) plus a `WARNING`-level
structlog line (fixed from `DEBUG` under T-07, which had made an unwritable-store burst invisible
everywhere); `FailOpenCounts.total` returns `None` (not `0`) when the store is unreadable, and the
render layer distinguishes "0 recorded" from "store unreadable" from "recorded but not persisted."
Two production readers exist: `counter_registry.py` (feeds `doctor`) and `ui/status_premium.py`
(feeds `status`) — this closes the T-07 finding the code comments describe ("58 call sites, 0
readers outside tests").

**Residual gap (ERR-02)**: both readers call `render_report(limit=8)` / `limit=4` — with 59
registered codes, `doctor`/`status` surface only the top 8 (or 4) by volume. A rare-but-severe code
(e.g. a cost-cap-ledger read failure that fires once but matters a great deal) can be silently
outranked in the display by a noisy, benign one, and would only be visible by reading
`fail_open.jsonl` directly.

**Fail-open vs fail-closed, spot-checked**: the split looks deliberate rather than accidental.
Zero-Claude mode explicitly fails **closed** when no external agent is available
(`test_zero_claude_scenarios.py::test_tool_task_fails_closed_when_external_agent_is_unavailable`) —
the one place where "just continue in Claude" would silently defeat the feature's whole purpose.
Telemetry/accounting/hook paths fail **open** by design (a raised exception there would kill the
user's turn over a bookkeeping failure), which is exactly the population `failopen.py` targets.

---

## §33 — Observability

**Can it answer "why this route"?** Partially, and the pieces don't share one shape:

- The hook's own decision path writes prose lines to `auto-route-debug.log`
  (`_debug_log()` in `hooks/auto-route.py`) tagged `[INVOCATION <id>]`, and — only when
  `LLM_ROUTER_TRACE` is set — mirrors the same event into `llm_router.trace` (structured, joinable
  by invocation id). **Trace is opt-in and off by default**, so the joinable version of "why this
  route" is not what a default install produces; the always-on version is a debug-log grep.
- `routing_report.py` builds several honest, denominator-safe answers ON TOP of that log:
  `draft_acceptance()` (drafts relayed as the answer vs. offered), `unterminated_invocations()`
  (R12 invariant — every invocation must log exactly one terminal outcome), `_outcome_counts()`.
  All three correctly restrict to real user prompts (drop `session_id=unknown`/test-suite sessions),
  consistent with the CLAUDE.md lesson about the 54%-noise day.
- `groundtruth_traceability.py` + `trace_id.py` provide a pinned "v3 join contract" — a
  deterministic hash joins a route decision back to the prompt/session/turn that produced it, with
  legacy (pre-hash) rows explicitly flagged as "traceable but not joinable" rather than silently
  joined to the wrong thing.
- `counter_registry.py` is the closest thing to a single "ask the system" surface — it registers
  `draft_acceptance`, `low_signal_classifications`, the fail-open snapshot, and others behind one
  `doctor`/`status` command, each with a documented `source=` module.

**OBS-03 — a concrete, self-measured answer already exists and it's a bad one.**
`routing_report.draft_acceptance()`'s own docstring states the last real measurement, from
2026-09-23: **0 drafts used of 44 offered** (773 seconds of local-model time spent, ~18.7s of added
latency on the median prompt, for zero accepted answers). This is the system correctly answering
its own "did it replace premium work?" question — and the honest answer, on the most recent
recorded measurement, is no. Flagging for cross-reference with the docs-claims domain: any claim of
"routes to local models to save cost" needs to be read against this number, not against "DIRECT
SUCCESS" counts (which `draft_acceptance()`'s own docstring says measure *drafts produced*, not
drafts *used* — a distinction one prior incident already got wrong: a session whose log "showed
successful routing throughout" had actually driven subscription quota from 49% to 79% because every
draft was discarded).

### Duplicate telemetry — no single event model (OBS-01)

At least 16 independently-designed append-only stores were found under `~/.llm-router/`:

| Store | Written by | Notes |
|---|---|---|
| `savings_log.jsonl` | `cost.py`, `hooks/codex-post-tool.py`, `hooks/gemini-cli-post-tool.py`, `hooks/opencode-post-tool.py`, `hooks/session-end.py`, `hooks/usage-refresh.py`, `hooks/savings_logger.py` | **6 different modules each redefine their own `_savings_log_path()`/`_savings_log_file()`** instead of importing `cost.savings_log_path()`, the one the module docstrings call canonical. Two independent drainers (`cost.import_savings_log` async, `session-end.py::_sync_import_savings_log` sync) race on the same file — acknowledged in-code as "AC-5 (dual-writer race)" and mitigated with an atomic claim-file rename ("serializes drainers"), not eliminated. No test exercising the actual race was found in the §30 sample. |
| `model_tracking.jsonl` | `model_tracking.py`, `hooks/session-end.py`, `lineage/lineage_store.py` (as an explicit `legacy` fallback path) | Same fact (which model actually ran), three write paths, one marked legacy-in-progress. |
| `fail_open.jsonl` | `failopen.py` | Single writer, single schema, two readers (see §21) — the best-behaved store in this list. |
| `coverage.jsonl` | `coverage.py` | Capped at 50,000 events. |
| `attempts.jsonl` | `attempt_log.py` | Rotates at 5,000 lines, and the module's own docstring records a prior bug where rotation was an uncoordinated `read_text`/`write_text` pair. |
| `routing_lineage.jsonl` | `lineage/lineage_store.py` | |
| `trace.jsonl` | `trace.py` | Off by default (`LLM_ROUTER_TRACE`). |
| `intercepts.jsonl` | `hooks/tool_intercept.py` | |
| `direct_samples.jsonl` | `direct_diagnostics.py` | |
| `gt_accumulation.jsonl`, `prompt_capture.jsonl` | `prompt_capture.py` | |
| `quality_feedback.jsonl` | `quality_feedback.py` | |
| `routing_quality.jsonl` | `routing_quality.py` | |
| `community_export.jsonl` | `community.py` | |
| `session_context_<id>.jsonl` (many, per-session) | `session_store.py` | |
| `auto-route-debug.log` | `hooks/auto-route.py` | Prose, not JSONL; parsed back as data by `routing_report.py` — see §34. |

This is squarely the §9/§33 "semantic duplication of telemetry" case the brief asks about: not one
canonical event model with typed readers, but ~16 stores each invented for the feature that needed
them, with at least one (`savings_log.jsonl`) genuinely dual-written from six places and one
(`model_tracking.jsonl`) carrying an explicit in-progress legacy migration. A consolidation
candidate, not a deletion candidate — several of these encode information (session-scoped context,
quality feedback) that has no other home.

---

## §34 — Logging

- **Logs as program state, confirmed**: `routing_report.py`'s `draft_acceptance()`,
  `unterminated_invocations()`, and `_outcome_counts()` all parse `auto-route-debug.log` with
  `open(...).readlines()` / line-by-line string matching (`"DRAFT UNUSED:" in line`, etc.) as their
  only data source — there is no structured counter incremented at decision time for any of these
  three; the prose log **is** the ground truth they compute from. The module is explicit and
  self-aware about this ("counted from the log rather than a new writer... a second writer is a
  second thing that can disagree"), and its counters correctly exclude test-suite noise — this is
  the *good* version of logs-as-state, done with the CLAUDE.md lesson already applied. It is still
  logs-as-state: a change to the debug-log line format anywhere in `auto-route.py` silently changes
  what these functions can see, and nothing in the §30 sample pins the log-line format itself as a
  contract independent of the parser (no test asserting "the string `DRAFT UNUSED:` appears
  verbatim at this call site" the way `test_direct_execution_blocklist_coverage.py` pins
  `_BLOCKED_COMMANDS` against `SECURITY.md`).
- **No rotation on the store that matters most (OBS-02)**: `_debug_log()` in `hooks/auto-route.py`
  opens `auto-route-debug.log` with plain `open(path, "a")` on every hook invocation, with no size
  cap and no rotation logic anywhere in the file (checked for `rotate`/`MAX_.*LOG`/`truncate` near
  the write site — none found). This is inconsistent with every sibling JSONL store that was built
  more recently: `coverage.jsonl` caps at 50,000 events, `fail_open.jsonl` at 20,000, and
  `attempts.jsonl` rotates at 5,000 lines (after a documented earlier bug in doing that rotation
  safely). The one log three separate counters treat as their database has no such bound.
- **PII/secrets, spot-checked**: `_debug_log()` calls only ever pass `prompt_len=<int>` and
  `session_id=<first 8 chars>` — not prompt content — so the file that grows unboundedly is at
  least not accumulating raw user text. Where prompt content *is* persisted (`session_store.py`
  line ~2119, `_scrub_secrets_text(prompt.strip())`; another site at ~4690 tagged `CHZ-ST-006`,
  `_scrub_secrets_text(prompt[:4096])`), both call sites route through the shared
  `secret_scrubber.scrub_text`, consistent with `tests/security/test_m07_no_second_scrubber.py`'s
  existence (pins that only one scrubber implementation exists — not independently re-verified here
  beyond confirming both call sites import the same function).
- **Rotation elsewhere is deliberate, not accidental**: `attempt_log.py`'s docstring records that an
  earlier `_rotate` was "a `read_text`/`write_text` pair with no coordination" — i.e., this repo has
  already been bitten by naive log rotation once and fixed it for that file. The fix was not
  propagated to `auto-route-debug.log`, which has no rotation to get wrong yet, but also none to
  protect it.

---

## Findings register

```
ID: TST-01
Category: Test isolation / false-positive guard
Severity: HIGH
Confidence: HIGH (reproduced live, twice, with a controlled A/B)
Location: Files: tests/conftest.py (`_no_module_state_leak`, ~L1384-1447; `importing_a_submodule`,
  ~L1353-1381); src/llm_router/{ui,hooks,policies,static,rules,commands}/ (no __init__.py in any).
  Symbols: _no_module_state_leak, _module_state_fingerprint, _LEAK_WATCHED_PACKAGES.
Observation: Any test that is first-in-process to `import llm_router.<one of six namespace
  packages>.<submodule>` gets a spurious ERROR at its own teardown: "this test left a fileless
  module stub on a package object," because a namespace package's `__file__` is genuinely None,
  which the guard's heuristic cannot tell apart from a mock.
Evidence: `pytest tests/test_first_forty_w2.py -q` alone -> ERROR at teardown of
  test_quota_absent_is_not_reported_as_zero, message quoted above. `pytest
  tests/test_session_summary_xaxis.py tests/test_first_forty_w2.py -q` (a file that imports
  llm_router.ui at collection time, run first) -> passes clean, same test file, same code.
Why this exists, if discoverable: `_LEAK_WATCHED_PACKAGES` and the fixture were built to catch a
  real incident (T-01: a fake submodule stub survived a monkeypatch and masked 8 failures). The fix
  generalized "no __file__" as "is a stub," which is true for mocks but also true for every
  namespace package, and `src/llm_router` ships six of them.
Why this matters: The suite's pass/fail signal for these files depends on pytest collection order
  and on unrelated files' import statements. Investigating a reported single-file failure by running
  it alone — the obvious first move — reproduces a failure that is an artifact of isolation, not of
  the code, and will not reproduce in the full run. `test_s7_rendered_output_is_rendered.py`
  already had to work around this once with a bespoke fixture (`importing_a_submodule`); the fix
  exists but is opt-in per test rather than structural.
User-visible impact: None directly (CI presumably runs the full suite). Contributor/CI-triage impact
  is real: false-positive isolation failures cost investigation time and teach people to distrust or
  route around a guard that is otherwise doing real work.
Engineering impact: Wastes debugging time on a real signal that looks structural but isn't; risks a
  future contributor "fixing" the false positive by weakening or deleting the T-01 guard entirely,
  which would silently reopen the actual incident it was built for.
Is behavior currently used? YES — the guard runs on every test via autouse.
Recommended action: SIMPLIFY. Add empty `__init__.py` files to the six namespace packages (`ui`,
  `hooks`, `policies`, `static`, `rules`, `commands`) — this is a near-zero-risk, single-purpose fix
  that makes their `__file__` non-None and removes the ambiguity at the source, rather than teaching
  every test file to opt into `importing_a_submodule`. (Adding `__init__.py` to `hooks/` needs a
  check that nothing depends on it being importable as a true namespace package across multiple
  install locations — not verified here; flag as the one thing to confirm before landing.)
Proposed target: `src/llm_router/{ui,hooks,policies,static,rules,commands}/__init__.py` (empty or
  with existing re-exports if any already exist elsewhere).
Behavioral compatibility risk: LOW — converting a namespace package to a regular package is usually
  transparent; verify no code relies on `pkgutil`-style namespace-package merge semantics for these
  six directories specifically (not checked here).
Security risk: None.
Performance impact: None.
Estimated complexity removed: Removes an entire class of order-dependent test flakiness; deletes the
  need for `importing_a_submodule` as a per-test opt-in (could become the default cleanup once the
  root cause is gone).
Validation required: Add `__init__.py` to one package (e.g. `ui`) and re-run the exact A/B in this
  finding to confirm the spurious ERROR disappears; then do the rest.
Dependencies on other findings: None.
```

```
ID: ERR-01
Category: Error handling — silent failure population
Severity: HIGH
Confidence: HIGH (AST-measured, not grepped)
Location: Files: all of src/llm_router (1,020 ExceptHandler nodes across 203 files);
  src/llm_router/failopen.py (the accounting layer).
Observation: Of 1,020 broad `except`/`except Exception`/`except (..., Exception, ...)` handlers in
  `src/`, 270 (26.5%) have a body of exactly `pass` — no log call, no comment-referenced ticket, no
  accounting call, nothing. Only 61 (6.0%) directly call `failopen.record(...)` inside the handler
  body; the grep-wide total of `failopen.record(` call sites across the whole tree is 162, which
  even crediting every indirect call (helper functions invoked from an except block) cannot cover
  more than roughly a sixth of the 1,020 handlers.
Evidence: `/tmp/except_scan.py` (AST walk, methodology described in §21) run against the worktree;
  raw counts quoted above are its stdout. failopen.py's own docstring independently states "the
  codebase carries ~810 broad except Exception handlers," corroborating the order of magnitude from
  a different starting count (excludes bare `except:`).
Why this exists, if discoverable: failopen.py was explicitly scoped ("in the money, routing,
  verification and telemetry paths") rather than rolled out to every broad except in the codebase;
  the scoping decision is reasonable, but nothing tracks or bounds how much of the *unscoped*
  remainder is silent versus merely logged-and-swallowed.
Why this matters: A caught exception with no log and no accounting is, by failopen.py's own stated
  design principle ("a caught exception is information; discarding it converts a known failure into
  an unknown one"), exactly the failure mode the tool exists to close — for the majority of the
  codebase's broad excepts, it has not yet been applied.
User-visible impact: Degraded behavior in an unrolled-out path (a feature quietly not working) has
  no operator-visible signal at all — no doctor line, no log line, no counter.
Engineering impact: Every future "why isn't X happening" investigation into one of the un-instrumented
  760-ish handlers starts from zero, the same starting point CLAUDE.md's own incident log (2026-09-13,
  routing-stopped-working) describes costing a full day twice.
Is behavior currently used? UNCERTAIN — cannot tell from static analysis whether the un-instrumented
  handlers fire often, rarely, or never in practice; that is exactly what accounting would answer.
Recommended action: KEEP failopen.py as-is; SIMPLIFY the rollout question into a measurable one —
  a ratchet test (same shape as R13/S9b) that counts pass-only broad excepts in the "money, routing,
  verification, telemetry" modules specifically (not all of src/) and pins the count, so it can only
  go down, the same pattern already used successfully elsewhere in this repo.
Proposed target: a new `tests/test_<n>_failopen_coverage_ratchet.py` scoped to the module list
  failopen.py's own docstring already names.
Behavioral compatibility risk: None (a new test, no production code change implied by this finding
  alone).
Security risk: LOW-MEDIUM — a silently-swallowed exception in an unaudited path could mask a
  security-relevant failure (e.g., a redaction step that silently no-ops); not confirmed for any
  specific handler here, flagged as a class risk.
Performance impact: None.
Estimated complexity removed: N/A (this is a visibility gap, not excess complexity).
Validation required: Sample 15-20 of the 270 pass-only handlers by hand (as S9b's lint did) to get a
  real/plausible/intentional/benign precision estimate before deciding how aggressively to
  instrument them — do not assume all 270 are bugs.
Dependencies on other findings: None.
```

```
ID: ERR-02
Category: Observability / operator-facing truncation
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/counter_registry.py:121-124; src/llm_router/ui/status_premium.py:243-247.
Observation: Both production readers of the fail-open registry call `render_report(limit=8)` /
  `limit=4)` respectively. With 59 distinct registered codes, `doctor` shows at most the top 8 by
  volume and `status` the top 4.
Evidence: grep for `.render_report(` call sites (2 total, both with an explicit `limit=`); code
  itself (`FailOpenCounts.render_report`, failopen.py:110-137) sorts by count descending and appends
  "... N more site(s)" — so under-reporting is at least honestly disclosed, not hidden.
Why this matters: A code that fires once but represents a serious degradation (e.g., a cost-cap
  ledger read failure) can be permanently outranked in the default view by a noisy, benign one,
  and the only way to see it is to read fail_open.jsonl by hand — which defeats the point of having
  a registry with a doctor command.
User-visible impact: An operator running `llm-router doctor` after a real incident may see nothing
  alarming even though the store recorded it.
Engineering impact: Low — the fix is small (surface severity or recency, not just volume, or raise
  the limit / add a `--all` flag).
Is behavior currently used? YES.
Recommended action: SIMPLIFY — sort by (is this code new since last read, then volume) rather than
  volume alone, or add an explicit "N codes not shown, run `doctor --failopen-all`" flag.
Proposed target: FailOpenCounts.render_report / its two call sites.
Behavioral compatibility risk: LOW.
Security risk: None.
Performance impact: None.
Estimated complexity removed: N/A.
Validation required: none beyond code review; low-risk change.
Dependencies on other findings: None.
```

```
ID: OBS-01
Category: Observability — duplicate telemetry / no single event model
Severity: HIGH
Confidence: HIGH
Location: Files: src/llm_router/cost.py, hooks/codex-post-tool.py, hooks/gemini-cli-post-tool.py,
  hooks/opencode-post-tool.py, hooks/session-end.py, hooks/usage-refresh.py, hooks/savings_logger.py
  (all define their own `_savings_log_path`/`_savings_log_file`); model_tracking.py,
  hooks/session-end.py, lineage/lineage_store.py (three writers of model_tracking.jsonl, one marked
  `legacy`); plus 12 further independent JSONL/log stores enumerated in §33's table.
Observation: `savings_log.jsonl` is written by 6 different modules, each of which re-implements its
  own path-lookup function instead of importing the one `cost.py` calls canonical
  (`cost.savings_log_path()`). Two independent drainers race on the file — the code's own comments
  name this "AC-5 (dual-writer race)" and describe a mitigation (atomic claim-file rename to
  serialize drainers), which reduces but does not eliminate the two-writer structure. No test
  exercising the actual concurrent-drain race was found in the §30 stratified sample.
Evidence: grep across src/ for `_savings_log_path\|_savings_log_file\|savings_log.jsonl` (7 distinct
  definitions found, quoted in §33); grep for "AC-5" (2 hits, cost.py:3382 and
  hooks/session-end.py:340, both describing the same race from each side).
Why this exists, if discoverable: each host integration (Codex, Gemini CLI, opencode, Claude Code)
  was added incrementally and each needed to flush savings data before the shared drainer ran; the
  path helper was copy-pasted forward each time rather than factored out.
Why this matters: this is precisely the §9/§33 "semantic duplication of telemetry" case — one fact
  (savings realized) has 6 write sites and 2 read/drain sites, all agreeing on a filename by
  convention rather than by import, so a future rename or path change (e.g. moving under
  LLM_ROUTER_HOME versioning) has to be made correctly in 7 places to stay consistent.
User-visible impact: None observed directly; the risk is silent divergence (one hook writes a
  slightly different schema) rather than an outage.
Engineering impact: High carrying cost for a "just add a host" change — a new host integration author
  has 6 existing examples to copy from, none of which import the canonical helper, so the pattern
  self-perpetuates.
Is behavior currently used? YES — this is live, per-turn-invoked code across 4 host integrations.
Recommended action: MERGE — every hook-local `_savings_log_path()`/`_savings_log_file()` should
  import `llm_router.cost.savings_log_path()` instead of redefining it. This is a pure DELETE > REUSE
  move with no behavior change (same filename, same directory resolution logic, assuming the 6
  redefinitions are in fact equivalent — verify each before merging, do not assume).
Proposed target: `llm_router.cost.savings_log_path()` as the sole definition; 6 call-site updates.
Behavioral compatibility risk: LOW if the 6 definitions are truly equivalent (spot-checked 3 of the
  6 here — codex-post-tool.py, gemini-cli-post-tool.py, opencode-post-tool.py all read `_state_dir() /
  "savings_log.jsonl"` identically); MEDIUM if session-end.py's or usage-refresh.py's differ in any
  env-var override behavior — not fully diffed here.
Security risk: None.
Performance impact: None.
Estimated complexity removed: 6 duplicate function definitions; one less "which one is canonical"
  question for the next host integration.
Validation required: diff all 6 definitions byte-for-byte for behavioral equivalence before merging;
  add the concurrency test for AC-5 that appears to be missing.
Dependencies on other findings: None.
```

```
ID: OBS-02
Category: Logging — unbounded log used as program state
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/hooks/auto-route.py (`_debug_log`, ~L3293-3314; `_debug_log_path`,
  ~L3177-3196); src/llm_router/routing_report.py (`draft_acceptance`, `unterminated_invocations`,
  `_outcome_counts`, all parsing the same file).
Observation: `_debug_log()` appends to `auto-route-debug.log` on every hook invocation via plain
  `open(path, "a")`, with no size cap and no rotation logic in the file. Three separate counters in
  routing_report.py treat this file as their sole data source via line-by-line string matching.
Evidence: grep for `rotate|MAX_.*LOG|truncate` in auto-route.py near the write site returns nothing
  relevant; contrast with coverage.py (`_MAX_EVENTS = 50_000`), failopen.py (`_MAX_EVENTS = 20_000`),
  attempt_log.py (`_MAX_LINES = 5000`, with a documented history of a prior uncoordinated-rotation
  bug it since fixed).
Why this exists, if discoverable: the debug log predates the JSONL stores that were built with size
  caps as a design habit; nobody has gone back to retrofit the same discipline onto it, likely
  because CLAUDE.md's own remediation effort focused on *reading* the log correctly (denominators,
  excluding test noise) rather than its lifecycle.
Why this matters: the file that at least three counters (and, per CLAUDE.md, at least one manual
  incident investigation) treat as ground truth grows without bound for the life of an install; every
  read of it (by these counters or by a human) gets slower over time, and there is no policy for how
  much history it's supposed to retain.
User-visible impact: None acute; a slow-growing disk/IO cost and slower `doctor`/report generation
  on long-lived installs.
Engineering impact: Medium — anyone adding a fourth log-derived counter has no established retention
  contract to design against.
Is behavior currently used? YES.
Recommended action: SIMPLIFY — apply the same rotation discipline `attempt_log.py` already uses
  (line-count cap with a coordinated, tested `_rotate`) to `auto-route-debug.log`.
Proposed target: hooks/auto-route.py `_debug_log()` / `_debug_log_path()`.
Behavioral compatibility risk: LOW-MEDIUM — routing_report.py's counters read the whole file; a
  rotation policy needs to either preserve enough history for those counters' typical windows or the
  counters need to be told about the rotation boundary (attempt_log.py's own docstring describes
  getting this wrong once already — reuse its fixed approach rather than reinventing).
Security risk: None (content is length/session-id-prefix only, not raw prompt text — verified).
Performance impact: Positive once fixed (bounded file size).
Estimated complexity removed: N/A — this is a missing safeguard, not excess complexity.
Validation required: confirm routing_report.py's three counters still produce sane numbers across a
  rotation boundary before shipping.
Dependencies on other findings: None.
```

```
ID: OBS-03
Category: Observability — self-measured capability result
Severity: INFORMATIONAL (feeds domain-12 / claims cross-reference)
Confidence: HIGH (quoting the code's own recorded measurement, not re-measuring)
Location: Files: src/llm_router/routing_report.py:155-190 (`draft_acceptance`).
Observation: The function's own docstring states its last real measurement: "Measured on this
  machine 2026-09-23: 0 used of 44 offered, for 773 seconds of local model time and ~18.7s of added
  latency on the median prompt." I.e., the system's own instrumentation, asked "did a local draft
  ever replace premium work," currently answers "no, zero times, on the last day it was measured."
Evidence: as quoted, in-repo, dated, with numerator/denominator/window per the CLAUDE.md discipline
  this repo otherwise enforces on itself.
Why this matters: any README/marketing claim of routing-to-local-models-saves-cost needs to be read
  against *this* number (drafts actually used), not against "DIRECT SUCCESS" counts, which the same
  docstring says measure drafts *produced* — a distinction one real incident already got backwards
  (a session that "showed successful routing throughout" while burning 49%->79% of subscription
  quota, because every draft was discarded).
User-visible impact: N/A directly — this is a finding for the claims/README domain, not a code
  defect in itself. The function that answers this question exists, is honest, and is wired into
  `doctor` via counter_registry.py:322 (`id="draft_acceptance"`) — so this is not a hidden metric.
Engineering impact: None.
Is behavior currently used? YES — `draft_acceptance()` is a registered, readable counter.
Recommended action: KEEP (the measurement mechanism). Flag for domain-12/claims-ledger
  cross-reference: any "local-first" or "saves cost via local drafts" claim should cite this counter
  or an updated run of it, not a DIRECT-success rate.
Proposed target: N/A (no code change recommended here).
Behavioral compatibility risk: None.
Security risk: None.
Performance impact: None.
Estimated complexity removed: N/A.
Validation required: re-run `draft_acceptance()` against current data before any synthesis report
  quotes the 0/44 figure as still current — it is dated 2026-09-23 in the docstring, one day before
  this baseline.
Dependencies on other findings: cross-reference with domain 12 (docs/claims).
```

```
ID: TST-05
Category: Test design — implementation-coupled assertions
Severity: LOW
Confidence: MEDIUM
Location: Files: tests/test_session_context_wiring.py (325 lines, ~60 mock references),
  tests/test_context_capture_hook.py (252 lines, ~41 mock references).
Observation: A subset of assertions in these files check the *shape* of a mocked call
  (`test_builds_and_threads_session_context_into_execute_chain`, `test_successful_record_event_call_shape`)
  rather than an externally observable outcome. Both files also contain genuinely valuable,
  non-coupled tests in the same suite (fail-open behavior under a raising `record_event`, for
  instance), so this is a partial classification, not a blanket one.
Evidence: mock-reference density (grep count) relative to file length and to assertion count, cross-
  checked against the actual test names in the summarized output (§30 methodology).
Why this matters: a call-shape assertion passes as long as the mock is invoked with the expected
  arguments, even if the real integration point it stands in for has since changed shape in a way
  that would break in production — it protects against one specific class of regression (accidental
  removal of the call) but not against "the call happens but does the wrong thing downstream."
User-visible impact: None directly; a false sense of coverage for the wiring these files describe.
Engineering impact: Low-medium; makes a future refactor of the session-context wiring path riskier
  than the test file count would suggest.
Is behavior currently used? YES.
Recommended action: KEEP the fail-open/behavioral tests in these files; SIMPLIFY by converting the
  call-shape assertions to assert on the actual constructed context/session-store content where
  feasible (some of this file's own sibling tests already do this correctly, e.g.
  `test_empty_context_is_passed_through_not_fabricated`).
Proposed target: the specific call-shape assertions named above.
Behavioral compatibility risk: None (test-only change).
Security risk: None.
Performance impact: None.
Estimated complexity removed: N/A.
Validation required: N/A.
Dependencies on other findings: None.
```

---

## Top items for synthesis

1. **TST-01** (HIGH) — reproducible false-positive test-isolation failure on namespace packages
   (`ui`, `hooks`, `policies`, `static`, `rules`, `commands`); fix is a near-zero-risk
   `__init__.py` addition to six directories. Best candidate in this domain for the global
   Top-10 correctness-risk list, because it actively misleads triage.
2. **ERR-01** (HIGH) — only ~6-16% of 1,020 broad-except handlers in `src/` are covered by the
   fail-open accounting system that exists specifically to close this gap; 270 are bare `pass`.
   Candidate for the global Top-10 "testing/observability problems" and for a scoped ratchet test
   (pattern already proven twice in this repo: R13, S9b).
3. **OBS-01** (HIGH) — `savings_log.jsonl` written by 6 independently-implemented path helpers
   across 4 host integrations, with an acknowledged-but-not-eliminated dual-writer race (AC-5).
   Best candidate for the consolidation ledger: MERGE the 6 definitions into the one canonical
   `cost.savings_log_path()` import — pure DELETE > REUSE, verify equivalence first.
4. **OBS-02** (MEDIUM) — `auto-route-debug.log` is the one telemetry store this repo relies on
   most (3 counters parse it, plus documented manual-investigation use) and the only one of five
   comparable stores with no rotation/cap. Small, well-precedented fix (reuse `attempt_log.py`'s
   already-fixed rotation approach).
5. **Do-not-change candidate**: the `_quarantined_tests/` triage process itself (README.md +
   dated TRIAGE docs + git-history-verified resolution pattern). It is actively working — 6 of 15
   files resolved correctly in two commits, one of which found a real "shipped but unimportable
   module" bug that a naive "delete stale-looking tests" pass would have destroyed evidence of.
   Any global simplification pass should preserve this process, not fold it away as clutter.
6. **Do-not-change candidate**: `failopen.py`'s design (never-raises, unknown-is-not-zero,
   unpersisted-loss counter, WARNING-not-DEBUG on its own write failure). Well-reasoned, already
   fixed once (T-07) from a worse state; the gap is rollout breadth (ERR-01), not design.
7. **OBS-03** (informational, cross-domain) — `draft_acceptance()` = 0/44 on 2026-09-23 is the
   system's own honest answer to "did local routing replace premium work" on its last measured
   day. Route to domain 12 for README/claims cross-reference before any savings claim ships.
8. **TST classification headline for synthesis**: in a 73-file stratified sample, 0 files were
   DUPLICATE/LOW-VALUE/OBSOLETE; 27 CRITICAL CONTRACT, 22 HIGH-VALUE REGRESSION, 22 USEFUL UNIT, 2
   IMPLEMENTATION-COUPLED. This suite is a genuine asset, not carrying cost — weigh this against any
   proposal in other domains to aggressively prune `tests/` for LOC reduction; the LOC is mostly
   docstring-explained regression coverage, not padding.
9. **ERR-02** (MEDIUM) — doctor/status truncate the 59-code fail-open registry to top-8/top-4 by
   volume; a rare severe code is invisible by design. Small fix, real gap.
10. **R13 ratchet baseline for the record**: 1 source-text assertion remains (down from 62),
    verified by running the actual detector, and the 1 survivor is deliberately documented. Use
    this exact number (not the 193 a naive grep produces) if any other domain cites it.
