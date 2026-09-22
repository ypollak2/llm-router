# Test and verification plan

v14.1.0 · 2026-09-21 audit (commit `032b473`) → this plan. Companion to
`REMEDIATION_PLAN.md`; phases below are that document's phases, not a new
sequence. Every file path, line number and symbol cited here was re-verified
against the working tree while writing this plan (commands and outputs kept in
the session, not reproduced here to keep this document a plan and not a log).

## The principle

A test that cannot fail today proves nothing about a defect that exists today.
Every entry below starts from a **red test**: written against current HEAD, it
fails, and the failure message is the finding restated as an assertion. A
"fix-verification" test is that same assertion, inverted, run after the fix
lands — not a new test invented after the fact to make the fix look good. Where
a finding genuinely cannot be reproduced as a unit test (an absence, like H-08,
or a claim about prose, like H-02), the entry says what stands in for "red"
instead of forcing a shape that doesn't fit.

Nothing in this document is implemented. Per the audit brief that produced it,
this is a plan, not a diff.

---

## Ordering and dependencies

| Can write now (no fix needed) | Needs a Phase 1 API first | Needs a Phase 4 decision first |
|---|---|---|
| C-01, C-02 (repro), C-03, C-04, H-01, H-04, H-05, H-06, H-07, H-09, H-10, M-01, M-02, M-03, M-05, M-06, M-07, M-09, M-10, M-11, M-12, all L | C-02 fix-verify (needs `is_simulated` written at insert), C-02 acceptance (needs Phase 2 clean window) | H-08 fix-verify (Option A replay API doesn't exist yet; Option B narrows `eligibility.assess()`, also undesigned) |

Everything in the left column can be written this week, against today's HEAD,
and should be — a red test is itself a more precise bug report than the audit
prose. The middle and right columns are **fix-verification** tests whose
assertion target doesn't exist yet: `is_simulated` is a column nobody writes,
and the replay function in Option A has no name yet. Writing those tests early
would mean asserting against an imagined signature that the real fix will not
match. Draft them as pseudocode next to the acceptance criteria in
`REMEDIATION_PLAN.md` §1.3 and §4, and convert to real pytest once the PR that
adds the function is up for review — not before.

Two explicit dependency chains worth naming because they gate a lot of tests
below, not just their own finding:

- **C-01 before H-01, H-09, M-03, M-01.** All four read or write the same
  ledger row shape (`RouteLedgerRecord` / `route_outcome`). Writing their tests
  against today's one-state ledger is still valuable (they fail for a documented
  reason), but their fix-verification tests should be written once, after C-01,
  against the three-state ledger — not once per finding against a shape that is
  about to change.
- **Isolation (M-04) before all of it.** See the next section. A test that
  believes it is sandboxed and is not doesn't fail loud — it silently writes to
  the developer's real `~/.llm-router`. `src/llm_router/paths.py`'s own
  docstring names an incident (`evidence/AUDITOR_INCIDENT.md`, not present in
  this checkout — referenced but not found; confirm it exists before citing it
  as evidence in a PR) where exactly that happened. Every test spec below names
  the specific env var or monkeypatch target that isolates it, because "isolated
  by default" is false for most of the modules in scope.

---

## Denominator and vacuity: standing rules for every test in this plan

This repository's own `CLAUDE.md` and the audit's `TEST_GAP_ANALYSIS.md` agree
on the failure mode that keeps recurring: a check that reports "clean" because
it checked nothing. Every test below must satisfy these, and the per-finding
"vacuity guard" field says which one applies:

1. **Print N.** Any test that computes a rate, a count or an aggregate prints
   the population size it computed over, in the assertion message, not only on
   success. A test that silently passes on `n=0` is worse than no test.
2. **Assert the fixture set is non-empty before asserting anything about its
   contents.** `assert len(fixture_rows) >= K` before the real assertion, where
   K is a number the vacuity guard justifies (usually "enough that the effect
   being measured is not noise" — see `CLAUDE.md`'s own ~50-prompt floor for
   rate measurements).
3. **A filter test needs a known positive.** If the test proves "X excludes Y",
   it must first prove the filter *can* exclude something, by running it against
   a row built to be excluded. `M-06`'s own history (`test_m06`) is exactly this
   shape: don't just prove no cold-start lock reproduces; prove the test harness
   can produce contention at all (M-05/M-06 concurrency probes) before trusting
   its zero.
