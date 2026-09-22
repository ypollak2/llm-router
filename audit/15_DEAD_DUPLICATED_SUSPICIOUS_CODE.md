# Dead, duplicated, and suspicious code — round 3, 2026-09-22

HEAD `357a402`. This round starts from the 2026-09-21 and 2026-09-22 audits'
`DEAD_AND_SUSPICIOUS_CODE.md` findings and (a) verifies which were actually
remediated by the 15 commits since `8c7366b`, (b) checks for regressions in the
remediation itself, and (c) hunts for duplicate concepts not previously
inventoried. Classification: DEAD / LEGACY-BUT-ACTIVE / FEATURE-FLAGGED /
TEST-ONLY / UNREACHABLE / UNKNOWN. No deletions proposed, per brief.

---

## Part 1 — Reconciling the prior round's findings against HEAD

| Prior finding (2026-09-22 audit) | Status at HEAD | Evidence |
|---|---|---|
| `failopen.snapshot()` — 0 production readers, write-only | **RESOLVED → LEGACY-BUT-ACTIVE** | `ui/status_premium.py:231` and `commands/doctor.py:1449` both now call `failopen.snapshot()` and surface it (`451565d`). 58+ writers now have 2 real readers plus tests. |
| `is_real` column — false "filtered everywhere" comment, filtered nowhere | **RESOLVED, by deprecation not by fixing the filter** | `cost.py:719` now carries an explicit "DO NOT RELY ON `is_real` AS A PROVENANCE FILTER" docstring pointing at `provenance`/`is_simulated`/`production_only()`. The column still exists with `DEFAULT 1` (kept for backward compatibility) — **it is now a documented landmine rather than a silent one.** A future contributor who greps for "is real" and finds this column before finding `provenance` still has to read the warning to avoid the trap; the column was not removed. |
| GT verifier pipeline (`propose.py`→`mutants.py`→`verifier_registry.py`) structurally unreachable — `gtc-` vs `gt-` namespace mismatch | **RESOLVED** | `5462cda` bridges `gtc-{exact_key(prompt)}`, recomputable from a frozen task's own prompt with no mapping table. Commit demonstrates an ACTIVE pool-authored verifier grading a frozen task end to end ("accepts Lisbon, rejects Madrid"). Verifiers carrying `proposed_files` (pytest/mutation strategies) are now explicitly REFUSED rather than silently skipped — a design choice (trust boundary), documented as such. |
| `commands/profile.py` — `ImportError: PROFILE_PATH` | **RESOLVED** | Now imports `_profile_path()` (function), matches `auto_profile`'s actual export. |
| `commands/dev_refresh.py` — wrong script name (`llm_router-install-hooks` vs registered `llm-router-install-hooks`) | **RESOLVED** | Verified: `dev_refresh.py` now shells out to `llm-router-install-hooks` (hyphenated, matches `pyproject.toml` entry point). |
| `budget_lineage_reconciliation.reconcile_budget_lineage_audited` — raises on every call | **RESOLVED by removal** | `c0fbbc6` deleted the function outright rather than stubbing it, with a comment explaining why a stub would be worse (silently disables audit logging). `reconcile_budget_lineage` (unaudited), the one real callers use, is untouched. |
| `commands/sse.py` → `main_sse_secured` — imports `llm_router.enterprise.*`, doesn't exist | **STILL TRUE, reclassified** | See Part 3. Not "broken" in the sense of an unnoticed regression — it is one call site of a deliberate, repo-wide "enterprise features live in a private fork" seam. Fails closed by design, same as 5 sibling files. |
| Secret-pattern tables: 2 undocumented drifted scrubbers (`library/store`, `hooks/agent-route`) | **RESOLVED** | Both now delegate to `secret_scrubber.scrub_text` (`3dc57c5`); `hooks/auto-route.py`'s fallback table now derives from canonical rather than being a hand copy. `test_m07_no_second_scrubber.py` now asserts the *call site* (monkeypatches `scrub_text` with a counter), not just that the canonical function is correct — closing the exact "tested the abstraction, not the adoption" gap the prior round named. |
| Classifiers — 13 files define `classify*`, hook-vs-router 59.7% agreement | **UNCHANGED, carried forward** | Still 19 `def classify*` matches at HEAD (count differs from the prior "13" because this round's grep is not scoped identically; not independently re-measured this round — see note below). Documented, parked, tested for divergence rather than for agreement. |
| 84 silent-mutation `except: pass` sites | **NOT reduced — see `18_GIT_HISTORY_FINDINGS.md` Class 3** | Census at HEAD reports 88. |

**Note on the classifier count**: this round's `grep -rn "def classify"` returns
19 files, not the prior round's 13; the difference is method (broader pattern,
includes `classify_signals`/`classify_text`/etc. variants) not a claim that 6
new classifiers were added. Treat the 19 as this round's number and the 13 as
the prior round's; a diff of the two lists was not performed and the
divergence-rate figure (59.7%, n=750) was not re-measured — it is carried
forward as **not independently re-verified this round**.

