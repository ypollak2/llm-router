# Fix RUN — live state

**This file is the handoff.** Re-read between tasks.
Started 2026-09-22 on `fix/audit-2026-09-22`.

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

| ID | Status | Task | Gate |
|---|---|---|---|
| F01 | **done** | Fix `test_h08_…::test_replay_detection_looks_for_a_real_runner` cleanup — restore the package attribute, not only `sys.modules[...]` | The 8 accumulation tests fail identically whether that file runs before or after them |
| F02 | **done** | Fix or update the 8 `test_groundtruth_accumulation.py` tests invalidated by H-08 | File passes alone; full suite passes in both orderings |
| F03 | **done** | Make `replay_available()` robust to module-cache mutation (T-13) | Setting `groundtruth.run_matrix` to a stub does not permanently flip the gate |
| F04 | **done** | Autouse fixture snapshotting `sys.modules` + key package attributes, failing on leak | Deliberately leaking a module in a probe test fails that test, naming it |
| F05 | **done** | Resolve `_quarantined_tests/` — fix, delete, or report ~90 known-failing assertions | `uv run pytest` output states the quarantined count, or the directory is gone |
| F06 | **done** | Make the suite independent of ambient host state | Suite result identical with and without a concurrent `llm-router update` |

**Phase 0 exit:** suite green under `-p randomly`, under `-x`, in both file
orderings, and with a concurrent CLI process running. **Until then, treat every
other gate in this plan as unverified.**

---

## Phase 1 — P0: wrong results, exposure, invalidated claims

| ID | Status | Task | Gate |
|---|---|---|---|
| F07 | **done** | `library/store.scrub_secrets` delegates to `secret_scrubber.scrub_text` (S-01) | A secret battery through `library-harvest` writes 0 survivals |
| F08 | **done** | `library/store.write_doc` uses `paths.private_opener` | `stat` shows 0600 at creation, not after chmod |
| F09 | **done** | `hooks/agent-route._scrub_agent_prompt` delegates to canonical (S-02) | Battery through `_log_agent_call` → 0 survivals in `agent_calls.json` |
| F10 | **done** | `hooks/auto-route._FALLBACK_SECRET_RES` generated from canonical, or its false "kept in sync" comment corrected (S-08) | Fallback and canonical redact the same classes, asserted by comparison not by comment |
| **F11** | **done** | **Rewrite `test_m07_no_second_scrubber.py` to call every rival scrubber** | Reverting F07 or F09 makes it fail. **This is Rule B's reference implementation** |
| F12 | **done** | `commands/demo.py` — baseline from each row's real premium cost, or remove the comparison (T-02) | A batch containing a premium call never prints a negative saving as "cheaper" |
| F13 | **done** | First test file for `commands/demo.py` | Reverting F12 fails it |
| F14 | **done** | Gateway passes `system` separately; classify the latest user turn (T-03) | `"hi"` classifies SIMPLE with and without 2KB of system boilerplate |
| F15 | **done** | `get_team_savings` provenance filter (T-05) | One synthetic row → $0.00 |
| F16 | **done** | Provenance column + write stamping on `claude_usage`, `codex_usage`, `gemini_usage`, `savings_stats` | Same |
| F17 | **done** | `get_quality_report`, `get_routing_savings_vs_sonnet`, `get_router_efficiency` filter or use `attribution.py` | Same |
| F18 | **done** | Delete or correct the false `is_real` comment at `cost.py:622` | No comment claims a filter that grep cannot find |
| **F19** | **done** | **Parametrised test over EVERY money surface** | One synthetic row → all six report zero. A new surface must be added to the list or the test fails |
| F20 | **done** | Exclude `budget_lineage_reconciliation` from the wheel or drop its `control_plane.audit` dependency (T-11) | Import + call from the extracted wheel succeeds |
| F21 | **done** | `test_shipped_modules_import.py` calls public functions from the built wheel; remove the `conftest.py` auto-skip hiding it | Reverting F20 fails it |

---

## Phase 2 — P1: reliability and evaluation

