# Domain 03 — Dead code & dependencies

Baseline: `llm-router-forensic` worktree, detached at `3c96d23`. All evidence below is
file:line static evidence, cross-checked against `git log`, tests, `pyproject.toml`,
`uv.lock`, `.github/workflows/*.yml`, and `scripts/README.md`. Nothing was modified.

## Overview

This repo already runs an unusually rigorous internal audit practice: several prior
findings (`L-01…L-06`, `M-10`, `M-11`, `T-11`, `T-18`, `R11`) are pinned as **ratchet
tests** in `tests/` that fail the moment the dead code they describe gains a caller or
the gap they guard reopens. Where my own analysis reproduces one of those, I say so and
do not re-claim it as new. My distinct contribution in this domain is:

1. **A real, previously-uncaught M-11-class dependency bug** — `pyyaml` is imported
   unguarded at module level on the MCP server's live import path but is not a
   declared runtime dependency and is not pulled in transitively by any declared
   runtime dependency (DEAD-01, CRITICAL).
2. **A second, currently-masked instance of the same bug class** for `httpx`
   (DEAD-02, MEDIUM) — and the specific reason the existing M-11 guard test misses
   both: `_declared()` in `tests/install/test_m11_declared_dependencies.py` treats a
   name declared in *any* optional extra as "declared" for the purpose of the core
   install, which defeats the test's own stated purpose.
3. **A ~5,100 LOC root-level `scripts/` duplication cluster** (25 files) with a
   dated, single-commit root cause, not previously flagged anywhere I could find
   (DEAD-03).
4. **`control_plane/api.py` + `control_plane/reconciliation.py`** (490 LOC) — I
   independently reproduced and confirmed the existing `M-10` finding is accurate at
   this commit, with the precise import chain (DEAD-04).