---

## Part 2 — New duplicate-concept findings (Phase 32)

### Provider family allowlists — DIVERGE TODAY (new finding, see git history report Class 2)

Two independent enumerations of "which provider strings are valid/belong to a
family":

| Set | Location | Contents | Purpose |
|---|---|---|---|
| `GOOGLE_PROVIDERS` / `OPENAI_PROVIDERS` | `model_registry.py:42-53` | canonical, created by T-20 (2026-09-22) specifically to be the single source | classifies a provider string into a vendor family (used by `quota_tracker`) |
| `VALID_PROVIDERS` | `cost.py:1642` (`_validate_routing_insert`) | independently hand-written, 12 entries, includes `'gemini'`, **omits `'google'`** | gate on `routing_decisions` inserts |

**Disagreement, reproduced**: `_validate_routing_insert("google/gemini-1.5-pro",
"google", 0.001)` raises `ValueError`; `provider_from_model()` — the function
that actually derives the provider tag for a real routed Gemini call — returns
`"google"`. The write is attempted inside a bare `except Exception:
log.warning(...)` at the call site (`router.py:2368`), so it fails silently.
**CONFIRMED, CRITICAL**: every real Gemini routing decision through the main
router path is dropped from `routing_decisions` — the quality ledger, the
bandit's telemetry, and cost analytics all lose real data for this provider
family. This is a second, independent instance of the class T-20 was written
to close, in a sibling file T-20 did not touch.

### Path resolution — mostly converged, one live holdout (new finding)

The bulk of the ~149 call sites `paths.py`'s own docstring says exist are now
either (a) resolved per-call through a small helper pattern —
`root = Path(base).expanduser() if base else Path.home() / ".llm-router"` —
repeated near-verbatim across `vision_registry.py`, `trace.py`,
`attempt_log.py`, `model_discovery.py`, `hooks/draft_usage.py`,
`hooks/tool_intercept.py`, `hooks/agent_writes.py`, `hooks/direct_executor.py`
(this is itself a small duplicate — the same 1-line resolver copy-pasted 8+
times rather than imported from one place, though all 8 currently agree, so it
is a maintenance risk rather than a live divergence), or (b) lazy proxies in
`install_hooks.py` re-resolving on every access (`fe91cbf`'s fix, deliberately
not a `__getattr__` — see that commit's note on why `__getattr__` would have
broken monkeypatching).

The live holdout: `claude_jsonl_usage.py:15`, `_CC_DIR = Path.home() /
".claude" / "projects"`, a genuine module-level constant. See
`18_GIT_HISTORY_FINDINGS.md` Class 1 for detail. Classified **UNKNOWN risk /
TEST-ONLY gap** below — no test exercises this module at all, so a fixture that
tried to redirect it (the exact mechanism that surfaced every one of the prior
nine instances) has not yet had the chance to fail.

### `is_real` vs `provenance`/`is_simulated` — two competing "is this row real" columns, one deprecated in place

Not a divergence in behavior (only `provenance`/`is_simulated` are load-bearing
now), but a duplicate-concept hazard left on purpose: `routing_decisions` (and
siblings) still carry both `is_real` (dead, `DEFAULT 1`, documented as
unreliable) and the new provenance columns. `PRAGMA table_info` still shows
both. A contributor reading the schema, not the docstring, has a 50/50 chance
of picking the wrong one; this is better than the silent version but is not
the same as removing the trap.

---

## Part 3 — Classification table