| ID | Status | Task | Gate |
|---|---|---|---|
| F22 | **done** | Idempotency-dedupe terminal writes a quality-ledger row (T-08) | Two identical keyed calls → 2 rows, second `route_outcome` distinct |
| F23 | **done** | Exhaustion-floor terminal writes a row | A fully gate-rejected route produces exactly 1 row |
| F24 | **done** | Mark degraded answers degraded on `LLMResponse` and in the rendered output (T-10) | A floor-served response is distinguishable from a clean one by field and by display |
| F25 | **done** | Floor responses do not feed `success=True` to the bandit | Bandit stats unchanged by a floor-served turn |
| F26 | **done** | Bounded bandit reward (T-09) | Paid at 0.99 success can outrank free at 0.50 |
| F27 | **done** | Quality signal stronger than "non-empty and not a deferral", or the reward stops calling it quality | A plausible-but-wrong answer does not score success |
| F28 | **done** | Surface `failopen.snapshot()` in `doctor` and `status`; raise the fallback above DEBUG (T-07) | A forced fail-open appears in `doctor` output |
| F29 | **done** | Second failure channel for `failopen` that does not depend on its own store | With the store unwritable, the loss is still visible |
| F30 | **done** | Bridge or retire the GT verifier pipeline (T-06) | Either an ACTIVE verifier grades a frozen task end to end, or the three modules are gone and the docs say labelling is manual |
| F31 | **done** | `run_verifier` receives a `cwd`; gate external evidence as repo state is gated | A repo-bound task either replays against its commit or is refused with a stated reason |
| F32 | **done** | `Pool.admit` first-arrival race (T-12) | 20 threads, same new prompt → exactly 1 canonical row |
| F33 | **done** | `propose.select_strategy` handles the FACTUAL/checkable shape (T-17) | An admitted checkable question does not fall to `no_reliable_verifier` |
| F34 | **done** | `route_server` auth parity with the gateway (S-03) | Unauthenticated request → 401 when a token is configured |
| F35 | **done** | `install` / `update` / `dev-refresh` honour `LLM_ROUTER_HOME` (S-04) | With it set to tmp, `~/.claude/` is byte-identical and mtime-unchanged after each |

---

## Phase 3 — P2: architecture and maintainability

| ID | Status | Task | Gate |
|---|---|---|---|
| F36 | **done** | Reduce the 84 mutation-wrapping silent swallows — count via `failopen` or propagate (T-14) | A forced write failure at a sampled site is visible somewhere |
| F37 | **done** | Fix `commands/profile` and `commands/dev-refresh`; graceful message for `tui` (T-18) | All three exit 0 or print an actionable message |
| F38 | **done** | Document or remove the 28 undocumented subcommands (T-19) | Every dispatchable command is in `--help` or gone |
| F39 | **done** | Make `test_h03_gateway_refuses_tool_calls.py` hermetic (T-22) | Passes with the network down |
| F40 | **done** | Move the 14 `requires_ollama` money e2e tests into a required lane (T-24) | CI runs them; they are the only full-stack ledger coverage |
| F41 | **done** | `direct_diagnostics` timeout mislabelling (T-23) | `doctor` stops advising a 0-second timeout |
| F42 | **done** | `quota_tracker` provider key `gemini` vs `google` (T-20) | A real Gemini row is counted |
| F43 | **done** | Surface the provenance cutover's excluded count (T-21) | "Why did my lifetime savings drop" has an in-product answer |
| F44 | **done** | Adopt `safe_subprocess`'s env allowlist in `verifiers.run_verifier` (S-07) | The subprocess no longer receives live API keys |

---

## Phase 4 — P3: cleanup

`session_store` → `private_opener` (S-09) · `SECURITY.md` 6→7 (S-10) ·
`env_registry` scripts/ scope (T-28) · `calls` denominator (T-26) · three
unparseable scripts · `propose.py`'s "authoring assistant" docstring ·
release HEAD so the published package carries the dashboard token fix (T-31).

---

## Carried forward, still parked

| ID | Status | Task | Why |
|---|---|---|---|
| P-01 | `parked` | Collect a clean measurement window | Needs elapsed real traffic, not code. Blocked on F15–F19 |
| P-02 | `parked` | Republish figures with n, window and source | Blocked on P-01 |

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


---

## Log

(appended after every task)

### Phase 0 — done. The suite signal is real.

| gate | result |
|---|---|
| accumulation alone / with h08 either way | **0 failures in all 4 orderings** (was 8 / 0 / 8 / 0) |
| full suite, `-p no:randomly` | exit 0 |
| full suite, random ordering | exit 0 |
| config tests with a poisoned ambient `.env` | pass |
| deliberate module-stub leak | **fails the leaking test by name** |

**F01** `monkeypatch` replaces hand-rolled save/restore — it cannot forget a
channel, which is exactly how the original missed the package attribute. Added
the post-cleanup assertion whose absence cost 8 masked failures.

**F02** The gate test was split into both sides — refused today, admitted once a
replayer exists — so the refusal cannot silently become permanent. Six mechanics
tests got a `with_replayer` fixture rather than encoding the pre-gate contract:
they are about pool/dedup/funnel behaviour, not about the gate.

Recorded while fixing: H-08's reason lands in `ineligibility_reasons`, which also
flips `replayable` to False. Arguably "the state was captured" and "something can
run it" should be separate axes — conflating them loses the very distinction
`R_NO_REPLAYER` exists to preserve. Asserted as-is rather than quietly.

**F03** `replay_available()` now reads run_matrix.py's SOURCE. A stub in
`sys.modules` can no longer answer for it. The probe was rewritten to exercise
the real mechanism instead of a channel the detector no longer consults.

**F04** Autouse leak detector: fails the test that leaves a fileless module stub
on a package object, naming it. Verified against a deliberate leak.

**F05** The run header now states the quarantine: 9 files excluded from
collection. "0 failures" was never "0 known failures".