4. **A mutation, not just an assertion, for anything safety-critical.** Where a
   finding sits on the routing-decision or money path, prefer specifying a
   mutation probe via `scripts/groundtruth/mutants.py`'s pattern over another
   hand-written assertion — this is the one mechanism in the repo already proven
   to catch what it claims to catch (6/6 in the audit's own re-run).
5. **State the exact assertion that fails today**, not "the test currently
   fails" — a future reader needs to know whether the fix landed by reading the
   assertion, not by re-running history.

---

## Test infrastructure gaps

This is the load-bearing section. Most of this plan is inert without it.

### Isolation is not one fixture — it's per-module

`tests/conftest.py::_isolate_llm_router_writes` (autouse) sets
`LLM_ROUTER_EXECUTION_LEDGER_DB`, `LLM_ROUTER_HEALTH_SNAPSHOT` and
`LLM_ROUTER_HOME` for every test. That covers the execution ledger and the
health snapshot. It does **not** cover most of the stores this plan needs to
write to, because those modules never read `LLM_ROUTER_HOME` — confirmed by
reading each module directly, not by trusting the audit's count:

| Store | Resolves via | Honors `LLM_ROUTER_HOME`? | Isolate with |
|---|---|---|---|
| `routing_quality.jsonl` | `routing_quality.py:252` — its own `LLM_ROUTER_ROUTING_LEDGER` env var, falling back to `Path.home()/.llm-router` | **No** | `monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / "rq.jsonl"))` |
| `usage.db` | `cost.py` — reads `LLM_ROUTER_DB_PATH`; `paths.state_path()` exists but `cost.py` has its own resolver | Partially — via a different var | `monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))`, per the pattern already in `cost.py:1303/1311`'s own error message |
| `usage.json` (quota) | `quota_tracker.py:84` — `USAGE_JSON: Path = Path.home() / ".llm-router" / "usage.json"`, a **class attribute evaluated at import time** | **No — no env var read at all** | `monkeypatch.setattr(QuotaTracker, "USAGE_JSON", tmp_path / "usage.json")` on the class, not `Path.home` |
| `ground_truth_candidates.jsonl` | `scripts/groundtruth/pool.py:140` — `os.environ.get("LLM_ROUTER_HOME", ...)`, read at call time | **Yes** | `monkeypatch.setenv("LLM_ROUTER_HOME", ...)` works here |
| `trace.jsonl` / `intercepts.jsonl` | `trace.py`, `hooks/tool_intercept.py` — need to confirm at test-authoring time; not yet checked against `paths.py` | **Unconfirmed — check before writing C-04's test** | — |

The `quota_tracker.py:84` case is the dangerous one for this plan specifically:
it is a **class-level default** (`Path.home() / ".llm-router" / "usage.json"`),
so `monkeypatch.setenv` does nothing — the attribute was already resolved at
import. H-06's test must patch the class attribute directly, or it will pass by
accident against a tmp value on some runs and silently touch the real file on
others, which is precisely the RED2-07 incident `paths.py` documents.

**Recommendation:** before writing any test in this plan, run
`grep -n "Path.home()" src/llm_router/<module>.py` for the specific module the
test touches, and confirm whether the resolution is a function (safe to
monkeypatch the env var) or a class/module-level default (must patch the
attribute). Do not assume the autouse fixture covers a store just because the
test passes — that is exactly how 7 synthetic rows reached the real ledger
during development (M-04's own evidence).

### What doesn't exist yet and blocks specific tests

- **A fixture that forces every model in the chain to fail.** `test_a01_attempt_failed_ledger.py`
  has the shape (a `fake_call_llm` that raises for the first N calls, then
  succeeds) but is tuned to test *escalation*, not *terminal failure* — it lets
  the last call succeed. C-01's reproduction test needs the inverse: every model
  in the resolved chain raises, driving execution to `router.py:3420`
  (`_emit_ledger_terminal(correlation_id, "failed", route_succeeded=False)`).
  This plan needs a new shared fixture,
  `tests/conftest.py::all_models_fail` or local to
  `tests/telemetry/test_crit01_ledger_records_failure.py`, that raises for every
  model name the resolved chain contains rather than a fixed count.
- **A frozen embedding fixture for C-03.** The three collision pairs need real
  `nomic-embed-text` vectors to test the cache's threshold logic without a live
  Ollama dependency (see C-03 below for why this matters for default-run
  coverage). Nothing in `tests/fixtures/` currently holds embedding vectors.
  This plan needs one JSON fixture file with the six precomputed vectors
  (3 pairs) captured once from a real Ollama run, checked into
  `tests/fixtures/semantic_cache_collision_vectors.json`.
- **A clean-git-tree fixture for H-08.** `tests/e2e/conftest.py` builds real
  Ollama/HTTP fixtures but nothing in the tree currently builds a disposable git
  repo with a captured commit + patch for `envelope.py` to describe. H-08's test
  needs a `tmp_path`-scoped `git init` + one commit + one uncommitted diff,
  which is cheap and hermetic (no network) but does not exist as a shared
  fixture today.
- **A shared secrets fixture.** `tests/security/test_agentic_env_isolation.py`
  defines `REAL_LOOKING_SECRETS` locally. C-04's test needs the same six
  patterns the audit measured (Anthropic key, AWS key id, `password=`, a home
  path, an email, a public IP) — reuse that set rather than inventing a second
  one; promoting it to `tests/fixtures/secrets.py` is worth doing once two
  files need it, which this plan makes true.

---

## Phase 0 — Containment

Tests that prove the defect and, where the fix is "stop doing X" rather than
"build Y," can also serve as the fix-verification test with no new writing.

### C-02 · Reported savings have the wrong sign — reproduction half only

*Full spec under Phase 1, where the fix lands. Phase 0's job is narrower: prove
the number is currently wrong, and prove withdrawing it is what happened.*

- **Reproduction test:** `tests/economics/test_crit02_savings_sign_after_fixture_removal.py::test_savings_query_is_not_positive_once_stub_rows_are_removed`. Arrange: insert into an isolated `usage.db` (see infra table — `LLM_ROUTER_DB_PATH`) a population shaped like the audit's measured contamination: some real-priced rows, 1,813-shaped stub-signature rows carrying real model names (token counts that are round stub values, e.g. `input_tokens=10, output_tokens=10`), 11 rows with `test-key` in the error text, and rows on `badmodel`/`ollama/b`. Act: call `cost.get_savings_by_period()`. Assert: the raw reported figure is positive (this is the defect, not the fix target) **and** a hand-computed sum over only the rows that survive stub-signature + non-existent-model exclusion is negative — so the test documents both the reported number and the honest one in the same run.
- **Fix-verification test:** deferred to Phase 1 (needs `is_simulated` written at insert time — see there).
- **Use case:** a user runs a normal session that includes the project's own test suite in the same shell history llm-router observes; the session-end panel reports positive net savings driven by test traffic that never should have been priced at all.
- **Vacuity guard:** the fixture set explicitly includes at least one row that a correct filter *should* keep (a real, non-stub, real-model row with genuine savings) — so a fix that excludes everything and reports `$0.00` for the wrong reason (an empty population) is distinguishable from a fix that correctly separates real from synthetic.
- **Type / marker:** unit, `fast`. Runs by default.

### C-03 · The semantic cache returns answers to different questions

- **Reproduction test:** `tests/routing/test_crit03_semantic_cache_collision.py::test_measured_pairs_collide_at_default_threshold`. Arrange: monkeypatch `semantic_cache._get_embedding` to return the frozen vectors from `tests/fixtures/semantic_cache_collision_vectors.json` (see infra gap above) for the three measured prompt pairs. Act: `store()` the first prompt of each pair with a distinct canned response, then `check()` the second prompt at the shipped default (`DEFAULT_THRESHOLD = 0.95`, `semantic_cache.py:39`). Assert: `check()` returns a hit for all three pairs today — the assertion that must fail once the threshold is fixed is `assert result is None` for "retry 3 times" vs "retry 30 times", "timeout 30" vs "timeout 300", and "increase by 10%" vs "decrease by 10%".
- **Companion live test (not default-run):** `tests/e2e/test_crit03_semantic_cache_collision_live.py`, marked `[pytest.mark.e2e, pytest.mark.requires_ollama]` matching the existing e2e convention (`tests/e2e/test_e2e_gateway.py:13`). Recomputes the three cosine similarities against a real running Ollama and asserts they still match the frozen fixture within tolerance — this is what keeps the frozen-vector test honest as the embedding model or index drifts. Runs in a nightly/Ollama-equipped CI job only; **this is the honest answer to "how does a CRITICAL finding's guard run by default"** — the hermetic version does, the drift-detector doesn't need to.
- **Fix-verification test:** same hermetic test, inverted — after the threshold is raised (or the cache is disabled by default per REMEDIATION_PLAN 0.2), `check()` must return `None` for all three pairs, and a bypass call (`check(..., bypass=True)` or whatever the added API is named) must skip the cache entirely regardless of similarity.
- **Use case:** a user asks "retry the deploy 3 times" in one session and "retry the deploy 30 times" in a later one; the second gets the first's cached answer, silently wrong by a factor of 10, with no indication a cache was involved.
- **Vacuity guard:** a fourth, near-duplicate pair that **should** still collide even at a corrected threshold (e.g. "how do I retry a request" / "how can I retry a request", genuinely paraphrastic) is asserted as a hit in the same file — proves the fix didn't just raise the threshold to 1.01 and disable the cache's actual purpose.
- **Type / marker:** unit/routing for the hermetic test (`fast`, default-run); `e2e` + `requires_ollama` for the live drift check (not default-run, needs a nightly job — flag in coverage table).

### C-04 · Raw secrets to disk

- **Reproduction test:** `tests/security/test_crit04_secrets_bypass_scrubber.py`. Reuse `REAL_LOOKING_SECRETS`-shaped fixtures (Anthropic key, AWS key id, `password=...`, a home path, an email, a public IP — the six the audit injection-tested) from `tests/security/test_agentic_env_isolation.py`. Arrange: point `trace.py`'s `emit()` and `hooks/tool_intercept.py`'s `_log_intercept()` at an isolated tmp path (confirm the exact override var per the infra table before writing — not yet confirmed for these two modules). Act: emit/log an event containing each secret. Assert: `secret not in path.read_text()` — **fails today** for all six, since both modules have 0 scrubber references.
- **Additional reproduction:** `test_intercepts_file_is_not_world_readable` — assert the created file's mode is `0o600`. Fails today (mode `644`, confirmed against the live `intercepts.jsonl` during the audit).
- **Additional reproduction:** `test_gc_purges_trace_and_intercept_files` — grep-level: assert `commands/gc.py`'s source references `trace.jsonl` and `intercepts.jsonl` by name. Fails today (0 references, per the audit).
- **Fix-verification test:** all three inverted, once the scrubber is wired in, the private 0600 opener already used by `auto-route.py` is reused here, and `gc.py` gains the two file names.
- **Use case:** `LLM_ROUTER_BASH_INTERCEPT=1` is active (it was during this audit) and a developer runs `curl -H "Authorization: Bearer sk-..." ...`; the bearer token lands in `intercepts.jsonl`, mode 644, indefinitely, readable by any local account.
- **Vacuity guard:** the same test file includes a positive control — the same six secrets passed through a store that **does** call the scrubber correctly today (`result_cache` or `session_store`, per `SECURITY_PRIVACY_AUDIT.md`'s list of compliant stores) — and asserts they are redacted there. This proves the assertion mechanism can detect a present scrubber, not just complain about an absent one.
- **Type / marker:** unit/security, `fast`. Runs by default — no external services needed, only tmp files.

### M-07 · `error_sanitization.py` is an orphaned fourth scrubber

- **Test:** `tests/security/test_m07_error_sanitization_deleted_or_unreachable.py::test_error_sanitization_has_no_importers`. `grep -rL` for `error_sanitization` outside its own module and its own test file returns nothing that isn't a comment. **Fails today only if the deletion in REMEDIATION_PLAN 0.4 hasn't happened** — before that, this is a documentation test recording the orphan, not yet a red test (it currently *passes*, trivially, since "0 callers" is the finding itself, not a violation of a rule anyone has stated as a rule). Convert it to a real regression once deleted: assert `import error_sanitization` raises `ModuleNotFoundError`.
- **Vacuity guard:** N/A — this is the register case: a check with nothing to fail against yet. State that plainly rather than dressing it up as red.
- **Type / marker:** grep-based, `fast`, default-run.

### M-09 · 4.8 GB RouterArena checkout inside `~/.llm-router`

- **Test:** `tests/install/test_m09_routerarena_not_under_state_dir.py::test_state_dir_contains_no_vendored_harness`. Assert `paths.llm_router_home() / "harness"` (or wherever RouterArena currently lands — confirm the exact subpath before writing) does not exist, or if present is a symlink outside the directory the `gc`/backup tooling walks. Fails today against the real `~/.llm-router` (4.8 of 5.0 GB) — **but this test must run against an isolated tmp state dir**, so it is really testing "does the installer/first-run code ever write RouterArena there," not asserting against the developer's real disk. Reproduce by running the relevant install/onboarding path in a tmp `LLM_ROUTER_HOME` and asserting nothing named `RouterArena` gets created.
- **Vacuity guard:** confirm the test actually exercises the code path that creates the checkout (find the call site first — not yet located) rather than asserting on an empty tmp dir where the harness was never going to be created anyway. This is exactly rule 3's "prove the filter can catch something" applied to an install-time check.
- **Type / marker:** `install`, `fast` if hermetic; `slow` if it must actually clone/copy something to trigger the path — confirm before assigning.

### M-11 · `llm-router status` crashes on a clean install

- **Test:** `tests/install/test_m11_status_command_clean_install.py::test_status_runs_in_a_venv_without_rich`. Arrange: a subprocess venv with the package installed from the built wheel (or `pip install -e .`) and explicitly **without** `rich` (confirmed not in `dependencies` in `pyproject.toml`). Act: run `llm-router status`. Assert: exit code 0, not `ModuleNotFoundError: rich`.
- **Fix-verification:** same test, after `rich` is added to `dependencies` (REMEDIATION_PLAN 0.5).
- **Use case:** the flagship "how much have I saved" command, first thing a new user runs.
- **Vacuity guard:** the test must build a *genuinely* clean venv (no rich in site-packages) — a venv that inherits the developer's global site-packages would pass vacuously. Assert `pip show rich` fails in the venv before running the real assertion.
- **Type / marker:** `integration` (spawns a subprocess and builds a venv — this is not free; consider `slow` if venv creation exceeds a few seconds). If marked `slow`, **it will not run by default** — flag this explicitly, since it's the one Phase-0 fix a new user hits immediately. Recommend keeping it `integration`-only if venv creation is fast enough to fit under the 30s global timeout with margin.

---

## Phase 1 — Make the instrument able to record reality

### C-01 · The quality ledger is structurally incapable of recording a failure

- **Reproduction test 1 (terminal failure):** `tests/telemetry/test_crit01_ledger_records_failure.py::test_forced_whole_chain_failure_writes_a_quality_ledger_row`. Arrange: an isolated `LLM_ROUTER_ROUTING_LEDGER` (see infra table), and a fake model-call function that raises for **every** model name in the resolved chain (new fixture — see infra gaps). Act: `router.route_and_call(...)`. Assert: `routing_quality.jsonl` contains at least one new row for this route, with a field indicating failure. **Fails today** because `record_route()` has exactly one call site (`router.py:2004`), inside the success path (`_finalize_successful_route`); the failure path (`router.py:3420`, `_emit_ledger_terminal("failed", ...)`) never touches this file, so the ledger has zero new rows for this route.
- **Reproduction test 2 (cache hit):** `test_cache_hit_writes_a_quality_ledger_row` — same arrangement but the second call hits a warm cache. **Fails today**: the gate at `router.py:1958` (`if not suppress_ledger and not served_from_cache:`) explicitly skips this file on a cache hit.
- **Fix-verification test:** both inverted, plus `test_three_terminal_outcomes_are_representable` — after the fix, assert the ledger's own outcome field (whatever `route_outcome` enum REMEDIATION_PLAN 1.1 introduces) has been observed with all three values (`success`, `failed`, `cache_hit`) across a scripted sequence of three routes (one forced success, one forced total failure, one forced cache hit) run against one isolated ledger file.
- **Use case:** a provider returns 500 on every model in the chain; the user's request still completes (or errors out visibly to them), but the routing-quality ledger — which every "success rate" and "escalation rate" figure is computed from — shows nothing happened. Ground Truth sampling, which reads this file, can never see a bad routing decision.
- **Vacuity guard:** the three-outcome test asserts the *count* of distinct outcome values seen is exactly 3, not "at least 1" — a fix that adds the failure path but leaves cache-hit still gated would pass a weaker assertion and still be broken.
- **Type / marker:** unit, `fast`. Runs by default — this is the audit's top finding; it must.

### H-09 · The North Star ledger's silent-loss fix was applied to its sibling only

- **Reproduction test:** `tests/telemetry/test_h09_north_star_ledger_counts_losses.py::test_north_star_write_failure_is_counted`. Arrange: monkeypatch the JSONL append inside `record_route()` (or the file write it calls) to raise. Act: trigger a completion route. Assert: whatever counter `failopen.record` increments is incremented by 1. **Fails today**: `router.py:2043` is `except Exception: log.debug(...)`, discarding the exception with no counter — this is a documented regression of the exact incident ("66 dropped events across 2400 writes, no error, no log, no counter") that `failopen.record` was written to prevent, just on the sibling ledger.
- **Fix-verification test:** same, inverted, once `failopen.record` is called at `router.py:2043` matching `router.py:1821`'s pattern.
- **Use case:** the quality ledger silently drops a write under the same conditions that once dropped 66 of 2400 execution-ledger writes; nobody notices because there is no counter to notice with.
- **Vacuity guard:** run the identical test against the sibling path — `_emit_ledger_attempt` at `router.py:1821` — as a positive control. It must already pass today, proving the test harness correctly detects counting when the counting code is present, before trusting its failure on the other path.
- **Type / marker:** unit, `fast`. Runs by default.

### C-02 continued · Write provenance into `usage.db` at insert time

- **Fix-verification test:** `tests/economics/test_crit02_savings_sign_after_fixture_removal.py::test_is_simulated_is_populated_on_insert` (new test, can only be written once the `INSERT INTO usage` statement gains the column — currently the statement at `cost.py:848` omits it entirely). Arrange: run a test-shaped call through `cost.log_usage()` (or whatever inserts) under `PYTEST_CURRENT_TEST` set (as it always is inside pytest) or `LLM_ROUTER_SYNTHETIC=1`. Assert: the inserted row's `is_simulated` column is `1`, using `detect_synthetic()`'s existing signal (`routing_quality.py:87`, already correct and reusable — REMEDIATION_PLAN says "use it," not "reinvent it").
- **Regression guard (write now):** `tests/economics/test_crit02_savings_sign_after_fixture_removal.py::test_is_simulated_filter_currently_excludes_nothing` — insert a row via the real code path, assert `is_simulated` is `NULL`/`0` even for a row that should be simulated (this is the **current, broken** state — assert it explicitly so the test suite has a documented red baseline to compare the fix against, not just prose in this plan).
- **Vacuity guard:** the fix-verification test must also insert one row where `detect_synthetic()` returns `False` and assert `is_simulated == 0` for it — a fix that stamps every row as simulated would pass a one-sided test.
- **Type / marker:** unit, `fast`.

### H-04 · `classification_method` is 0% populated

- **Reproduction test:** `tests/telemetry/test_h04_classification_method_populated.py::test_classification_method_matches_the_classifier_used`. Arrange: route a request with a mocked classifier producing `classification_data["classifier_type"] = "heuristic"` (the key the builders actually write — confirmed at `src/llm_router/tools/routing.py:388` and `:573`). Act: inspect the `RouteLedgerRecord` written to the isolated `routing_quality.jsonl`. Assert: `row["classification_method"] == "heuristic"`. **Fails today**: both writer sites (`router.py:1997` and `router.py:2040`) read `(classification_data or {}).get("method")` — a key the builders never set — so the field is always `None`.
- **Fix-verification test:** same, inverted, once the reader is changed to `.get("classifier_type")` (a one-line fix per REMEDIATION_PLAN 1.4).
- **Use case:** an analyst asks "which classifier routes best?" — a query that today silently returns "no data" (100% null) rather than an error, and would keep doing so forever without this test.
- **Vacuity guard:** assert the value equals the *specific* classifier string used in the test (`"heuristic"`), not just "is not None" — a fix that defaults the field to a placeholder string would pass a weaker assertion.
- **Type / marker:** unit, `fast`. Runs by default.

### H-01 · Provenance is computed and then not read

- **Reproduction test:** `tests/observability/test_h01_summarize_ignores_provenance.py::test_one_synthetic_row_does_not_dominate_escalation_rate`. Arrange: an isolated ledger with 1 row where `synthetic=True` and (for the vacuity guard) 4 additional rows where `synthetic=False`, 2 of which escalate. Act: `routing_quality.summarize()`. Assert: `quality_escalation_rate` reflects only the 4 real rows (2/4 = 0.5), not the audit's demonstrated `1.0` from the synthetic row alone. **Fails today**: `summarize()` has 0 references to `synthetic` or `is_evaluable`.
- **Fix-verification test:** same, inverted, once `summarize()` calls `is_evaluable()` (REMEDIATION_PLAN 1.5) before including a row.
- **Use case:** a manual test-mode dry-run, or a single benchmark invocation, is enough by itself to move the org-wide quality-escalation dashboard to 100% or 0%, because the denominator has one row in it.
- **Vacuity guard:** the expected rate (0.5, from the 4 real rows) is an exact value, not a direction — a fix that excludes the synthetic row but also mis-handles the real ones (e.g. divides by 5 instead of 4) fails this test even though it "removed" the synthetic row's effect.
- **Type / marker:** unit, `fast`. Runs by default.

**Phase 1's own acceptance test** (from REMEDIATION_PLAN: "enable accumulation, run the full test suite, confirm reported savings is $0.00 and reported success rate is undefined rather than 100%") is **not a pytest test** — it is a manual/CI gate run once, after all of the above land, and its own output (a savings figure and an escalation rate over the test suite's own synthetic traffic) is the artifact that proves Phase 1 is done. Script it as `scripts/verify_phase1_acceptance.sh` if it needs to be repeatable, but do not force it into the pytest suite, where it would be indistinguishable from a normal test and would need its own isolation on every CI run.

### M-03 · 34 rows with no fallback reason (not assigned a phase in REMEDIATION_PLAN)

REMEDIATION_PLAN.md does not name a remediation action for M-03 — confirmed by
grep; it appears only in `AUDIT_FINDINGS.md` and `FEATURE_REALITY_MATRIX.md`.
Placed here by inference, since it is a ledger-completeness defect adjacent to
H-04 and H-09 and likely shares a root cause with one of them (a path that
constructs `RouteLedgerRecord` without deriving `fallback_reason` even though
`chosen_model != final_model`).

- **Test:** `tests/telemetry/test_m03_fallback_reason_never_null_when_occurred.py::test_every_model_divergence_has_a_reason`. Arrange: force a fallback (first model errors, second succeeds) via the existing `test_a01`-style fixture. Assert: the resulting row has `fallback_occurred=True` and `fallback_reason is not None`. This does **not** reproduce the specific 34-row bug without first finding which code path produces `fallback_occurred=False, fallback_reason=None, chosen_model != final_model` simultaneously — flag as **needs investigation** before this test can be written as a true repro; today it's a regression guard on the common path, not a repro of the residual one.
- **Vacuity guard:** print the count of `chosen_model != final_model` rows the test's own fixture produces (should be exactly 1 per forced-fallback route) so a future maintainer can tell the assertion actually exercised the divergent path.
- **Type / marker:** unit, `fast`.

---

## Phase 2 — Start a clean measurement window

This phase is mostly process (mark old rows provenance-unknown, wait for a real
window, republish with N/window/source), not new test surface. Two tests are
still worth having:

- `tests/economics/test_crit02_savings_sign_after_fixture_removal.py::test_pre_phase1_rows_are_excluded_by_default` — rows lacking `is_simulated` (written before the fix) must be treated as unknown-provenance and excluded by any reader that claims fail-closed semantics (REMEDIATION_PLAN 2.2), mirroring `is_evaluable()`'s own handling of rows predating the `synthetic` field (`routing_quality.py:246-248`). This is the one Phase 2 test that is genuinely new code, not a republish.
- `tests/economics/test_savings_report_carries_n_and_window.py` — any function that formats a savings or escalation figure for a user-facing surface must include N and the window in its return value or output string; assert this structurally (the CLAUDE.md house rule: "a number in a README, CHANGELOG or release note carries its N, its window and the file it came from, or it does not ship" — extended here to any runtime-printed figure, not only shipped docs).

---

## Phase 3 — Concurrency correctness

### H-06 · `quota_tracker` loses reads under concurrency

- **Reproduction test:** `tests/reliability/test_h06_quota_tracker_atomic_write.py::test_concurrent_reads_do_not_fail_under_load`. **Must** `monkeypatch.setattr(QuotaTracker, "USAGE_JSON", tmp_path / "usage.json")` on the class — `LLM_ROUTER_HOME` does nothing here (see infra table; `quota_tracker.py:84` never reads an env var). Arrange: N (≥100) concurrent async readers/writers against the tracker. Act: run them concurrently via `asyncio.gather`. Assert: read failure rate < 1%. **Fails today**: measured 32–38%.
- **Fix-verification test:** same, after the write path moves to temp-file-then-rename (REMEDIATION_PLAN 3.1, pattern to borrow conceptually from `budget_backend.py`'s serialised-writer design — note that module uses SQLite `BEGIN IMMEDIATE` locking, not literally temp+rename, since it's a different storage shape; the *property* to copy is "no reader ever observes a half-written file," not the specific bytes).
- **Use case:** 10 hooks read this file on a normal session; under concurrent hook invocation (a plausible shape — multiple Claude Code sessions, or a single session firing several hooks close together) roughly a third of reads see a torn or missing file and presumably fail open or return stale data.
- **Vacuity guard:** the test must print the actual observed concurrent-write overlap count (e.g., "N writers had at least one write in-flight when another write started: K times") and assert `K > 0` — proving the race window was actually hit, not just that N tasks ran. A harness that accidentally serialises would report 0% failure today too, for the wrong reason.
- **Type / marker:** `reliability`. Needs `pytestmark = pytest.mark.timeout(90)` (or similar), following the existing pattern in `tests/reliability/test_ledger_concurrency.py:26`, since the global 30s default may not cover 100+ concurrent async operations reliably. Runs by default (no external services, just needs the timeout override).

### H-07 · `Pool.admit()` loses increments

- **Reproduction test:** `tests/reliability/test_h07_pool_admit_no_lost_increments.py::test_concurrent_admits_are_not_lost`. Arrange: N (≥200 — large enough that a 19-vs-21-shaped loss is not noise; the audit's own measurement at N≈21 is itself borderline per this plan's own vacuity rules) concurrent calls into `scripts/groundtruth/pool.py:211`'s `Pool.admit()`. Assert: the pool's final count equals N exactly. **Fails today**: audit measured 19 recorded of 21 occurred.
- **Structural regression guard:** `test_accumulate_reuses_one_pool_instance` — grep or import-inspect `scripts/groundtruth/accumulate.py` and assert it does not construct a fresh `Pool()` inside the per-call function (today it does, which is *why* production hits this bug — an in-process lock on a pool that gets discarded every call protects nothing).
- **Fix-verification test:** the count test, inverted, at the same N.
- **Use case:** Ground Truth accumulation runs under real concurrent traffic; some fraction of eligible candidates never make it into the pool, and nothing reports the loss.
- **Vacuity guard:** run the same test at N=1 as a sanity check that the harness itself works (must show 1 of 1) before trusting the N=200 result.
- **Type / marker:** `reliability`, in-process (no external services), should fit the default 30s timeout; runs by default.

### M-05 / M-06 · `usage.db` and `result_cache` cold-start races

| Finding | Test | Assertion that fails today | Marker |
|---|---|---|---|
| M-05 | `tests/storage/test_m05_migration_failure_not_swallowed.py::test_a_failing_migration_is_visible` | Force one migration statement to raise (mock the connection); assert the failure is logged/raised, not silently passed. Confirmed pattern: `cost.py` has multiple `"""Idempotent migration..."""`-documented ALTER statements; audit measured 5/12 cold starts swallow a migration failure with no trace | `storage`, `fast` |
| M-06 | `tests/storage/test_m06_busy_timeout_before_wal.py::test_busy_timeout_precedes_wal` | Source-order assertion (or cold-start lock-rate measurement, mirroring the audit's 2/12 measurement) on `result_cache.py` — confirmed today: `journal_mode=WAL` is set at line 150, `busy_timeout=3000` at line 151, i.e. **after** WAL, which is what causes the lock window on cold start | `storage`, `fast` |

Both runnable by default; both are small, targeted fixes with a clear before/after.

---

## Phase 4 — Ground Truth

**Blocked on Phase 2's clean window per REMEDIATION_PLAN** — but the tests
below don't need real data, only isolated fixtures, so they can be written now
and simply not wired into any "GT is trustworthy" gate until Phase 2 closes.

### H-10 · The approval gate is a string comparison

- **Reproduction test:** `tests/security/test_h10_approval_gate_rejects_non_assistant_actor.py::test_approve_rejects_a_non_human_actor_that_is_not_literally_assistant`. Arrange: a `Verifier` in `VALIDATED` status (per `verifier_registry.py`'s state machine). Act: `verifier.approve(actor="assistant-v2")` (or `"AUTOMATION"`, or `"script"` — any string that is not the exact literal `"assistant"`). Assert: the call is **rejected** — `(False, "approval requires a human actor")` or equivalent. **Fails today**: `verifier_registry.py:112`'s check is `if not actor or actor == "assistant": return False, ...` — any actor string other than the exact literal `"assistant"` is treated as human and the call returns `(True, "approved")`.
- **Related, worth adding in the same file:** `test_activate_has_no_actor_check_at_all` — `activate()` (`verifier_registry.py:120`) calls `_move()` with the actor but `_move()` itself performs no identity validation (confirmed by reading it — it only checks the state-transition table). This means even a correctly-fixed `approve()` gate doesn't stop `activate()` from being called by any actor once a verifier is `APPROVED`. This is adjacent to H-10 but not identical to it — REMEDIATION_PLAN 4.4 says "replace the approval gate," singular; confirm whether `activate()` is in scope before assuming this second test is covered by the same fix.
- **Use case:** an autonomous agent scripts the CLI (`verifier-cli approve --by anything-else`), self-promotes its own verifier from `VALIDATED` to `APPROVED`, and (per the second finding above) can then call `activate()` with no gate at all, reaching `ACTIVE` — a verifier a human never reviewed now grades real candidates.
- **Vacuity guard:** the test asserts rejection for **several** non-"assistant" strings (`"assistant-v2"`, `"ASSISTANT"`, `"a human"`, `""` is already covered by `not actor`) — not just one — so a fix that special-cases one more string instead of implementing real identity still fails.
- **Type / marker:** unit/security, `fast`. Runs by default.

### M-01 · `completeness()` disagrees with `reconstructable`

- **Test:** `tests/audit/test_m01_completeness_matches_reconstructable.py::test_completeness_agrees_with_reconstructable_on_a_missing_patch`. Arrange: an `Envelope` whose repo state was captured (hash stored) but whose patch (`envelope.py`'s `diff` field) was never stored — e.g. a patch too large and dropped, per the module's own capping logic. Act: call both `env.completeness()` and `env.repo.reconstructable`. Assert: they agree — if `reconstructable` is `False`, `completeness()` must report the patch as missing, not `(True, [])`. **Fails today** per the audit's direct demonstration.
- **Vacuity guard:** also assert `accumulate.py`'s own redundant correct check (`has_repo_state=bool(env.repo and env.repo.reconstructable)`, `accumulate.py:95`) is what's currently masking this — write a variant of the test that calls `env.completeness()` **directly**, bypassing `accumulate.py`, so removing the redundant check later doesn't silently un-mask a bug this test was supposed to catch.
- **Type / marker:** unit, `fast`. Runs by default.

### M-02 · `detect_synthetic()` misses most synthetic traffic

- **Test:** `tests/audit/test_m02_detect_synthetic_consults_all_signals.py::test_sandbox_path_session_is_detected_as_synthetic`. Arrange: a session whose workspace path matches the sandbox pattern `sources.py` already knows how to detect (`^-(private-)?(tmp|var-folders)-`, per `CLAUDE.md`'s own documented rule and `sources.py:96-104`'s `BENCH_SANDBOX` comment) but with neither `LLM_ROUTER_SYNTHETIC` nor `PYTEST_CURRENT_TEST` set. Act: `routing_quality.detect_synthetic()`. Assert: `True`. **Fails today**: `detect_synthetic()` (`routing_quality.py:87-100`) checks exactly two signals and neither is the sandbox-path or fixture-session-id detector `sources.py` already implements.
- **Regression guard:** `test_bench_scripts_set_the_synthetic_flag` — grep every `bench_*.py` for `LLM_ROUTER_SYNTHETIC`; today 0 of them set it (per the audit).
- **Vacuity guard:** the sandbox-path fixture used must be a **real** pattern already proven to occur (the audit's own measured `-private-tmp-bq-claude` and eleven siblings) — not an invented path that happens to match the regex.
- **Type / marker:** unit, `fast`. Runs by default.

### H-08 · The replay envelope has no replayer

Genuinely gated on the Phase 4 fork (Option A: implement replay; Option B:
narrow eligibility). Reproduction is possible now; full fix-verification is not.

- **Reproduction test (write now):** `tests/e2e/test_h08_edit_task_cannot_be_graded_today.py::test_run_matrix_grades_an_edit_task_wrong_without_replay`. Arrange: a hermetic tmp git repo (new fixture — see infra gaps) with one commit and one uncommitted patch that, *if applied and tested*, would pass a known pytest file. Build an `Envelope` around it (repo commit + patch, matching `envelope.py`'s real shape). Mock the model call to return the exact patch content as its answer. Act: `run_matrix.call_model()` / whatever grades it. Assert (documents the harm, doesn't need replay to exist): the candidate is graded as failing or `UNUSABLE`, **even though the patch is correct**, because the harness sent the prompt as single-turn text and graded raw text against a pytest file that imports from a tree never checked out. This is reproducible today with zero dependency on which remediation option is chosen.
- **Fix-verification test, Option A (implement replay):** same fixture, but once a replay function exists (name TBD — do not guess the signature), assert the correct patch is now graded as passing. Cannot be written until that function is designed.
- **Fix-verification test, Option B (narrow eligibility):** `tests/e2e/test_h08_eligibility_no_longer_admits_ungradeable_tasks.py::test_edit_shaped_task_is_ineligible` — once `eligibility.assess()` is narrowed, assert it returns ineligible for an EDIT/code-shaped task (repo state + patch present, no mechanical verifier that doesn't require file access). This one **can** be written now, since narrowing a boolean gate doesn't require a new API — write it, expect it to fail today (assess() currently admits this shape), and it becomes the fix-verification test if Option B is chosen.
- **Use case:** the eligibility gate admits a real EDIT/code candidate into the Ground Truth pool; it can never be graded correctly; if it happens to fail because the harness has no file access, it's recorded as a failing candidate, poisoning `cheapest_acceptable_model` labels with false negatives.
- **Vacuity guard:** the reproduction test must also include a **QA-shaped** (non-EDIT) task in the same file as a control, asserted to grade *correctly* — proving the failure is specific to the replay gap, not a broken test harness.
- **Type / marker:** `integration` (hermetic — real `git` in `tmp_path`, mocked model, no network). Should run by default; confirm `git` subprocess calls stay well under the 30s global timeout (they will — a `git init` + one commit is milliseconds).

### GT audit items 4.3 / 4.5 (not independently numbered findings, but named as remediation items)

- **4.3 — `discriminate.policy_score` pools judge verdicts with mechanical ones once `generate_snippet()` grows a judge branch.** Not reproducible today (the branch doesn't exist — `generate_snippet()` currently has no path for judge/rubric strategies, so `policy_score` never receives one to pool incorrectly). **Write the guard test now anyway, as a tripwire:** `tests/audit/test_gt43_policy_score_filters_by_verification_type.py::test_policy_score_does_not_pool_a_judge_cell_with_mechanical_cells` — construct cells directly (bypassing `generate_snippet()`) with one `SUBJECTIVE_METHODS`-flagged judge cell and several `DETERMINISTIC_METHODS` mechanical cells; assert `policy_score` either filters the judge cell out or scores it separately. This **should already fail** if written today, since `discriminate.py::policy_score` has zero verification-type filtering (confirmed: it never imports `DETERMINISTIC_METHODS`) — the only reason it's not visibly broken yet is that nothing calls it with mixed cells. Writing this test now converts a latent defect into a known red test before `generate_snippet()` is extended, rather than after.
- **4.5 — snippet-path discrimination floor.** `tests/audit/test_gt45_snippet_validator_has_a_discrimination_floor.py::test_validate_snippet_verifier_rejects_a_trivial_bad_answer`. Arrange: call `validate_snippet_verifier` with a `bad_answers` list that is trivially wrong in a way that doesn't exercise real discrimination (e.g. an empty string, or a string with no semantic relation to the task). Assert: the result does **not** reach `HIGH` confidence — mirroring the floor the pytest path (`mutants.py`) already enforces via `MUTATIONS`. Fails today: nothing stops `detected > 0` for a barely-discriminating `bad_answers` set from reaching `HIGH`.
- **Type / marker:** both unit, `fast`.

### H-05 · No completion route is ever verified (unassigned phase — flagged)

REMEDIATION_PLAN.md never names H-05 directly. It is implied by Phase 4's
"do not build more GT pipeline before C-01" framing (verification rate feeds
GT's input population) but has no explicit action item. Placed here by
inference; **a plan gap worth raising with whoever owns Phase 4's scoping.**

- **Test:** `tests/observability/test_h05_verification_attempted_not_hardcoded_false.py::test_verification_attempted_reflects_whether_verification_ran`. Arrange: two routes — one through a path where verification genuinely happens (MGEE, per `routing_quality.py:526`, which correctly sets `verification_attempted=True`) and one ordinary completion route. Assert: the MGEE route's row is `True`; the ordinary completion route's row is `False` **for the right reason** — because verification wasn't attempted, not because the field is unconditionally hardcoded. **Fails to prove the right thing today**: `router.py:2015` hardcodes `verification_attempted=False` inside `_finalize_successful_route` for every completion route, so the field is a constant, not a measurement — a test that only checked "completion routes show False" would pass today for the wrong reason (rule 3's vacuity trap, self-applied).
- **Vacuity guard:** the test must include the MGEE-path positive case (`True`) in the same file, or it cannot distinguish "the field correctly reports no verification happened" from "the field is a constant."
- **Type / marker:** unit, `fast`.

---

## Phase 5 — Surface honesty

### H-03 · The gateway silently drops tool definitions

- **Reproduction test:** `tests/integration/test_h03_gateway_preserves_tool_definitions.py::test_tools_field_survives_the_gateway_or_is_explicitly_rejected`. Arrange: mock the underlying model call to return a tool-call-shaped response when the prompt includes tool definitions. POST to the gateway's `/v1/chat/completions` (`gateway.py`) with a `tools=[...]` payload. Assert: the response's `finish_reason` is `"tool_calls"` (or the client sees an explicit, documented rejection) — **not** silently `"stop"` with a prose reply. **Fails today**: `_flatten()` (`gateway.py:263`) reduces `messages` to a text blob before the handler runs, `tools` is discarded by the Pydantic request model, and `finish_reason` is hardcoded `"stop"` (`gateway.py:465`).
- **Fix-verification test:** same, inverted, once tool definitions are threaded through or the endpoint returns an explicit "function calling not supported" error instead of a fabricated prose reply.
- **Use case:** an OpenAI-compatible client (Cursor, Continue, a custom agent) does function calling through the gateway; it receives a confident prose answer instead of a tool call and has no signal that anything went wrong — the agent loop silently breaks.
- **Vacuity guard:** a control request **without** `tools=` in the same test file must still correctly return `finish_reason="stop"` — proving the fix didn't just stop returning `"stop"` ever.
- **Type / marker:** `integration`, hermetic (mocked model backend, real gateway HTTP layer). Should run by default.

### M-08 / M-10 / M-12 / H-02

| Finding | Test | Assertion that fails today | Marker |
|---|---|---|---|
| M-08 | `tests/security/test_m08_gateway_requires_bearer_auth.py::test_unauthenticated_request_is_rejected` | POST to the gateway with no `Authorization` header; assert 401/403. Confirmed today: `gateway.py:588`'s own comment states "this app has NO request authentication." Reference pattern: `commands/sse.py` requires Bearer on every request | `security`, `fast` |
| M-10 | `tests/integration/test_m10_control_plane_api_importable.py::test_control_plane_api_imports_cleanly` | `import llm_router.control_plane.api` inside a fresh venv built from the wheel; assert no `ImportError`. Confirmed today: raises `ImportError: cannot import name 'audit'` — the enterprise module it imports unconditionally isn't distributed | `install`, `integration` |
| M-12 | `tests/docs-private/test_m12_docs_claims_match_shipped_surface.py::test_documented_commands_exist` | Parse docs for command names (`health`, `gain`) and host-integration names; assert each resolves to a real CLI command / accepted install target. Fails today for at least `health` and `gain` per the audit | `docs-private`, `fast` |
| H-02 | `tests/docs-private/test_h02_readme_stat_traces_to_one_measurement.py::test_hero_stat_is_not_spliced` | Parse the README's headline stat; cross-reference against `docs/MEASUREMENT.md` rows; assert the triple (N, %, %) matches one row's (N, window, condition), not three different rows'. Fails today — confirmed spliced from three cells | `docs-private`, `fast` |

M-10's test needs the same "genuinely clean venv" discipline as M-11's — installing
from the wheel, not `pip install -e .` against a dev tree that happens to have
the enterprise module on `sys.path` some other way.

### The consolidated LOW section

Thirteen findings, almost all dead-code or docs-drift, each a one-line grep or
introspection assertion. One file: `tests/test_low_findings_consolidated.py`.

| ID | Assertion | Currently |
|---|---|---|
| L-01 | `derive_trace_id` removed from `__all__` or given a caller | 0 callers, still exported — implies a shipped feature |
| L-02 | `judge_cascade.should_cascade`/`should_judge_inline` have a caller, or are deleted alongside the duplicate inline logic in `streaming_judge.py` | 0 production callers |
| L-03 | the 9 named `cost.py` reporting functions have a caller outside their own tests, or are deleted | 0 callers today |
| L-04 | the 5 named modules (`budget_lineage_reconciliation`, `feedback_handler`, `hook_deadlock_checker`, `oauth_token_rotation`, `service_manager`) have a production caller, or their test files are deleted alongside them | 0 callers |
| L-05 | `context_signal.py`'s docstring no longer says "NOT the one in production," or the module is deleted | live decoy |
| L-06 | `storage/service.py::migrate_config` has a caller and no `# TODO`, or is deleted | 0 callers + TODO |
| L-07 | the bandit reorder failure at `router.py:3978` is counted (same pattern as H-09) | silent, uncounted |
| L-08 | `accumulate_report.py::_runtime_outcomes`'s two `except Exception: return {}` carry a comment or a counter distinguishing "broken" from "no data yet" | indistinguishable today |
| L-09 | setting `LLM_ROUTER_PERSIST_RAW=1` logs a startup warning | silent today |
| L-10 | setting `LLM_ROUTER_PERSIST_TTL_DAYS=0` logs a warning that purging is disabled | silent today |
| L-11 | `envelope.capture_repo_state` uses the `tools/fs.py::_assert_under_root` containment check | unguarded path-join today |
| L-12 | `tests/test_gateway_service.py:53`'s `assert not dest.exists() or True` has the `or True` removed | tautology, confirmed present |
| L-13 | the default-excluded marker set (`slow`, `requires_ollama`, `requires_api_keys`, `requires_codex`) is printed in CI output, or a nightly job runs them | invisible today — this is also this plan's own repeated caveat about default-run coverage |

Each is one `grep`/`ast`-based assertion; none need mocks, fixtures, or
isolation beyond what a plain source-tree scan requires. `fast`, default-run,
all thirteen in one file so a single CI job's runtime for "is the dead code
still dead / still undocumented" stays proportional to its actual cost.

---

## Coverage summary

| Finding | Test file | Marker | Runs by default | Phase |
|---|---|---|---|---|
| C-01 | `tests/telemetry/test_crit01_ledger_records_failure.py` | none (`fast`) | Yes | 1 |
| C-02 | `tests/economics/test_crit02_savings_sign_after_fixture_removal.py` | none (`fast`) | Yes | 0 / 1 / 2 |
| C-03 (hermetic) | `tests/routing/test_crit03_semantic_cache_collision.py` | none (`fast`) | Yes | 0 |
| C-03 (live drift check) | `tests/e2e/test_crit03_semantic_cache_collision_live.py` | `e2e`, `requires_ollama` | **No** — nightly/Ollama CI only | 0 |
| C-04 | `tests/security/test_crit04_secrets_bypass_scrubber.py` | none (`fast`) | Yes | 0 |
| H-01 | `tests/observability/test_h01_summarize_ignores_provenance.py` | none (`fast`) | Yes | 1 |
| H-02 | `tests/docs-private/test_h02_readme_stat_traces_to_one_measurement.py` | none (`fast`) | Yes | 0 / 2 |
| H-03 | `tests/integration/test_h03_gateway_preserves_tool_definitions.py` | `integration` | Yes | 5 |
| H-04 | `tests/telemetry/test_h04_classification_method_populated.py` | none (`fast`) | Yes | 1 |
| H-05 | `tests/observability/test_h05_verification_attempted_not_hardcoded_false.py` | none (`fast`) | Yes | 4 (unassigned — inferred) |
| H-06 | `tests/reliability/test_h06_quota_tracker_atomic_write.py` | `reliability`, `timeout(90)` | Yes | 3 |
| H-07 | `tests/reliability/test_h07_pool_admit_no_lost_increments.py` | `reliability` | Yes | 3 |
| H-08 (repro + Option B) | `tests/e2e/test_h08_edit_task_cannot_be_graded_today.py` | `integration` | Yes | 4 |
| H-08 (Option A fix-verify) | not yet writable | — | — | 4 (post-decision) |
| H-09 | `tests/telemetry/test_h09_north_star_ledger_counts_losses.py` | none (`fast`) | Yes | 1 |
| H-10 | `tests/security/test_h10_approval_gate_rejects_non_assistant_actor.py` | none (`fast`) | Yes | 4 |
| M-01 | `tests/audit/test_m01_completeness_matches_reconstructable.py` | none (`fast`) | Yes | 4 |
| M-02 | `tests/audit/test_m02_detect_synthetic_consults_all_signals.py` | none (`fast`) | Yes | 4 |
| M-03 | `tests/telemetry/test_m03_fallback_reason_never_null_when_occurred.py` | none (`fast`) | Yes | unassigned — inferred (1) |
| M-04 | no single test — see Test Infrastructure Gaps | — | — | prerequisite |
| M-05 | `tests/storage/test_m05_migration_failure_not_swallowed.py` | `storage` | Yes | 3 |
| M-06 | `tests/storage/test_m06_busy_timeout_before_wal.py` | `storage` | Yes | 3 |
| M-07 | `tests/security/test_m07_error_sanitization_deleted_or_unreachable.py` | none (`fast`) | Yes | 0 |
| M-08 | `tests/security/test_m08_gateway_requires_bearer_auth.py` | `security` | Yes | 5 |
| M-09 | `tests/install/test_m09_routerarena_not_under_state_dir.py` | `install` (or `slow` — confirm) | Yes, unless slow | 0 |
| M-10 | `tests/integration/test_m10_control_plane_api_importable.py` | `install`, `integration` | Yes | 5 |
| M-11 | `tests/install/test_m11_status_command_clean_install.py` | `integration` (or `slow` — confirm) | Yes, unless slow | 0 |
| M-12 | `tests/docs-private/test_m12_docs_claims_match_shipped_surface.py` | none (`fast`) | Yes | 5 |
| L-01…L-13 | `tests/test_low_findings_consolidated.py` | none (`fast`) | Yes | 5 |

---

## What this plan does not test

- **It does not test that the fixes are implemented correctly beyond the
  stated assertion.** A fix-verification test proves the specific defect is
  gone, not that the surrounding code is otherwise well-designed.
- **It does not cover `okf.py` knowledge-store retrieval, `agentic/react.py`'s
  ReAct chain, or `semantic/store.py`/`semantic/traces.py` for secret leakage**
  — `SECURITY_PRIVACY_AUDIT.md` names these as explicitly not covered, and
  this plan inherits that gap rather than closing it.
- **It does not include a live-provider integration suite.** Every test here
  that touches a model call mocks it. `TEST_GAP_ANALYSIS.md`'s own finding —
  live provider failure/timeout/retry only exists under `tests/e2e/`,
  deselected by default via `requires_ollama`/`requires_api_keys` — stands
  unresolved by this plan except for the one recommendation (L-13) to make that
  exclusion visible in CI output.
- **It does not attempt to separate the 1,813 contaminated `usage.db` rows
  after the fact.** Per REMEDIATION_PLAN's own explicit instruction, that
  population is not separable, and no test in this plan tries to prove
  otherwise — C-02's tests prove the *symptom* (wrong sign) and the *fix*
  (provenance at insert), not a retroactive cleanup.
- **H-08's Option A fix-verification test cannot be written by this plan**,
  because the API it would assert against doesn't exist. This is stated
  plainly rather than padded with a placeholder test that would need to be
  rewritten anyway.
- **It does not verify the "positive findings" register** (append atomicity,
  truncation survival, mutation resistance, `budget_backend.py` correctness,
  etc.) — those already have tests, per `TEST_GAP_ANALYSIS.md`, and this plan
  only adds new coverage where the audit found a gap.
- **It does not estimate effort or assign owners.** That's `REMEDIATION_PLAN.md`'s
  job; this plan only says what proves each phase's work is real.