| Component | Classification | Why |
|---|---|---|
| `failopen.snapshot()` / `record()` | **LEGACY-BUT-ACTIVE** | Built to fix a "66 dropped events, no counter" incident; now has 2 real production readers (`doctor`, `status_premium`) plus its original 58 writers. No longer write-only. |
| `is_real` column | **DEAD-BY-DESIGN (deprecated in place)** | Written once at migration/insert (`DEFAULT 1`), read by nothing production-facing, explicitly warned against in its own docstring. Kept for backward-compat schema stability, not for use. |
| GT verifier registry bridge (`gtc-` ↔ frozen task) | **LEGACY-BUT-ACTIVE (newly connected)** | Reachable from `run_matrix.py --use-registry` in production GT accumulation runs per `5462cda`'s own end-to-end verification; not independently re-run by this audit round beyond reading the diff and the commit's stated proof. Confidence on production reachability: STRONGLY SUPPORTED, not independently reproduced here. |
| `sampling.py` | **TEST-ONLY, by its own docstring** | Explicitly "ready now, deliberately unused." Unchanged this round. Honest dead code, not a defect. |
| `commands/sse.py::main_sse_secured` | **FEATURE-FLAGGED (externally gated)** | Module imports cleanly (verified: `from llm_router.server import main_sse_secured` succeeds under isolated `LLM_ROUTER_HOME`). Calling it raises `ImportError` because `llm_router.enterprise.identity` is not shipped in this OSS distribution — by design (`server.py:333`'s own comment). One of at least 6 files (`server.py`, `identity.py`, `quota_routing.py`, `control_plane/api.py`, `commands/audit.py`, `plugins/__init__.py`) that reference `llm_router.enterprise.*` the same way: lazy import, fail closed, never at module top level. Not an isolated bug — it is the visible seam of a proprietary/enterprise fork. |
| `budget_lineage_reconciliation.reconcile_budget_lineage_audited` | **REMOVED** | Deleted in `c0fbbc6`, not stubbed. No longer applicable. |
| `claude_jsonl_usage._CC_DIR` | **LEGACY-BUT-ACTIVE, TEST-GAP** | One production caller (`dashboard/tui.py`), zero test coverage of the module. Module-level `Path.home()` binding — the recurring Class 1 pattern — present and unaddressed. |
| `secret_scrubber.scrub_text` (canonical) vs its former copies | **RESOLVED — canonical, all live call sites delegate** | See Part 1. |
| `cost.py::VALID_PROVIDERS` | **LIVE BUG, not dead code** | Actively executes on every `log_routing_decision` call; the defect is that it disagrees with `model_registry`'s canonical provider-family sets, not that it's unreachable. Flagged here because it was found via the same duplicate-concept sweep that finds dead code, and belongs in the same inventory. |
| `scripts/silent_mutation_census.py` | **TEST/CI-ONLY tool, reachable** | Runs standalone, backs `test_t14_silent_mutation_ratchet.py`. Not shipped in the wheel (it's under `scripts/`), so it is infrastructure for maintaining the repo, not a runtime component — correctly scoped. |
| `env_registry` | **LEGACY-BUT-ACTIVE, scope now stated honestly** | T-28 fix (`31459b5`) narrowed its own claim from "every environment variable this codebase reads" to `src/llm_router/` only, after an 18-variable gap was found in `scripts/`. The 18 `scripts/`-only variables remain outside any registry — not itself a new finding, but worth noting the registry's coverage claim is now accurate rather than the underlying gap being closed. |

---

## What a fourth audit round should check first

1. Does `_validate_routing_insert`'s `VALID_PROVIDERS` get replaced with
   `model_registry.GOOGLE_PROVIDERS | model_registry.OPENAI_PROVIDERS | {...}`,
   or does it get hand-patched to add `'google'` as a thirteenth literal? The
   latter would "fix" this finding while leaving the class unresolved — the
   same test-the-abstraction-not-the-adoption gap `3dc57c5`'s postmortem
   already names once.
2. Whether `claude_jsonl_usage.py` gets a test at all, and whether that test
   converts `_CC_DIR` to the same per-call resolver pattern used in its 8
   siblings, or gains a hand-written exception.
3. Whether the 8 near-identical one-line path resolvers
   (`Path(base).expanduser() if base else Path.home() / ".llm-router"`,
   copy-pasted across `vision_registry.py`, `trace.py`, `attempt_log.py`,
   `model_discovery.py`, and 4 hook files) get consolidated into one importable
   helper before the ninth copy is added with a subtly different default.