**F06** `RouterConfig.model_config["env_file"]` is redirected per test. pydantic
evaluates it at class-body execution, so it was frozen against the real home at
import AND read a cwd-relative `.env` — which is what flipped three
default-profile assertions during the audit while a second process ran the CLI.


## Defects introduced BY the fix, found by exercising it (2026-09-22)

Both are in `F15`–`F17`'s own code. Both were found by running the change, not
by reading it, and both now have regression tests in
`tests/test_t05_provenance_plumbing.py`.

| # | Defect | How it presented | Fix |
|---|---|---|---|
| 1 | `production_only(include_simulated=True)` returned `""`, but **5 call sites EMBED** the fragment as `f"WHERE {production_only(..., prefix='')} AND ..."` | `sqlite3.OperationalError: near "AND": syntax error` on every call that used the documented escape hatch — which nothing had ever done | returns `"1=1"` when no prefix is supplied; added `test_no_money_query_can_compose_an_empty_where` so a 6th embedding caller cannot reintroduce it |
| 2 | `agentic/telemetry.py` gained `is_simulated` in its INSERT but **not in its own `CREATE TABLE`** | **silent total data loss.** `_default_recorder` is fail-open by design, so `table savings_stats has no column named is_simulated` went into `except: pass`; every agentic delegation savings row was dropped on any fresh install, with no error anywhere | column added to `_SAVINGS_DDL`; `test_agentic_savings_row_survives_on_a_fresh_database` looks for the row |

### Third defect, found while verifying #2

`agentic/telemetry._db_path()` composed `~/.llm-router/usage.db` directly and
**did not honour `LLM_ROUTER_HOME`** — a survivor of the T00b repo-wide sweep,
because it is not import-time binding, it simply never asked the resolver.

The probe verifying #2 therefore wrote a fake **$1.25 row stamped
`is_simulated=0` (production) into the operator's real ledger**. Row id 8827,
deleted by hand; `savings_stats` went 8827 → 8826. Same shape as
`evidence/AUDITOR_INCIDENT.md`.

`_db_path()` now goes through `paths.state_path()`, and
`test_agentic_telemetry_honours_llm_router_home` pins it.

### Test-side consequence (expected, not a defect)

29 money tests wrote rows as a pytest process (stamped synthetic) and read them
back. All now pass `include_simulated=True` / `include_synthetic=True` — the
named hatch `get_savings_by_period` already had. **No assertion was weakened, no
test skipped or xfailed.**

Two further failures were mechanical regenerations caused by editing three
hooks: `scripts/build_plugin_bundle.py` and `config/mutmut_gf.cfg`.

## Phase 4 — done 2026-09-22

| Item | What | Verified |
|---|---|---|
| S-09 | `session_store` appends via `private_opener` | `with opener: 0o600   without: 0o644` |
| S-10 | SECURITY.md's allowlist count | **Reproduced 10 of 12** — the file said 6, the audit said 7. Both wrong. Corrected, with `docs/security_command_matrix.txt` + the repro command so the number is re-runnable rather than remembered |
| T-26 | `calls` incremented before the subscription skip | `calls` and the dollar figures now share a denominator; excluded rows surface as `subscription_calls` |
| T-28 | `env_registry` claimed to cover "every environment variable this codebase reads" | Scope narrowed to `src/llm_router/` in the docstring; `scripts/` (18 vars) named as out of scope |

### Not done, and why

* **Release HEAD (T-31)** — publishing is outward-facing and needs the operator's
  decision; nothing here tags or pushes.
* **Three unparseable scripts** and **`propose.py`'s docstring** — cosmetic; no
  gate for them would have been more than a grep.

---

## Parked, needs a decision

| ID | Task | Why it is parked |
|---|---|---|
| P-01 | Collect a clean measurement window | Needs elapsed real traffic, not code. Unblocked by F15–F19 |
| P-02 | Republish figures with n, window and source | Blocked on P-01 |
| P-03 | `test_stage0_routing_recovery::test_installed_hook_matches_repo_source` fails | The hook installed in the operator's real `~/.claude/hooks/` has drifted from source because F41 edited `auto-route.py`. Re-syncing writes unreviewed branch code into live routing — outward-facing, on an uncommitted branch, so not done unattended |
| P-04 | F40's live lane | The 14 `requires_ollama` money e2e tests still need a live Ollama; making CI run one is an infrastructure/cost decision. A hermetic full-stack ledger test now covers the plumbing on every run |

---

## Defects this run introduced and caught

| # | Defect | Found by |
|---|---|---|
| 1 | `production_only(include_simulated=True)` returned `""`; 5 call sites embed it, producing `WHERE  AND …` | the agent updating the tests, on the first call that used the hatch |
| 2 | `agentic/telemetry.py` INSERT gained `is_simulated`, its own `CREATE TABLE` did not — **silent total data loss** behind a fail-open `except` | same agent |
| 3 | `agentic/telemetry._db_path()` ignored `LLM_ROUTER_HOME` | verifying #2 — the probe wrote a fake $1.25 row into the live ledger (row 8827, deleted) |

All three have regression tests in `tests/test_t05_provenance_plumbing.py`.