5. Confirmation that the `L-03` "orphaned reporter" cluster in `cost.py` (including
   the audit's named lead, `log_savings`) is real and unchanged at this commit
   (DEAD-05).
6. `scripts/pre-release-checklist.py` — an undocumented, uncalled, single-commit
   script that instructs the reader to run a file (`scripts/release.py`) that does
   not exist in this repository (DEAD-06).

No new zero-caller `src/` public function/class was found beyond what
`test_l03_dead_public_api_ratchet.py` already tracks — I re-ran its detection logic by
hand against several additional candidates (below) and it holds.

---

## Dependency audit (§26)

### Runtime (`[project.dependencies]`)

| Dep | Where used (src/) | Notes |
|---|---|---|
| `mcp` (`>=2.0.0,<3.0.0`) | `server.py`, `service.py`, 6 more | Pin is load-bearing (`mcp.server.mcpserver.MCPServer` doesn't exist in 1.x or 3.x's old path); guarded by `tests/test_mcp_dependency_pin.py`. Healthy. |
| `litellm` (`>=1.50.0`) | `router.py`, 1 more | Also the sole transitive source of `httpx` and `aiohttp` in the core closure (see DEAD-02). |
| `pydantic`, `pydantic-settings` | 5 files | Used directly; also required by `mcp`/`fastapi`. No issue. |
| `aiosqlite` | 10 files | Primary storage driver (`cost.py`, attempt logs, etc). No issue. |
| `aiohttp` | 2 files | Also a transitive dep of `litellm` — belt-and-suspenders is fine here since it's declared. |
| `structlog` | 10 files (`alerts.py`, `logging.py`, `metrics.py`, `failopen.py`, `secret_scrubber.py`, `cost.py`, `router.py`, `quota_routing.py`, `quota_envelope_routing.py`, `dashboard/server.py`) | Core logging backbone. No issue. |
| `fastapi`, `uvicorn` | `service.py`, `gateway.py`, `server.py`, `commands/serve.py`, `control_plane/api.py` | Needed for the SSE/HTTP surfaces; `control_plane/api.py` is one of the two excluded-from-wheel modules (DEAD-04), so its usage doesn't count against the "used in the shipped package" claim, but the other 4 do. |
| `rich` | 6 files | Added reactively per pyproject comment (M-11, `status.py`/`ui/status_premium.py`). Healthy now, and guarded by `tests/test_m11_declared_dependencies.py::test_rich_is_declared`. |
| `requests` | `stats.py`, `local_platforms.py` | Same M-11 class as `rich` — already fixed and declared. This is the template DEAD-01/02 repeats. |

### Optional extras

| Extra | Packages | Actually used from `src/`? |
|---|---|---|
| `dev`, `test` | pytest family, hypothesis, ruff | Dev/test-only, correctly not in runtime deps. |
| `scripts` | `httpx>=0.27`, `pyyaml>=6.0` | **Both are also imported unguarded from core `src/` modules that ship in the wheel** — see DEAD-01/DEAD-02. This extra's real role ("scripts/ tooling deps") and its accidental second role ("silently supplies two core-runtime imports") are two different things wearing one extra. |
| `agno` | `agno>=2.5.14` | Used only in `integrations/agno.py`; gated (`OPTIONAL` map in `test_shipped_modules_import.py`). Healthy. Transitively supplies `pyyaml`, `rich`, `httpx` — this is exactly why a dev machine with `agno` installed never sees DEAD-01/02 fail. |
| `tracing` | `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc` | Used in `tracing.py` and `observability/core.py`, both behind `try/except ImportError`. Healthy, correctly optional. |
| `code-context` | `tree-sitter`, `tree-sitter-languages` | Used in `context_prep.py` and `code_context.py` (import name `tree_sitter`), tested by `tests/test_code_context.py`. Healthy. |
| `tui` | `textual` | Used in `dashboard/tui`, `tui/` (2+2 files); gated via `KNOWN_BROKEN["llm_router.tui.cli"]` in the M-11 test and `OPTIONAL` in the shipped-modules test. Healthy — deliberately unimportable from a bare install and tracked as such. |

No unused, duplicate, or stdlib-replaceable runtime dependency found. No undeclared
transitive dependency found **except** the two below, which are the headline finding.

---

## Scripts inventory (§8, every file in `scripts/`)

193 files under `scripts/`. `scripts/README.md` documents exactly 4 buckets —
`ci/`, `release/`, `bench/`, `dev/` — as canonical, run by CI (`.github/workflows/
{ci,benchmarks,self-audit}.yml`) or by hand per that README. It does **not** mention:
the ~25-file root-level duplicate cluster (DEAD-03), `scripts/routerarena/` (23 files,
separate from the documented `scripts/bench/routerarena/`), `scripts/groundtruth/` (19
files — used by the ground-truth accumulation pipeline referenced in this repo's own
CLAUDE.md and by `scripts/mutation_sample*.py`), the `gf_*.py` mutation-testing family
(8 files), or the many top-level `lint_*.py`/`measure_*.py`/`verify_*.py` one-off
scripts wired individually into CI or run manually (`lint_savings_sign.py` is the one
`self-audit.yml` calls; the rest were not individually traced — out of proportion to
this audit's remaining budget, and none showed signs of being dead on a name/import
check).

| Bucket | Files | Status |
|---|---|---|
| `scripts/ci/*` | 2 | Canonical, CI-wired. |
| `scripts/release/*` | 10 | Canonical, referenced by `scripts/README.md`, `ci.yml`, and this repo's own `CLAUDE.md` ("the path that works: `bash scripts/release/pre-release-verify.sh`"). |
| `scripts/bench/*` (incl. `bench/routerarena/`) | 8 | Canonical; `update_benchmarks.py` is CI-wired, `routerarena/` is what `submissions/routerarena/README.md` documents. |
| `scripts/dev/*` | 10 | Canonical per README; several duplicated by root (see DEAD-03). |
| **Root-level duplicates of the above 4 buckets** | **25** | **DEAD-03 — see below.** |
| `scripts/routerarena/*` (top-level, distinct from `bench/routerarena/`) | 23 | Undocumented in `scripts/README.md`; added 2026-09-12, actively developed (post-dates the DEAD-03 sync event, so it is NOT part of that cluster — it's organic parallel naming, not leftover fallout). Not independently verified further; flagged UNCERTAIN for a future pass, not classified dead. |
| `scripts/groundtruth/*` | 19 | Referenced by this repo's own `CLAUDE.md` ("`scripts/groundtruth/sources.py` owns both rules. Do not re-implement them.") — live, in-use tooling. |
| `scripts/gf_*.py` | 8 | Mutation-testing harness family (`gf_mutmut.py`, `gf_split.py`, etc.) — not individually traced; no evidence of being dead. |
| Misc top-level `lint_*.py` / `measure_*.py` / `verify_*.py` / `bench_*.py` | ~55 | `lint_savings_sign.py` is CI-wired (self-audit.yml); others not individually traced this round. |
| `scripts/routerarena_prep.py`, `scripts/routerarena_submit.py` | 2 | Root-level, undocumented, cross-reference only each other and `scripts/sync_downstream.py`. `routerarena_prep.py` was edited as recently as 2026-09-22 (commit `aff5e81`), so it is maintained, just undocumented — LOW-VALUE (doc gap), not dead. |
| `scripts/pre-release-checklist.py` | 1 | **DEAD-06 — see below.** |

---

## Findings register (`DEAD-` prefix)

### DEAD-01 — `pyyaml` unguarded on the MCP server's live import path, undeclared as a runtime dependency

```
ID: DEAD-01
Category: Dependency — undeclared, unguarded, on critical path
Severity: CRITICAL
Confidence: HIGH

Location:
  Files: src/llm_router/repo_config.py:21 (import site);
         src/llm_router/router.py:58 (import site);
         src/llm_router/tools/routing.py (imports router.py at module level);
         src/llm_router/server.py:37,57 (imports tools.routing / tools.routing.llm_route
           at module level)
  Symbols: `import yaml` (unguarded, module level, repo_config.py:21)
  Lines: repo_config.py:21; router.py:58; server.py:37,57

Observation: `src/llm_router/repo_config.py` does `import yaml` at module scope with
no try/except (line 21). `router.py` imports `repo_config.effective_config` at module
scope (line 58). `tools/routing.py` imports `router.py` at module scope. `server.py` —
the MCP server, i.e. the module every host (Claude Code, Cursor, etc.) actually starts
— imports `llm_router.tools.routing` at module scope (line 37) and
`llm_router.tools.routing.llm_route` again explicitly (line 57). `pyyaml` is not in
`[project.dependencies]` in pyproject.toml; it appears ONLY inside the `scripts`
optional extra (`scripts = ["httpx>=0.27", "pyyaml>=6.0"]`).

Evidence: `uv.lock` was walked for every core runtime dependency's full transitive
closure (`mcp`, `litellm`, `pydantic`, `pydantic-settings`, `aiosqlite`, `aiohttp`,
`structlog`, `fastapi`, `uvicorn`, `rich`, `requests`, and litellm's own sub-deps
`aiohttp`, `click`, `fastuuid`, `httpx`, `importlib-metadata`, `jinja2`, `jsonschema`,
`openai`, `pydantic`, `python-dotenv`, `tiktoken`, `tokenizers`) — none list `pyyaml`.
The only two packages in the whole lockfile that depend on `pyyaml` are `agno` (its own
optional extra) and `huggingface-hub` (a `tree-sitter`/`code-context`-adjacent
transitive, also an extra). A default `pip install llm-routing` therefore does not
guarantee `yaml` is importable.

Also unguarded at module scope, same class, reachable through the same or an adjacent
chain: `src/llm_router/policy.py:19`, `src/llm_router/okf.py:24` (imported by
`router.py` and `context.py`), `src/llm_router/control_plane/policy_bundle.py:7`.
`okf.py`'s import is moot once `repo_config.py` already fails first in the same import
of `router.py`, but it is an independent unguarded site in its own right.

Why this exists, if discoverable: This is the exact same defect class as the `rich`
regression (M-11) and the `requests` regression (same pyproject.toml comment block),
both already found and fixed. `pyyaml` was not caught a third time because every
developer/CI environment that has ever run this repo's test suite also had `dev` (which
pulls test tooling) and very plausibly `agno` and/or `scripts` extras installed, which
transitively or directly supply `yaml` — reproducing verbatim the M-11 docstring's own
diagnosis: "it worked for every developer because the dev environment installs it
transitively, which is exactly why nobody noticed."

Why this matters: `tests/install/test_m11_declared_dependencies.py` exists
specifically to catch this class and passed at the audited baseline (0 failures, 9,640
tests). Its `_declared()` helper unions `dependencies` with **every** entry across
**all** `optional-dependencies` groups before checking whether an import is "declared"
— so an import satisfied only by the unrelated `scripts` extra is treated as
core-declared. The guard the repo built to prevent exactly this bug has a hole shaped
exactly like this bug.

User-visible impact: On a machine with only the declared core dependencies installed
(no `scripts`, `agno`, `dev`, or `code-context` extra), starting the llm-router MCP
server — the primary way this product runs, per every host integration — raises
`ModuleNotFoundError: No module named 'yaml'` on the very first tool registration.
`llm-router status`/other CLI commands as well as `llm-router-quickstart`/`-onboard`
were not individually traced for the same chain, but `router.py` sits under enough of
the codebase that other entry points should be checked too before assuming this is the
only reachable site.

Engineering impact: Silent, environment-dependent failure with no test coverage — the
worst kind, because CI is green.

Is behavior currently used? YES (server startup path) — UNCERTAIN whether it has ever
actually failed for a real end user (depends on how they installed; `pip install
llm-routing` with no extras would trigger it; the documented install paths — README,
not audited in this domain — may or may not always include an extra).

Recommended action: SIMPLIFY / MOVE. Either (a) move `pyyaml` from the `scripts` extra
into `[project.dependencies]`, since it is plainly a core runtime need
(`repo_config.py`, `policy.py`, `okf.py`, `control_plane/policy_bundle.py`,
`model_registry.py`, `org_policy.py`, `dynamic_routing.py`, `user_routing_policy.py`,
`storage/adapters/yaml_adapter.py`, `commands/config.py`, `agents/registry.py` all
import it — 12+ files), or (b) wrap every one of those imports in the same
`try/except ImportError` degrade pattern already used correctly in
`storage/adapters/yaml_adapter.py` and `safe_config.py`. (a) is the smaller, safer
change given how load-bearing YAML config loading is here.
Proposed target: declare `pyyaml>=6.0` in `[project.dependencies]`; fix
`tests/install/test_m11_declared_dependencies.py::_declared()` to only count
`project.dependencies` (or add a second, stricter check) so this class cannot recur a
third time.

Behavioral compatibility risk: NONE (pure dependency-declaration fix).
Security risk: NONE.
Performance impact: NONE.
Estimated complexity removed: N/A (this is a bug fix, not a simplification) — but it
removes an entire undetected-failure-mode class.
Validation required: run the M-11 test suite after tightening `_declared()`; construct
a venv with ONLY `[project.dependencies]` installed and import `llm_router.server` to
confirm the crash before the fix and its absence after (not performed in this audit —
audit-only, no environment mutation permitted).
Dependencies on other findings: shares root cause and fix shape with DEAD-02.
```

### DEAD-02 — `httpx` unguarded and undeclared in core `src/`, currently masked by `litellm`'s transitive pull

```
ID: DEAD-02
Category: Dependency — undeclared, unguarded, currently accidentally satisfied
Severity: MEDIUM
Confidence: HIGH

Location:
  Files: src/llm_router/media.py:12; src/llm_router/tools/agoragentic.py:13;
         src/llm_router/control_plane/client.py:3
  Symbols: `import httpx` (unguarded, module level, all three sites)

Observation: All three imports are unguarded and module-level. `httpx` is declared
only in the `scripts` optional extra, exactly like DEAD-01.

Evidence: `uv.lock` shows `litellm`'s own dependency block lists `{ name = "httpx" }`
directly, and `litellm` is a core runtime dependency. So on a bare `pip install
llm-routing`, `httpx` IS present today — but only because litellm happens to depend on
it, not because llm-routing declares it. This is the identical shape the pyproject.toml
comment already documents for `requests` before it was fixed: "it resolved only
because litellm happens to pull it in transitively, which is not a dependency
declaration."

Why this matters: If litellm ever drops or version-bounds away its own httpx
dependency, these three modules — media/vision handling, the Agoragentic tool
integration, and the control-plane HTTP client — break with no warning and no test
catching it, for the same reason DEAD-01 wasn't caught: the `_declared()` blind spot.

User-visible impact: None today. Latent.

Engineering impact: A dependency the repo doesn't control (litellm's own dependency
graph) is silently load-bearing for three of this repo's own modules.

Is behavior currently used? YES, and currently works.

Recommended action: SIMPLIFY — declare `httpx` explicitly in `[project.dependencies]`
(it is already effectively guaranteed; this just makes the guarantee this repo's own
rather than borrowed).
Proposed target: add `httpx` to `[project.dependencies]`; same `_declared()` fix as
DEAD-01 covers both.
Behavioral compatibility risk: NONE. Security risk: NONE. Performance impact: NONE.
Validation required: same as DEAD-01.
Dependencies on other findings: DEAD-01 (same root cause, same fix).
```

### DEAD-03 — ~5,100 LOC root-level `scripts/` duplicate cluster, reintroduced by a sync commit 17 days after being cleaned up

```
ID: DEAD-03
Category: Dead/duplicate scripts
Severity: MEDIUM
Confidence: HIGH

Location: 25 root-level files in `scripts/` that duplicate a file of the same name in
`scripts/{ci,release,bench,dev}/`. Full list (root path → canonical path, line counts
from this baseline):
  scripts/analyze-violations.py → dev/ (108 vs 108 — byte-identical)
  scripts/cleanup-hook-health.py → dev/ (byte-identical)
  scripts/audit_demo.sh → dev/ (108 vs 108, 12 diff lines)
  scripts/demo_routing.py → dev/ (309 vs 309, 4 diff lines)
  scripts/gen_cast.py → dev/ (141 vs 141, 42 diff lines)
  scripts/generate-readme-svgs.py → dev/ (891 vs 891, 8 diff lines)
  scripts/router_isolation_test.sh → dev/ (191 vs 191, 52 diff lines)
  scripts/test_savings_realtime.sh → dev/ (58 vs 58, 12 diff lines)
  scripts/eval_classifier.py → bench/ (311 vs 319, 11 diff lines)
  scripts/benchmark.py → bench/ (232 vs 240, 26 diff lines)
  scripts/update_benchmarks.py → bench/ (32 vs 40, 17 diff lines; only bench/ is CI-wired)
  scripts/agoragentic_{deploy_serverless,publish_listing,register}.py → release/ (all differ)
  scripts/{publish-deprecation,publish-pypi}.sh → release/ (differ)
  scripts/release.sh → release/release.sh (93 vs 213 lines — release/ has grown substantially since the fork)
  scripts/sync-versions.py → release/sync-versions.py (123 vs 150)
  scripts/verify-release.py → release/verify-release.py (175 vs 175, 8 diff lines)
  scripts/pre-release-verify.sh → release/pre-release-verify.sh (97 vs 163)
  scripts/{verify-plugin-sync,verify-version-sync}.py → ci/ (differ; only ci/ is CI-wired)
  scripts/routerarena_submit.py → (cross-refs sync_downstream.py only)
Total: 25 files, ~5,100 LOC (`wc -l` sum).

Evidence (dated, single commit): `git log` shows every one of these 25 files' most
recent-before-this-commit-range edit lands on commit `57250744` / `5725074`
("13.0.0 — sync the upstream routing core, rebranded (#40)", 2026-08-19) — the SAME
commit for all 25, one exception (`routerarena_prep.py`, edited again 2026-09-22).
`scripts/README.md` — which documents ONLY the `ci/`, `release/`, `bench/`, `dev/`
buckets — was itself last touched by commit `1c6c6ebe` ("refactor(scripts): group 27
loose scripts into ci/release/bench/dev", 2026-08-02), 17 days EARLIER. The `ci/`,
`release/`, and `bench/` subdirectory copies kept receiving independent commits after
the Aug 19 sync (`bench/update_benchmarks.py` and `bench/benchmark.py` as recently as
2026-09-22; `ci/verify-version-sync.py` 2026-09-01; `release/pre-release-verify.sh`
2026-09-15) — i.e., every subdirectory copy is demonstrably the one that has kept being
maintained, and every root copy has been frozen since the moment it was reintroduced.

Why this exists: The Aug 2 refactor consolidated 27 loose root scripts into
subdirectories. The Aug 19 "sync the upstream routing core" commit (`#40`) — the same
commit type documented in `scripts/sync_downstream.py` as periodically pulling files
from an upstream repo — reintroduced root-level copies of scripts that upstream still
carries at its own root, because the sync tooling did not know the downstream
repository had already reorganized them. Nobody has run a cleanup pass since.

Why this matters: Some pairs have materially diverged (`release.sh`: 93 vs 213 lines;
`pre-release-verify.sh`: 97 vs 163 lines) — meaning fixes landed in one copy only.
Anyone who runs `bash scripts/release.sh` instead of `bash scripts/release/release.sh`
(both look equally plausible from the filename) gets the stale, 213-line-shorter
version. This repo's own CLAUDE.md already had to write down "`release.sh` is not the
release path" as a lesson learned from exactly this kind of ambiguity — the root
`release.sh` duplicate is a live instance of the trap that lesson is about.

User-visible impact: None to end users of the package (scripts/ ships to nobody — sdist
excludes it). Impact is entirely on maintainers who might run the wrong copy.

Engineering impact: 5,100 LOC of stale/drifted duplicate maintenance surface; risk of
"fixed it in one copy" recurring (this is literally what CLAUDE.md's R11 section calls
out as this repo's worst historical bug pattern — "fixes get applied call-site-by-
call-site instead of consolidated, so the same defect reappears in a new file").

Is behavior currently used? UNCERTAIN for the root copies specifically (nothing in
`.github/workflows/*` or `scripts/README.md` references them; `scripts/
pre-release-checklist.py` references the root `sync-versions.py`/`verify-version-
sync.py`, so at least that one internal caller exists — see DEAD-06). NO for CI.

Recommended action: DELETE the 24 root-level files that are pure stale duplicates
(keep `routerarena_submit.py`/`routerarena_prep.py` pending DEAD-06's resolution, since
`sync_downstream.py` and `pre-release-checklist.py` reference them). Before deleting,
diff each pair one more time to confirm no root-only fix exists that the subdir copy is
missing (none were found in this pass, but this list was built by naming pattern, not
by that direction of diff). Additionally: fix `sync_downstream.py`'s (or its upstream
counterpart's) exclude list so a future sync cannot reintroduce root-level copies of
already-relocated scripts.
Proposed target: `scripts/` contains only the 4 documented buckets plus
`groundtruth/`, `gf_*`, `routerarena/` (pending DEAD's UNCERTAIN item above), and the
long tail of individually-CI-wired lint/measure scripts.
Behavioral compatibility risk: LOW — nothing in CI or documented workflows points at
these paths.
Security risk: NONE. Performance impact: NONE.
Estimated complexity removed: ~5,100 LOC, 24 files.
Validation required: re-run `.github/workflows/{ci,benchmarks,self-audit}.yml`
locally/in CI after deletion; grep repo-wide one more time for any reference to the
root paths (only `pre-release-checklist.py` and `sync_downstream.py` found in this
pass).
Dependencies on other findings: DEAD-06 (pre-release-checklist.py itself should be
resolved first or in the same change).
```

### DEAD-04 — `control_plane/api.py` + `control_plane/reconciliation.py`: confirmed still PROVEN DEAD in the shipped package (M-10, reproduced independently)

```
ID: DEAD-04
Category: Dead code (module-level, excluded from wheel)
Severity: LOW (already tracked and mitigated; recorded here for completeness/cross-domain visibility)
Confidence: HIGH

Location: src/llm_router/control_plane/api.py (379 lines);
          src/llm_router/control_plane/reconciliation.py (111 lines)

Observation: Both modules do `from llm_router.control_plane import audit as cpa` at
module scope (api.py, top of file; reconciliation.py, top of file). No file named
`control_plane/audit.py` exists anywhere in this repository, in git history, or in the
`_quarantined_tests`. Both modules are listed in `[tool.hatch.build.targets.wheel]
exclude` in pyproject.toml (added at M-10, resolved 2026-09-22) and are independently
confirmed excluded by `tests/test_shipped_modules_import.py::
test_unshipped_modules_are_actually_excluded_from_the_wheel`. Nothing in `src/`
imports either module (grep confirmed; the only cross-reference is `api.py`'s own
lazy `from llm_router.control_plane.reconciliation import
reconcile_tenant_effective_policy` inside a function body — one dead module calling
the other dead module).

Why this exists: git blame shows both files arrived in the 2026-08-19 "sync the
upstream routing core" commit (`5725074`) — they are upstream/enterprise
control-plane code that references an enterprise audit-logging module this
distribution was never meant to carry, and nobody wrote a downstream-local
`audit.py` shim.

Why this matters: This is a live 490 LOC pocket of source that cannot be imported from
a source checkout and is deliberately excluded from every install. It is dead weight
in the repository (not in the shipped artifact).

User-visible impact: NONE (already excluded from the wheel).
Engineering impact: 490 LOC that any repo-wide refactor, linter, or `grep` has to
account for and mentally discard.
Is behavior currently used? NO in the shipped package; NO from a source checkout
either (both raise ImportError immediately).
Recommended action: KEEP for now (matches the existing M-10 ratchet's own stated
reasoning — "stubbing the import would silently disable audit logging in a control
plane, which is the opposite of what an audit module is for," and the decision to
ship `audit.py` or delete these two modules is explicitly called a packaging/product
decision, not a mechanical one). Recorded here as LEGACY, not re-litigated.
Proposed target: no change without a product decision on whether `control_plane/
audit.py` will ever be written for this distribution.
Behavioral compatibility risk: NONE either way (already non-functional).
Security risk: NONE. Performance impact: NONE.
Estimated complexity removed if deleted: 490 LOC + 11 dependent test files' control-
plane-adjacent setup would need re-checking (test_policy_runtime_swap.py,
test_cp_store_postgres.py, test_cp_store_sqlite.py, test_budget_lineage_
reconciliation.py, test_cp_signing.py, test_cp_migration.py, test_cp_policy_
bundle.py, test_router_control_plane_policy.py all touch sibling control_plane
modules that DO work — signing.py, store.py, events.py, schemas.py,
policy_bundle.py, migration.py, client.py, store_postgres.py — none of which are
excluded or broken; only api.py/reconciliation.py are dead).
Validation required: none beyond what M-10's own tests already provide.
Dependencies on other findings: none.
```

### DEAD-05 — `cost.py` "orphaned reporter" cluster (L-03), including the named lead `log_savings`: confirmed unchanged

```
ID: DEAD-05
Category: Dead public API (already ratcheted)
Severity: LOW (tracked, not new — recorded for this domain's completeness per the
brief's explicit lead)
Confidence: HIGH

Location: src/llm_router/cost.py — 8 of the 11 entries in
tests/test_l03_dead_public_api_ratchet.py::DEAD_PUBLIC_API live here:
`log_savings` (cost.py:3226), `format_spend_for_display`, `get_usage_summary`,
`log_quota_snapshot`, `get_router_efficiency`, `get_classifier_overhead`,
`get_cache_hit_stats`, `log_quality_trend`, `refresh_baseline_pricing_from_api`.
The other 3 (`derive_trace_id` in trace_id.py, `should_judge_inline`/`should_cascade`
in judge_cascade.py) live outside cost.py.

Observation: `grep -rn "log_savings"` across the whole repo (excluding
`_quarantined_tests`) finds it called only from `tests/test_cost_host.py` and
`tests/test_savings.py` — test-only callers, never from `src/`. This matches
`test_l03_dead_public_api_ratchet.py`'s own AST-based `_call_sites()` check, which
this audit re-ran by hand against `log_savings`, `get_usage_summary`, and
`refresh_baseline_pricing_from_api` and reproduced: zero call/import sites in `src/`.

Why this matters (repeated from the ratchet's own docstring, since this is the
audit's explicitly named lead): these are public names on a package published to
PyPI, so unilaterally deleting them is a breaking change for any downstream
importer — a product decision, not a mechanical one. The ratchet test intentionally
freezes rather than deletes.

Is behavior currently used? NO (confirmed independently).
Recommended action: DEPRECATE, not silently DELETE — matches the repo's own stated
policy. If a deletion pass is ever authorized, this is a straightforward one (8
complete, non-side-effecting reporter functions in one file); LOC not separately
measured beyond `cost.py`'s own size (cost.py is large — used by dozens of callers
for its live functions, so deleting piecemeal requires care, but each of these 8 is
independently removable).
Behavioral compatibility risk: MEDIUM if removed without a deprecation cycle (public
PyPI API surface). Security risk: NONE. Performance impact: NONE.
Validation required: `tests/test_l03_dead_public_api_ratchet.py` already re-validates
this on every run; nothing further needed unless a deletion is authorized.
Dependencies on other findings: none.
```

### DEAD-06 — `scripts/pre-release-checklist.py`: undocumented, uncalled, points at a file that does not exist

```
ID: DEAD-06
Category: Dead/broken script
Severity: LOW
Confidence: HIGH

Location: scripts/pre-release-checklist.py (whole file, 1 commit, 2026-08-19,
same commit as the DEAD-03 cluster)

Observation: Not listed in `scripts/README.md`. Not referenced by any `.github/
workflows/*.yml`. The only repo-wide references to its own filename are from itself.
Its own docstring/help text instructs: "Run this BEFORE `python scripts/
release.py <version>`" (line 13) and prints `python scripts/release.py {version}`
(line 484) — but `scripts/release.py` does not exist anywhere in this repository;
only `scripts/release/release.py` (a package-relative path) and `scripts/
release_helper.py` do. It also shells out to `scripts/verify-version-sync.py` and
`scripts/sync-versions.py` (line 353, 419) — the root-level DEAD-03 duplicates, not
the CI-wired `scripts/ci/` copies.

Why this exists: `scripts/sync_downstream.py`'s own comments confirm this repository
is the downstream target of an upstream sync, and that upstream's `release.py`
(root-level) is deliberately renamed to `release_helper.py` here to avoid colliding
with this repo's `scripts/release/` package. `pre-release-checklist.py` predates that
rename/reorg (or was itself pulled in by the same Aug 19 sync without adjustment) and
was never updated to point at the current release entry point.

Why this matters: a maintainer who finds this file and follows its own printed
instructions hits a `python: can't open file 'scripts/release.py'` dead end.

User-visible impact: NONE (dev tooling only).
Engineering impact: a documented-nowhere script giving wrong instructions is worse
than no script.
Is behavior currently used? NO (uncalled from CI or docs; only self-referential).
Recommended action: DELETE, or if its checklist content is still wanted, fold it into
`scripts/release/pre-release-verify.sh` (the actually-current, CI-referenced,
CLAUDE.md-endorsed pre-flight script) and update the reference to the real release
entry point.
Proposed target: removed; `scripts/release/pre-release-verify.sh` remains the one
documented pre-flight script.
Behavioral compatibility risk: NONE. Security risk: NONE. Performance impact: NONE.
Estimated complexity removed: ~500 lines (file not separately measured beyond earlier
`wc -l` sum which includes it).
Validation required: confirm no doc/onboarding material references this filename
(grep performed, clean) before deleting.
Dependencies on other findings: part of the DEAD-03 cluster's same root-cause commit.
```

---

## Deletion ledger (§49)

| Candidate | Type | Evidence | Depends on | Removal risk | Est. LOC | Confidence |
|---|---|---|---|---|---|---|
| 24 root-level `scripts/` duplicates (DEAD-03) | DUPLICATE/LEGACY | Single-commit reintroduction, dated `git log`, diverged from canonical | `sync_downstream.py`-style tooling should be fixed first to prevent recurrence | LOW | ~4,600 | HIGH |
| `scripts/pre-release-checklist.py` (DEAD-06) | DEAD/broken reference | No callers, points at nonexistent file | none | LOW | ~500 | HIGH |
| `cost.py` 8-function reporter cluster + `trace_id.derive_trace_id` + `judge_cascade.should_*` (DEAD-05 / L-03) | LEGACY (public API, needs deprecation) | Repo's own ratchet test, reproduced independently | PyPI breaking-change process | MEDIUM (public API) | not separately measured | HIGH |
| `control_plane/api.py` + `reconciliation.py` (DEAD-04 / M-10) | LEGACY (retain per existing decision) | Repo's own ratchet test, reproduced independently | product decision on `control_plane/audit.py` | N/A (retain) | 490 (if ever removed) | HIGH |

## Consolidation ledger (§50)

| Concepts | Current impls | Canonical | Deleted concepts | Risk |
|---|---|---|---|---|
| Release pre-flight verification | `scripts/pre-release-verify.sh`, `scripts/release/pre-release-verify.sh`, `scripts/pre-release-checklist.py` | `scripts/release/pre-release-verify.sh` (CI-wired, CLAUDE.md-endorsed) | root `pre-release-verify.sh`, `pre-release-checklist.py` | LOW |
| Version sync verification | `scripts/verify-version-sync.py`, `scripts/ci/verify-version-sync.py` | `scripts/ci/verify-version-sync.py` (CI-wired) | root copy | LOW |
| Plugin manifest sync verification | `scripts/verify-plugin-sync.py`, `scripts/ci/verify-plugin-sync.py` | `scripts/ci/verify-plugin-sync.py` (CI-wired) | root copy | LOW |
| Benchmark regeneration | `scripts/update_benchmarks.py`, `scripts/bench/update_benchmarks.py` | `scripts/bench/update_benchmarks.py` (CI-wired) | root copy | LOW |
| Core-runtime YAML/HTTP dependency declaration | Declared only in `scripts` extra, satisfied by luck (agno) or by litellm's transitive graph (httpx) | `[project.dependencies]` | none (additive fix) | LOW |

## Do-not-change register contribution

- `tests/test_l03_dead_public_api_ratchet.py`, `tests/test_shipped_modules_import.py`,
  `tests/install/test_m11_declared_dependencies.py`, `tests/test_r11_one_canonical_
  source.py` — these are exactly the kind of ratchet-with-teeth (anti-vacuity tests,
  frozen lists, dated docstrings citing the specific incident) this audit brief asks
  for elsewhere. Do not delete or weaken them; DEAD-01/02 recommend *tightening* one
  of them (`test_m11_declared_dependencies.py::_declared()`), not loosening it.

---

## Top items for synthesis (5–10 best candidates)

1. **DEAD-01** (`pyyaml` undeclared, unguarded, on the MCP server's live import path) —
   candidate for global Top-10 correctness risks. This is a real, evidenced,
   currently-live gap in a package marked "Production/Stable," of the exact same class
   the repo has already been bitten by twice.
2. **DEAD-02** (`httpx` same class, currently masked) — pair with DEAD-01 in any
   remediation; same one-line test fix closes both permanently.
3. The `test_m11_declared_dependencies.py::_declared()` blind spot itself (any extra
   satisfies "declared for core") — candidate for global testing-problems Top-10
   ("false confidence" class): a green ratchet test that cannot actually catch the
   next instance of the bug it was written for.
4. **DEAD-03** (5,100 LOC root scripts duplicate cluster) — candidate for global
   deletion ledger; clean, dated, single-root-cause, low-risk removal.
5. The **root cause behind DEAD-03**: an upstream→downstream sync process
   (`scripts/sync_downstream.py`) that can silently reintroduce files a downstream
   cleanup had already removed — candidate for a process/tooling fix, not just a
   one-time deletion, or the same duplication returns at the next sync.
6. **DEAD-06** (`pre-release-checklist.py`) — small but a clean, zero-risk deletion
   with a concrete "tells the reader to run a nonexistent file" defect.
7. **DEAD-04/DEAD-05** — not new, but confirmed accurate at this commit; worth citing
   in the synthesis as evidence that the repo's self-audit ratchets are holding, which
   should raise confidence in (not duplicate) the other 10 auditors' domains that rely
   on the same test suite being trustworthy.
