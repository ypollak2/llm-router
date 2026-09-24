# 01 — Recon, Repository Map, Metrics, Versioning/Release, Repository Hygiene

Auditor: domain 01 (Recon/Metrics). Baseline: worktree
`<worktree>`, detached HEAD `3c96d23`
(`v15.1.0-23-g3c96d23` — 23 commits past the last version tag). All commands
below were run read-only against this worktree; nothing in it was modified.
`LLM_ROUTER_BASH_INTERCEPT=off` was set for every shell command in this
session (the router's own output-compression hook would otherwise summarize
command output before it reached the auditing model — a real risk when the
subject being audited is the router itself).

Note on scope: the brief asked me to also read this worktree's `CLAUDE.md`.
No such file exists here — it is deliberately gitignored
(`0754cc9 chore: gitignore CLAUDE.md (local AI instructions, not for repo)`),
confirmed absent by a full-tree `find`. Per the coordinator, I instead read it
read-only from the live repo at `<repo>/CLAUDE.md`.
It documents this project's own measurement failures (denominator errors on
routing-rate claims, benchmark traffic contaminating the ground-truth corpus,
`x or 0` turning "unknown" into a comparable number, `release.sh`'s dead PyPI
path, wall-clock corruption from macOS sleep). None of it changes this
domain's findings, but two of its lessons are directly relevant and are
applied below: (1) don't trust a green CI job without checking what it
actually scoped, and (2) a release process's own claims need the same
evidence bar as a product claim.

---

## §3 Repository map

| Path | Responsibility | Evidence |
|---|---|---|
| `src/llm_router/` | The package (415 `.py` files, 131,781 lines). 31 subpackages + 189 files flat at package root. | `find`/`wc` below |
| `hooks/` (root) | **Generated bundle**, byte-identical mirror of `src/llm_router/hooks/*.py` (14 of 16 files diffed identical), produced by `scripts/build_plugin_bundle.py` for the Claude/Codex/Factory plugin marketplace format, which requires flat files at fixed paths. Two files renamed in the bundle (`llm_router_hook_payload.py`, `llm_router_tool_surface.py`). | `diff -q` per file; `scripts/build_plugin_bundle.py:44,73,141` builds `hooks.json`/`.mcp.json`/the file map |
| `.claude-plugin/`, `.codex-plugin/`, `.factory-plugin/` | Per-host plugin manifests, also produced by `build_plugin_bundle.py`; each carries its own copy of the version string (`15.1.0`, matching `pyproject.toml` at HEAD). | `grep -n version .claude-plugin/*.json` |
| `commands/` (root, 44 files under `src/llm_router/commands/`) | CLI subcommand implementations, dispatched from `cli.py`'s manual if/elif chain (see §3.1) | — |
| `tools/` (17 files under `src/llm_router/tools/`) | MCP tool implementations. Server module's own docstring: *"All 60 tools are registered by modules in llm_router/tools/"* (`src/llm_router/server.py:3`). | `server.py:1-58` |
| `hosts/` | Per-coding-host integration modules (cursor, gemini_cli, hook_io, …) | `src/llm_router/hosts/` |
| `agentic/`, `agents/` | Two separate packages — agentic execution/ledger machinery vs. agent-framework adapters; not the same concept (flagged for the semantic-duplication auditor, not resolved here) | `src/llm_router/agentic/` (12 files), `src/llm_router/agents/` (5 files) |
| `frameworks/` | Third-party agent-framework adapters (agno, crewai, langgraph, openai_agents, pydantic_ai, hermes, claude_agent_sdk) — all zero-inbound from internal code (§5); gated behind optional `pyproject.toml` extras (`agno = ["agno>=2.5.14"]` confirmed; others not verified as declared extras — flag for §26 dependency auditor) | `pyproject.toml:71-73` |
| `control_plane/` | Multi-tenant policy-push server/client (11 files); several modules zero-inbound (§5) | `src/llm_router/control_plane/` |
| `_quarantined_tests/` | 9 test files + `README.md` + `TRIAGE_2026-09-15.md`, held since the "13.0.0 sync" (2026-08-19) because upstream renamed/removed the symbols they import. Actively triaged, not abandoned (§4). | see §4 table |
| `tests/` | 783 files, 141,575 lines | `find`/`wc` |
| `scripts/` | 162 `.py` files, 36,777 lines — CI/release/bench/dev scripts, grouped since `1c6c6eb` (2026-08-02, "group 27 loose scripts into ci/release/bench/dev") | `find scripts -name '*.py' \| wc -l` |
| `docs/`, `guide/`, `architecture/` | 54 markdown files, 10,986 lines. `docs/` is gitignored except `BENCHMARKS.md` and `releases/` (deliberate, per `audit/CHECKPOINT_2026-09-24.md` §1.3 — "closed as accepted") | `.gitignore:111` `/docs/*` with two negations |
| `audit/` | 53 tracked files, 872 KB — three generations of prior forensic audits, still committed at HEAD. See §37. | `git ls-files audit/ \| wc -l` |
| `submissions/routerarena/` | RouterArena benchmark-submission harness (19 files: router variants, configs, PR bodies) — a real product artifact, not debris | `find submissions -maxdepth 2` |
| `.github/workflows/` | 8 workflows: `ci.yml`, `publish.yml`, `benchmarks.yml`, `binary.yml`, `hol-skill-validate.yml`, `plugin-scan.yml`, `self-audit.yml`, `update-stats.yml` | `ls .github/workflows` |
| `.mcp.json` | Points at `npx -y llm-routing` (npm-published wrapper), not the local source — this is the manifest a *consumer* would install, matching the npm package under `npm/` | `.mcp.json` |
| `KIMI.md`, `.kimi/mcp.json`, `.rules`, `glama.json` | Per-host routing-rule/config files (Kimi Code, Trae IDE, Glama MCP directory listing) — legitimate multi-host config, not clutter | read directly |

### §3.1 CLI dispatch mechanism (corrects an assumption made before reading the code)

`cli.py:main()` is **not** `argparse`-based. It is a manual `if/elif` chain on
`sys.argv[1:]`. A `frozenset` at `cli.py:837-887` (`_KNOWN_SUBCOMMANDS`, 57
names) exists **only** to power a "did you mean" typo suggestion — its own
comment says so explicitly: *"NOT a second source of truth for dispatch
itself, so it can drift without breaking anything except the suggestion
quality. Keep it roughly in sync with the if/elif chain."* This is a
self-documented, low-severity duplication (LOW: worst case is a wrong typo
suggestion, not a routing bug) — noted for §40/§27, not scored as a defect
here. Additionally, `pyproject.toml:92-97` (`[project.scripts]`) declares 5
independent console-script entry points: `llm-router`, `llm-router-onboard`,
`llm-router-install-hooks`, `llm-router-quickstart`,
`llm-router-isolation-test` — plus a documented **deliberate absence**:
*"there is deliberately NO `llm-router-sse` script... server.main_sse binds
the full MCP tool surface with no auth."*

### §3.2 MCP registration mechanism

`src/llm_router/server.py:31` imports `from mcp.server.mcpserver import
MCPServer` (not `fastmcp` — `pyproject.toml`'s dependency comment explains
this is load-bearing: `mcp>=2.0.0,<3.0.0` because 2.x removed
`mcp.server.fastmcp`). `mcp = MCPServer("llm_router", lifespan=_lifespan)` at
`server.py:83`. Individual tools are plain `async def` functions in
`tools/*.py` imported by name into `server.py` (e.g. `tools/routing.py:257
async def llm_route`) rather than decorated in place with a grep-able
`@mcp.tool()` — a literal-decorator grep undercounts real tool count by
construction (4 hits, all in the optional `agoragentic.py` module) and is not
a reliable census method for whoever owns §29; use the module docstring's
stated count (60) plus manual enumeration of `tools/*.py` top-level
`async def`/`def` (150 total across the package, which is an upper bound,
not the registered-tool count — includes private helpers).

### §3.3 Hook registration mechanism

Hooks are subprocess entry points, not Python imports — consistent with the
brief's "hook scripts run as files" warning. `install_hooks.py` contains two
near-identical tuples (`~line 692-707` and `~line 721-730`) mapping hook
filename → installed name → Claude-Code hook event → matcher, one for
Claude Code and one for a second host (Claude Desktop or similar, name
truncated in my read — needs the semantic-duplication auditor to confirm
these two tables can't drift, since they are hand-kept like the CLI
dispatch/suggestion pair above). Confirmed events actually used:
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`.

---

## §4 Baseline

The provided baseline (`audit/forensic_2026-09-24/00_baseline_pytest.log`)
covers pytest only: **9,640 tests, 0 failed, 0 error, 200 skipped.** I ran the
remaining baseline checks the brief asks for, read-only:

| Check | Command | Result |
|---|---|---|
| Lint, whole tree | `ruff check .` | **201 errors** (see breakdown below) |
| Lint, CI's actual scope | `ruff check src/ tests/` (matches `.github/workflows/ci.yml:28`) | **0 errors** |
| Format check, whole tree | `ruff format --check .` | **1,235 files would be reformatted, 153 already formatted** |
| Type checker | — | **None configured.** `pyproject.toml` has no `[tool.mypy]`/`[tool.pyright]` section, no such dependency, and `.github/workflows/` has no type-check step. This is a fact, not a gap I'm inferring — grepped `pyproject.toml` for `mypy\|pyright` with zero hits. |

**The 201 whole-tree lint errors are entirely outside CI's scope**
(`ruff check src/ tests/` alone returns 0; `ruff check _quarantined_tests
scripts` alone returns all 201). Breakdown: 54 `F541` (f-string missing
placeholders), **44 `invalid-syntax`**, 26 `E702`, 24 `F401` (unused import),
20 `E701`, 10 `E401`, 10 `F841` (unused variable), 6 `E402`, 6 `E741`, 1
`F811`.

**The 44 `invalid-syntax` findings are a real, reproduced defect, not a lint
opinion.** They are concentrated in three files:
`scripts/bench_session_replay.py`, `scripts/gen_cast.py`,
`scripts/dev/gen_cast.py`. I confirmed independently of ruff, with the
project's own pinned interpreter:

```
$ HOME=$(mktemp -d) <repo>/.venv/bin/python -m py_compile scripts/gen_cast.py
  File "scripts/gen_cast.py", line 31
    f"  {'\"what does os.path.join do?\"':<46}  ...
SyntaxError: f-string expression part cannot include a backslash
```

`.venv/bin/python --version` → **3.11.15**; `pyproject.toml requires-python =
">=3.11"`; `[tool.ruff] target-version = "py311"`. These three scripts use a
Python-3.12-only f-string feature (a backslash/quote-reuse inside the
expression part) and **cannot be imported or executed at all** under the
project's own declared minimum and its own pinned dev venv. This is
`REC-004` below.

**This exact defect was already found and documented by the prior audit two
days earlier** (`audit/2026-09-22/DEAD_AND_SUSPICIOUS_CODE.md:107-109`:
*"Three files cannot be parsed by the project's own Python... They cannot
run."*) and is **still present, unfixed, at HEAD**, across 52 intervening
commits including a dedicated `15.0.0` and `15.1.0` remediation release. See
`REC-004` and the synthesis note at the end of this file.

### Quarantined tests — why each one is quarantined (not obsolete-by-default)

`_quarantined_tests/` is actively triaged, most recently by its own
`TRIAGE_2026-09-15.md`. Two files it recommended deleting (`test_claim_evidence.py`,
`test_surface_status.py`) are **already gone** from the directory at HEAD —
i.e. the triage's own recommendations were partially acted on, which is
evidence this is a maintained holding pen, not abandoned debris.

| File | Reason quarantined | Current status (per `TRIAGE_2026-09-15.md`) | Classification |
|---|---|---|---|
| `test_audit.py` | imports a symbol (`cmd_audit`) the 13.0.0 upstream sync removed | import error; upstream carries 11 replacement tests, diff not yet done | GENUINE INTEGRATION DEBT |
| `test_budget_envelope.py` | pre-sync `budget_envelope` API | 15 failing; KEEP, confirm upstream covers the behaviour | GENUINE INTEGRATION DEBT |
| `test_classify.py` | imports a symbol (`CONFIDENCE_THRESHOLD`) the sync removed | import error, not yet diffed against upstream's 1 replacement | GENUINE INTEGRATION DEBT |
| `test_deep_reasoning_classifier.py` | loads a repo-root benchmark tree (`bench/routerarena/...`) the sync doesn't carry, by file path (invisible to the sync's import-based skip detector) | 45 failing; "largest block of assertions here; needs an API diff first" | GENUINE INTEGRATION DEBT + a documented blind spot in the sync tool's own dependency detection |
| `test_routerarena_submit.py` | same repo-root-benchmark-tree issue | 6 skipped cleanly | GENUINE INTEGRATION DEBT |
| `test_signals.py` | imports a symbol (`detect_pii`) the sync removed | import error, not yet diffed | GENUINE INTEGRATION DEBT |
| `test_subscription_local.py` | pre-sync subscription-local routing API | 14 failing; KEEP | GENUINE INTEGRATION DEBT |
| `test_summary.py` | `observability/` package moved under it post-sync | 7 failing; KEEP | GENUINE INTEGRATION DEBT |
| `test_team.py` | imports a symbol (`_add_ws23_context`) the sync removed | import error, not yet diffed | GENUINE INTEGRATION DEBT |

None of these are FLAKY, ENV, or ABANDONED in the brief's taxonomy — all nine
are the same root cause (the 2026-08-19 upstream sync renamed/relocated the
symbols they exercise) and all nine are explicitly still-open work items in
the directory's own tracking file, not silent bit-rot. The one process risk:
`TRIAGE_2026-09-15.md` itself is now over a week stale relative to this
audit's date (2026-09-24) with the "diff against upstream" step still
unstarted for 8 of 9 files — a second follow-up triage is due.

---

## §5 Metrics

| Metric | Count | Method |
|---|---|---|
| `src/llm_router/**/*.py` files | 415 | `find` |
| `src/llm_router` lines | 131,781 | `wc -l` |
| `tests/**/*.py` files | 783 | `find` |
| `tests/` lines | 141,575 | `wc -l` |
| `_quarantined_tests/**/*.py` files | 9 | `find` |
| `scripts/**/*.py` files | 162 | `find` |
| `scripts/` lines | 36,777 | `wc -l` |
| `docs/+guide/+architecture/` markdown files | 54 | `find` |
| `docs/+guide/+architecture/` lines | 10,986 | `wc -l` |
| Files flat in `src/llm_router/` root (no subpackage) | 189 | `find -maxdepth 1` |
| Subpackages under `src/llm_router/` | 31 (3 with zero `.py` files: `static/` = 1 HTML asset, `rules/` = 14 markdown host-rule files, `policies/` = 6 YAML policy files — legitimate data bundles, not dead packages) | `find -type d` + per-dir `.py` count |
| Top-level `def`/`class` (public, non-`_`) in `src/` | 1,466 | `grep -E '^(def \|class )[A-Za-z]'` (module-level only; undercounts nested) |
| `LLM_ROUTER_*` env vars — via canonical `env_registry.py` | **207 declared**, self-scoped to `src/` only; module's own docstring states 195 were independently measured across 313 call sites as of 2026-09-22, and a further 18 exist only in `scripts/`, out of this registry's stated scope | `src/llm_router/env_registry.py:12-14,19` |
| `LLM_ROUTER_*` env vars — raw `os.environ.get`/`os.getenv` literal calls in `src/` | 183 | `grep -oE 'os\.(environ\.get\|getenv)\("LLM_ROUTER_...")'` |
| `LLM_ROUTER_*` token occurrences anywhere in `src/` (includes comments, strings, non-getenv reads) | 292 | raw `grep -o` |
| SQLite `CREATE TABLE` names, `src/` | ~48 distinct (2 grep artifacts excluded: `and`, `IF` from multi-line statements) | `grep -hoE 'CREATE TABLE...'` |
| MCP tools | 60, per `server.py`'s own docstring (not independently re-derived — decorator-grep is unreliable here, see §3.2) | `server.py:3` |
| Console-script entry points | 5 (+1 deliberately absent, documented) | `pyproject.toml:92-97` |
| CLI subcommands (typo-suggestion list, NOT authoritative) | 57 | `cli.py:837-887` |

### 25 largest files, `src/llm_router/` (by line count)

```
5359  router.py
4870  hooks/auto-route.py
4296  cost.py
2513  hooks/session-end.py
2111  install_hooks.py
1757  hooks/enforce-route.py
1706  tools/admin.py
1702  commands/doctor.py
1533  commands/install.py
1437  hooks/session-start.py
1397  okf.py
1338  dashboard/server.py
1223  hooks/agent-route.py
1209  cli.py
1087  ui/session_summary.py
1009  dashboard/tui.py
1006  session_store.py
 976  config.py
 971  retrospective.py
 966  tools/text.py
 943  tools/routing.py
 868  gateway.py
 864  dashboard_data.py
 861  budget_backend.py
 846  hooks/agent_loop.py
```

`router.py` (5,359 lines, 58 internal imports — the single most
import-heavy module in the package) and `hooks/auto-route.py` (4,870 lines,
30 internal imports) are both routing-critical (§13's domain) and both
oversized by any conventional threshold; noting for the structure/routing
auditors rather than scoring here.

### Import-graph metrics (`src/llm_router` only; stdlib `ast`, no third-party tool)

Method: parsed every `.py` file under `src/llm_router/` with `ast`, resolved
`import llm_router.X` / `from llm_router.X import ...` / relative imports
against the 415 known module names, built a directed graph. **0 parse
errors** (all 415 files are valid Python 3.11 syntax when read via `ast` —
note this differs from the `ruff`/`py_compile` findings above, which apply
only to `scripts/`, outside `src/`).

**Top 25 most-imported internal modules (inbound):**

```
133 llm_router            (the package itself — __init__ re-exports)
 58 llm_router.types
 43 llm_router.config
 31 llm_router.logging
 29 llm_router.cost
 28 llm_router.tool_surface
 21 llm_router.savings
 14 llm_router.paths
 14 llm_router.router
 12 llm_router.codex_agent
 11 llm_router.semantic.scope
 11 llm_router.profiles
 10 llm_router.secret_scrubber
 10 llm_router.install_hooks
 10 llm_router.claude_usage
 10 llm_router.agentic.ledger
  9 llm_router.policy
  8 llm_router.sqlite_wal
  8 llm_router.routing_quality
  8 llm_router.safe_subprocess
  7 llm_router.token_budget
  7 llm_router.calibration
  7 llm_router.capabilities
  7 llm_router.discover
  7 llm_router.classify
```

**Top 10 highest-outbound (imports the most internal modules — complexity
concentration, not a defect by itself):** `router.py` (58), `cli.py` (55),
`hooks/auto-route.py` (30), `tools/admin.py` (28), `server.py` (21),
`tools/routing.py` (20), `hooks/session-end.py` (17), `cost.py` (13),
`tools/text.py` (12), `tools/agentic.py` (10).

**Zero-inbound modules (no other `src/llm_router` module imports them):
117 of 415 (28%).** This number **overstates** dead code by construction —
the brief's own rule ("no direct import ≠ dead") applies directly here.
Breaking the 117 down by why a static import scan can't see the caller:

| Class | Count (approx.) | Why zero-inbound is expected | Evidence |
|---|---|---|---|
| `hooks/*.py` (28 files: `auto-route`, `enforce-route`, `session-start`, `session-end`, `agent-route`, etc.) | 28 | Run as subprocess entry points via Claude/Codex/Gemini hook config, never Python-imported | §3.3 |
| Package `__init__.py` roots (`agentic`, `dashboard`, `data`, `decisions`, `frameworks`, `hosts`, `integrations`, `library`, `memory`, `monitoring`, …) | ~12 | Expected — a package's own `__init__` is imported *as the package*, which this scan counts under the dotted package name, not the `__init__` file itself | — |
| `frameworks/*.py` (agno, crewai, langgraph, openai_agents, pydantic_ai, hermes, claude_agent_sdk) | 7 | Third-party framework adapters, plausibly loaded only when that framework is present; only `agno` confirmed as a declared optional extra (`pyproject.toml:71-72`) | needs §26 owner to confirm the other 6 are gated the same way, or are aspirational/unwired |
| `control_plane/{api,client,signing,store_postgres}.py` | 4 | Multi-tenant control-plane feature; `store_postgres.py` has a test (`tests/test_cp_store_postgres.py`) but zero production callers found anywhere (CLI, server, or other `src/` module) | grepped whole repo, not just `src/` |
| `migrations/versions/001_create_llm_router_health.py` | 1 | Migration runner likely loads these by directory scan / filename convention, not static import — needs confirmation from whoever owns §35 | — |

**Genuinely unresolved (UNCERTAIN, not DEAD) — flagged for the dead-code /
duplication auditors, each already raised by a PRIOR audit and still
zero-inbound today:**

- `feedback_handler.py` — zero inbound in `src/`; only reference outside its
  own file and its own test is in **`audit/2026-09-21/*.md`** (4 prior-audit
  documents), i.e. it was flagged before and nothing changed.
- `gateway_service.py` — same pattern: zero inbound; referenced only by its
  own test, `architecture/CURRENT_ARCHITECTURE.md`,
  `architecture/TARGET_ARCHITECTURE.md`, and **6 files in
  `audit/2026-09-21/`**.
- `budget_lineage_reconciliation.py` — zero inbound; referenced only by its
  own test, `conftest.py` (a fixture, not a call site), and **9 files across
  `audit/2026-09-21/` and `audit/2026-09-22/`**, including
  `DEAD_AND_SUSPICIOUS_CODE.md` in both.
- `commands/admin_actions.py`, `contamination_audit.py` — zero inbound from
  `src/`; each has exactly one test file and (for `contamination_audit.py`)
  a handful of `scripts/` consumers, which may import it dynamically or by
  subprocess — not confirmed either way in the time available.

These four/five modules being independently flagged by **two separate prior
audit generations** (2026-09-21 and 2026-09-22) and still showing zero
inbound callers today is the strongest single "known finding never acted on"
pattern in this domain — see the synthesis note.

---

## §7 Product core map — first pass

Classification basis: inbound-import centrality (§5), presence in
`[project.scripts]` / MCP tool surface / hook registration (reachable at
runtime), and whether the module is exercised only by its own tests.

| Classification | Contents | Evidence |
|---|---|---|
| **CORE** | `router.py`, `cli.py`, `server.py`, `config.py`, `types.py`, `cost.py`, `logging.py`, `savings.py`, `classify.py`, `policy.py`, `tool_surface.py`, `paths.py`, `secret_scrubber.py`, `hooks/auto-route.py`, `hooks/enforce-route.py`, `hooks/session-start.py`, `hooks/session-end.py`, `install_hooks.py`, `env_registry.py` | Top of the inbound-import ranking (§5) and/or a registered hook event (§3.3) and/or a console-script entry point |
| **SUPPORTING** | `commands/*` (44 files — CLI feature surface), `tools/*` (17 files — MCP feature surface), `hosts/*`, `dashboard/*`, `ui/*`, `semantic/*`, `signals/*` | Reachable from CORE via CLI dispatch or MCP registration, each with real tests, but individually replaceable without breaking the routing path |
| **OPTIONAL** | `frameworks/*` (7 files, gated by extras), `control_plane/*` (11 files — multi-tenant policy push, opt-in), `benchmark/*`, `agents/*` | Zero or near-zero inbound from CORE/SUPPORTING; exist behind an extras flag or a separate CLI subtree |
| **EXPERIMENTAL** | `submissions/routerarena/*` (not under `src/`, a standalone benchmark-submission harness), `_quarantined_tests` (its subjects) | Explicitly marked as such by their own README/location |
| **LEGACY** | `service.py` (a FastAPI HTTP service kept alongside the MCP server; actively touched as recently as 2026-09-22 so not dead, but its hardcoded `"5.3.0"` version — see §36 — suggests it receives less release-process attention than the MCP path) | git log + §36 evidence |
| **DEV-ONLY** | `scripts/*` (162 files: bench/ci/release/dev), `conftest.py`, `setup.cfg`'s `[mutmut]` mutation-testing config | Not shipped in the wheel (needs §26 confirmation via `[tool.hatch.build.targets.wheel]` include list, not verified here) |
| **DOC-ONLY** | `docs/`, `guide/`, `architecture/`, `audit/` | Markdown, no runtime effect |
| **UNCERTAIN (candidate DEAD or OPTIONAL, not resolved here)** | `feedback_handler.py`, `gateway_service.py`, `budget_lineage_reconciliation.py`, `commands/admin_actions.py`, `control_plane/store_postgres.py` | §5 zero-inbound list, cross-referenced against 2 prior audits that raised the same modules |

This is explicitly a first pass for whoever runs the full §7 exercise —
it is accurate at the "which subsystem" level but does not attempt the
95%-of-user-value sizing exercise the full brief asks for, which requires
usage/telemetry data (§17/§33's domain) this pass did not pull.

---

## §36 / §73 Versioning, release, and one-source-of-truth (version)

**The good pattern, confirmed:** `src/llm_router/__init__.py:25-46` computes
`__version__` dynamically — tries `pyproject.toml`'s `[project].version`
first (source checkout), falls back to `importlib.metadata.version("llm-routing")`
(installed wheel), falls back to `"0.0.0+unknown"`. Its own comment states
the rationale explicitly: *"Report the version of the code that is ACTUALLY
RUNNING... Distribution metadata is only refreshed by `pip install`, so a
source checkout must read the live file."* `cli.py:904-905` and
`session_spend.py:88-90` both consume this correctly. This is a genuine
single source of truth, done deliberately, and should be preserved as-is.

**Two confirmed violations of that source of truth (not hypothetical —
reproduced by reading the exact lines):**

1. `src/llm_router/service.py:42` — `FastAPI(title="llm_router-service",
   version="5.3.0")`
2. `src/llm_router/service.py:329` — the `/health` endpoint literally
   returns `{"status": "ok", "version": "5.3.0", "pid": os.getpid()}`

Both are hardcoded string literals that bypass `llm_router.__version__`
entirely. The real version at HEAD is `15.1.0` (23 commits further, see
below) — **`5.3.0` is not a rounding error, it is roughly ten major versions
stale**, on a file that was last touched 2026-09-22 (2 days before this
audit, in a fix commit) and is actively used (`service_manager.py` launches
it; `tests/test_adaptive_router.py`, `tests/test_sidecar_service.py` exercise
it) — this is not dead legacy code, so it is not merely an artifact of
neglect; it is a live health-check endpoint reporting the wrong version to
anything that polls it. `REC-001` below.

One further version literal, lower confidence: `cli.py:365`, a
Gemini-extension manifest writer, hardcodes `"version": "9.0.1"` for the
generated `gemini-extension.json`. This may be an independent "extension
manifest schema version" rather than the package version — I did not find
Gemini's own extension-manifest-versioning convention documented anywhere in
this repo to confirm either way. Flagged UNCERTAIN, not scored as a defect.

**HEAD is 23 commits past the last version tag.** `git describe --tags
3c96d23` → `v15.1.0-23-g3c96d23`. Those 23 commits (`git log
v15.1.0..3c96d23`) touch 63 files (+6,502/-107 lines) and include real,
user-facing fixes: a Codex-adapter stdout-parsing bug, an enforcement-hold
duration bug, a reverted enforcement-exemption attempt, and the savings
"unverified" reclassification that is this exact commit's own message.
`pyproject.toml`'s `version = "15.1.0"` (and its two mirrors in
`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`) has not
moved. This is not a one-off: `audit/FROZEN_STATE.md` (written for the prior
audit two days earlier) records the **same shape of drift** — that audit's
subject HEAD (`357a402e`) was itself "not a released state," 15 commits past
`v14.1.0`. **This repo has now shipped/audited an untagged, unreleased HEAD
at least twice in a row** — worth stating as a recurring release-process gap
rather than a one-off oversight. `REC-002` below.

**Related, from the live repo's own `CLAUDE.md` (read per the coordinator's
instruction) and confirmed still relevant:** the PyPI publish path switched
to OIDC (`e9d7710`, inside the 23 unreleased commits) but the OIDC trusted
publisher is **not yet registered on PyPI** per
`audit/CHECKPOINT_2026-09-24.md §1.1` — meaning the next `git tag vX.Y.Z`
will build and then fail to upload until that one manual step (PyPI project
settings, requires login) is done. This is a release-mechanics finding, not
a code defect, but it is exactly the kind of thing that turns a version bump
into a broken release; noting it here since it sits directly on this
domain's "versioning/release" boundary.

Git tag sequence itself is clean and monotonic (`v15.1.0 → v15.0.1 → v15.0.0
→ v14.1.0 → v13.3.2 → ...`) — the tagging mechanism is not the problem; the
gap between "last tag" and "HEAD" and the untracked hardcoded literals are.

---

## §37 / §38 Repository hygiene and git archaeology

### The `audit/` directory itself is the largest hygiene item in this domain

53 tracked files, 872 KB, committed at HEAD — not just present in a
gitignored scratch dir. Three generations:

1. `audit/2026-09-21/` (11 files) — first forensic audit.
2. `audit/2026-09-22/` (12 files) — second forensic audit, explicitly
   distrustful of the first: `audit/README.md:84-86` instructs future
   readers *"Do not trust... the prior audits in `2026-09-21/` and
   `2026-09-22/`. Those audits were wrong at least twice about their own
   numbers — record where they are wrong."*
3. `audit/00_*.md` through `audit/28_*.md` plus `FROZEN_STATE.md`,
   `CHECKPOINT_2026-09-24.md`, two `REMEDIATION*_RUN.md` — the third,
   current-successor audit (the one this very task is a continuation of).

**Classification: LEGACY, not PROVEN DEAD.** The 2026-09-21 and 2026-09-22
subdirectories are explicitly superseded and explicitly distrusted by their
own successor's index, but no single document fully replaces their
line-by-line content, so deleting them outright would discard a record of
what was found and fixed (useful for exactly the "known finding never acted
on" pattern this report keeps surfacing). **Recommendation for the
deletion/consolidation ledger: archive (e.g. squash into one
`audit/history/` file per generation with a one-paragraph "superseded,
kept for record" header) rather than delete outright** — matches the
brief's own DELETE > MERGE preference where MERGE preserves more.
`REC-003`.

**`audit/README.md`'s own artifact table cites four files that do not
exist and never did.** `16_CONFIGURATION_AUDIT.md`, `17_PERFORMANCE_AUDIT.md`,
`19_FAILURE_MODE_MATRIX.md`, `20_SIMPLICITY_AUDIT.md` are listed against
phases 33, 35-36, 46, 47 ("wave 2... informed by what wave 1 finds"). `git
log --diff-filter=A` and `--diff-filter=D` both return **empty** for all
four paths — they were never added, so they weren't deleted either; wave 2
of that audit's own plan was apparently never fully executed, or was folded
into other files without updating the index. This is a documentation-accuracy
defect in the audit corpus itself, not in the product. `REC-005`.

### Other root/hygiene items checked

| Item | Finding | Verdict |
|---|---|---|
| `intercept_bench.json` (root, 276 lines) | Generated output of `scripts/bench_intercept.py`, last touched 2026-09-15 (`ccee00d`), committed at repo root, not referenced by any doc, test, or CI step as an expected/checked artifact | LOW-VALUE generated artifact sitting at root instead of `docs/BENCHMARKS.md`'s tracked convention or being gitignored — small, `REC-006` |
| `KIMI.md`, `.kimi/mcp.json` | Kimi Code host routing rules/config | KEEP — legitimate per-host config, this product supports many IDE/CLI hosts |
| `.rules` | Trae IDE routing rules | KEEP — same reason |
| `glama.json` | Glama MCP directory listing manifest | KEEP — external distribution requirement |
| `submissions/routerarena/` | RouterArena benchmark submission harness, real and current (referenced in `CHECKPOINT_2026-09-24.md`) | KEEP |
| SQLite table naming | Several tables exist in both a bare and an `llm_router_`-prefixed form (`envelopes`/`llm_router_envelopes`, `cp_heartbeats`/`llm_router_cp_heartbeats`, `cp_instances`/`llm_router_cp_instances`, `cp_tenant_active_policy`/`llm_router_cp_tenant_active_policy`, `cp_tenants`/`llm_router_cp_tenants`) | Not resolved here — flagged for the §16 storage auditor; could be an in-progress rename, two genuinely separate tables (local vs. control-plane), or drift |

### Git archaeology note

The pattern across both this domain's version finding and the hygiene
finding above is the same shape: **this repository has, at least twice,
audited or shipped a HEAD state that was already known to be ahead of its
own released/indexed reference point**, and at least one specific defect
(`scripts/gen_cast.py` + 2 siblings failing to parse) was found once,
written down, and is still present unfixed two audit generations later. Both
are evidence of a process gap (release cadence / remediation follow-through)
rather than a code-quality gap, and both recur — see synthesis.

---

## Findings register (REC-)

```
ID: REC-001
Category: Versioning / one-source-of-truth
Severity: MEDIUM
Confidence: HIGH (reproduced by reading exact lines and confirming the file is live)
Location: Files: src/llm_router/service.py Lines: 42, 329
Observation: FastAPI app title and /health response both hardcode version="5.3.0", bypassing llm_router.__version__.
Evidence: `grep -n version src/llm_router/service.py` → lines 42, 329; `__init__.py:25-46` shows the correct dynamic mechanism these two lines don't use; `git log -1 --format=%h\ %ad -- src/llm_router/service.py` → aff5e81, 2026-09-22 (file is actively maintained, not abandoned).
Why this exists, if discoverable: service.py predates or was copy-pasted before __version__'s dynamic-resolution pattern existed, and nothing re-checks it on release.
Why this matters: anything polling /health for version drift (deploy tooling, monitoring, a future self-audit script) gets a false answer that is 10 major versions stale.
User-visible impact: LOW for CLI users (they never hit this path); MEDIUM for anyone running the FastAPI service as infra.
Engineering impact: silent — nothing fails, it just lies.
Is behavior currently used? YES — service.py is launched by service_manager.py and covered by 2 test files.
Recommended action: SIMPLIFY — replace both literals with `from llm_router import __version__`.
Proposed target: src/llm_router/service.py:42,329 import __version__ once at module top.
Behavioral compatibility risk: NONE (the field is purely informational).
Security risk: NONE. Performance impact: NONE. Estimated complexity removed: ~2 lines fixed, 0 removed.
Validation required: confirm no test pins the literal "5.3.0" (a quick grep before changing).
Dependencies on other findings: none.

ID: REC-002
Category: Release process
Severity: MEDIUM
Confidence: HIGH
Location: Files: pyproject.toml, .claude-plugin/plugin.json, .claude-plugin/marketplace.json Lines: pyproject.toml:3
Observation: HEAD (3c96d23) is 23 commits past the v15.1.0 tag; the version string was not bumped despite 63 files and 6,502 inserted lines of real fixes landing since.
Evidence: `git describe --tags 3c96d23` → v15.1.0-23-g3c96d23; `git log --oneline v15.1.0..3c96d23` → 23 commits including Codex-adapter, enforcement-hold, and savings-verification fixes.
Why this exists, if discoverable: this is the second time (audit/FROZEN_STATE.md records the identical shape for the 2026-09-22 audit's subject, 15 commits past v14.1.0) — suggests audits/remediation sessions habitually run ahead of the tag rather than the tag following the work.
Why this matters: anyone comparing "what does v15.1.0 do" against this tree is comparing against a stale reference; a self-audit that reads pyproject.toml's version as ground truth (as this very audit briefly did before checking git describe) draws the wrong boundary.
User-visible impact: LOW-MEDIUM (installed package is still whatever was actually tagged/published; the gap is a documentation/traceability issue for maintainers and auditors, not a runtime bug).
Engineering impact: makes "what shipped in X" ambiguous.
Is behavior currently used? N/A (process finding).
Recommended action: KEEP the tagging discipline, but tighten it — tag closer to each meaningful merge, or make "unreleased commits since last tag" a `doctor`/CI-visible number.
Proposed target: a one-line check in CI or `llm-router doctor` reporting `git describe --tags` vs pyproject version.
Behavioral compatibility risk: NONE. Security risk: NONE. Performance impact: NONE.
Validation required: none beyond the git commands already run.
Dependencies on other findings: relates to REC-005's "audit's own index promises artifacts it never produced" — same class of "the tracking document didn't keep up with the work."

ID: REC-003
Category: Repository hygiene
Severity: LOW
Confidence: HIGH
Location: Files: audit/2026-09-21/** (11 files), audit/2026-09-22/** (12 files)
Observation: Two full prior-audit-report directories remain tracked at HEAD, and the current audit's own README explicitly disclaims their numbers as wrong.
Evidence: `git ls-files audit/2026-09-21 audit/2026-09-22 | wc -l` → 23; audit/README.md:84-86 ("Do not trust... the prior audits in 2026-09-21/ and 2026-09-22/. Those audits were wrong at least twice about their own numbers").
Why this exists, if discoverable: each audit generation was committed in full rather than archived/summarized after being superseded.
Why this matters: a reader (human or agent) who opens these without reading audit/README.md first inherits wrong numbers with no warning at the point of use.
User-visible impact: NONE (internal engineering artifact). Engineering impact: LOW-MEDIUM — 872KB and 53 files of "which version of this fact is current" friction for anyone grepping audit/.
Is behavior currently used? NO — these are historical documents, not executable.
Recommended action: MERGE — archive both generations into a single dated summary per generation (what was found, what was fixed, "superseded, kept for record") rather than deleting the full detail outright.
Proposed target: audit/history/2026-09-21-summary.md, audit/history/2026-09-22-summary.md; delete or keep the originals behind git history only, per the operator's call.
Behavioral compatibility risk: NONE. Security risk: NONE. Performance impact: NONE. Estimated complexity removed: ~800KB, 20+ files from the live audit/ index.
Validation required: confirm nothing (script, doc link, CI step) reads these paths programmatically — I found none in this pass but did not exhaustively grep every workflow.
Dependencies on other findings: none.

ID: REC-004
Category: Dead / broken code
Severity: HIGH
Confidence: HIGH (reproduced twice: ruff parse error AND python -m py_compile SyntaxError under the project's own pinned interpreter)
Location: Files: scripts/bench_session_replay.py, scripts/gen_cast.py, scripts/dev/gen_cast.py Lines: bench_session_replay.py:138; gen_cast.py:31 (both copies)
Observation: All three files use a Python-3.12-only f-string construct (backslash / quote-reuse inside the expression part) and fail to even compile under the project's declared minimum (>=3.11) and its own pinned dev venv (3.11.15).
Evidence: `HOME=$(mktemp -d) .venv/bin/python -m py_compile scripts/gen_cast.py` → SyntaxError: f-string expression part cannot include a backslash, line 31; identical failure for the other two files; `ruff check .` reports all 44 "invalid-syntax" hits are confined to exactly these 3 files.
Why this exists, if discoverable: likely authored/tested on a local machine running Python 3.12+, never run through the project's own CI (which scopes `ruff check` to src/ and tests/ only — scripts/ is unchecked, per .github/workflows/ci.yml:28) or through the pinned .venv before commit.
Why this matters: this is not a style nit, it is proof the file cannot execute at all in this project's supported environment — and CI's lint gate cannot see it because it only scans src/ and tests/.
User-visible impact: NONE directly (these are dev/demo scripts, not shipped in the package). Engineering impact: any developer or CI step that tries to run these three scripts as documented gets an immediate SyntaxError.
Is behavior currently used? UNCERTAIN — referenced from architecture/docs and scripts/README.md as demo/cast-generation tooling, but by definition cannot currently run, so "used" is moot until fixed.
Recommended action: SIMPLIFY (fix the 3 f-strings — trivial, assign the interpolated sub-expression to a local variable first) OR DELETE if the cast-generation workflow is abandoned.
Proposed target: scripts/gen_cast.py, scripts/dev/gen_cast.py (note: these are near-duplicates of each other per an earlier scripts reorg, `1c6c6eb`; consider deleting one), scripts/bench_session_replay.py.
Behavioral compatibility risk: NONE (scripts don't run today; fixing them can only add capability). Security risk: NONE. Performance impact: NONE. Estimated complexity removed: 0 (fix) or ~200 lines (delete the duplicate).
Validation required: after fixing, run each script once under .venv/bin/python to confirm it executes end to end (not just compiles).
Dependencies on other findings: this exact defect was already reported by audit/2026-09-22/DEAD_AND_SUSPICIOUS_CODE.md:107-109 and left unfixed across 52 subsequent commits including two release cuts — see synthesis note. This is the single strongest "known finding, never acted on" instance found in this domain.

ID: REC-005
Category: Documentation accuracy (audit corpus)
Severity: LOW
Confidence: HIGH
Location: Files: audit/README.md Lines: 45-48 (table rows for 16/17/19/20)
Observation: The prior audit's own artifact index lists 4 deliverables (16_CONFIGURATION_AUDIT.md, 17_PERFORMANCE_AUDIT.md, 19_FAILURE_MODE_MATRIX.md, 20_SIMPLICITY_AUDIT.md) that were never created.
Evidence: `git log --diff-filter=A -- audit/16_CONFIGURATION_AUDIT.md` (and the other 3 paths) all return empty; `git log --diff-filter=D` for the same paths also empty — never added, so not deleted either.
Why this exists, if discoverable: audit/README.md:57-60 itself says these were "wave 2, informed by what wave 1 finds" — wave 2 appears to have been only partially executed, or its output was folded into other files without updating this index.
Why this matters: a reader trusting this index (as I initially did) will look for files that don't exist and either waste time or wrongly conclude something was deleted.
User-visible impact: NONE. Engineering impact: LOW — one stale table in an internal doc.
Is behavior currently used? N/A.
Recommended action: SIMPLIFY — either produce the 4 missing files or edit audit/README.md's table to mark those phases "not run" / "folded into audit/XX".
Proposed target: audit/README.md:45-48.
Behavioral compatibility risk: NONE. Security risk: NONE. Performance impact: NONE.
Validation required: none beyond the git log checks already run.
Dependencies on other findings: same class as REC-002 (tracking document drifted from the work).

ID: REC-006
Category: Repository hygiene
Severity: LOW
Confidence: MEDIUM
Location: Files: intercept_bench.json (repo root)
Observation: A 276-line generated benchmark-output JSON sits at repo root, untouched since 2026-09-15, not referenced by any script, test, doc, or CI step as an expected artifact.
Evidence: `grep -rln intercept_bench --include='*.py' --include='*.md' --include='*.yml' .` → only scripts/bench_intercept.py (the generator) matches; no consumer found.
Why this exists, if discoverable: looks like a one-off benchmark run's output committed by accident or for a one-time PR discussion, then never cleaned up.
Why this matters: root-level clutter; a reader doing `ls` at repo root has to figure out this is generated output, not configuration.
User-visible impact: NONE. Engineering impact: NEGLIGIBLE (872 bytes... actually 276 lines but small).
Is behavior currently used? NO.
Recommended action: DELETE (regenerate via scripts/bench_intercept.py if the number is ever needed again) or MOVE under docs/BENCHMARKS.md's tracked-results convention if it's meant to be evidence for a claim.
Proposed target: remove from repo root.
Behavioral compatibility risk: NONE. Security risk: NONE. Performance impact: NONE. Estimated complexity removed: 1 file.
Validation required: confirm no README/CHANGELOG claim cites a number from this specific file before deleting (I did not find one, but did not exhaustively check every doc's benchmark numbers).
Dependencies on other findings: none.

ID: REC-007
Category: Dead code (UNCERTAIN — flagged, not concluded)
Severity: LOW (evidence-limited; could be MEDIUM if confirmed dead)
Confidence: LOW-MEDIUM
Location: Files: src/llm_router/feedback_handler.py, src/llm_router/gateway_service.py, src/llm_router/budget_lineage_reconciliation.py, src/llm_router/commands/admin_actions.py, src/llm_router/control_plane/store_postgres.py
Observation: All five are zero-inbound in the src/llm_router import graph (415-module ast-based scan, 0 parse errors) and each is referenced, outside its own file and its own test, only by prior audit documents (2026-09-21 and/or 2026-09-22) that already flagged it as suspicious.
Evidence: import-graph script (ast-based, see §5 methodology); whole-repo grep per name, results tabulated in §5's "genuinely unresolved" list.
Why this exists, if discoverable: each may be (a) genuinely dead since the audit that flagged it, (b) reached via dynamic dispatch/entry-point/string lookup this static scan can't see (the brief's own caveat), or (c) legitimately optional/enterprise code not wired into the default path.
Why this matters: if genuinely dead, this is 5 modules' worth of maintenance surface (imports, tests, docs) with zero user value; if not dead, it's evidence the import graph alone cannot be trusted for this call and a runtime/coverage-based check is needed.
User-visible impact: UNKNOWN pending resolution. Engineering impact: MEDIUM if confirmed dead across 5 files.
Is behavior currently used? UNCERTAIN — explicitly not resolved in this pass; per the brief's own rule this must NOT be silently promoted to DEAD.
Recommended action: KEEP pending investigation — hand to whoever owns §8/§10 with a request to check for dynamic loading (importlib, string-keyed dispatch in commands/ or control_plane's own router) before any deletion.
Proposed target: n/a until investigated.
Behavioral compatibility risk: UNKNOWN. Security risk: NONE identified. Performance impact: NONE identified.
Validation required: a runtime coverage run (pytest --cov) or a grep for string-based dynamic imports of these exact module names, which this pass did not have time to complete.
Dependencies on other findings: these are the same modules the 2026-09-21/2026-09-22 audits already raised — see synthesis note; if this audit reaches the same conclusion a third time and it is again not acted on, that is itself the more important finding.
```

---

## Top items for synthesis

1. **`scripts/gen_cast.py` + 2 siblings cannot execute at all under this
   project's own pinned Python** — found by the 2026-09-22 audit, still
   broken 52 commits and 2 release cuts later. The strongest available
   "documented finding, never fixed" example in the whole audit corpus so
   far — a 3-line-per-file fix that nobody applied because CI's lint scope
   (`src/ tests/` only) can't see `scripts/`. (`REC-004`)
2. **CI's lint gate silently excludes `scripts/` and `_quarantined_tests/`
   entirely** — not itself a bug, but it is *why* REC-004 survived. Worth a
   Top-10 "testing/CI problems" slot: a green `ruff check` in CI proves
   nothing about roughly a third of the repo's Python files by line count
   (36,777 of ~310,000 total .py lines).
3. **Version one-source-of-truth is 90% solved and it shows** —
   `env_registry.py` (207 vars, self-scoped, test-enforced) and
   `__init__.py`'s dynamic `__version__` are both genuinely good patterns,
   explicitly self-aware of their own boundaries (env_registry's docstring
   admits it doesn't cover `scripts/`). Recommend both for the **do-not-change
   register** — they are the target shape the rest of the codebase should be
   pulled toward, not candidates for simplification.
4. **`service.py`'s hardcoded `"5.3.0"`** two lines, live code, ten major
   versions stale, health-check-facing. Trivial fix, real (if minor) trust
   cost. (`REC-001`)
5. **HEAD running 23 commits ahead of the last tag is now a repeated
   pattern** (also true of the 2026-09-22 audit's subject, 15 commits ahead
   of its tag) — candidate for a Top-10 "process/correctness risk": audits
   and releases in this repo keep being cut from a moving, untagged HEAD,
   which makes "what does version X actually do" an open question twice
   in a row. (`REC-002`)
6. **The audit corpus itself has accumulated 53 tracked files / 872KB across
   3 generations**, with the newest explicitly disclaiming the older two's
   numbers. Candidate for the **deletion/consolidation ledger**: archive,
   don't delete outright — the record of "what was found and fixed" has
   value, the raw duplicate detail sitting in the main tree does not.
   (`REC-003`)
7. **5 modules flagged as suspicious by two independent prior audit
   generations are still zero-inbound today** (`feedback_handler.py`,
   `gateway_service.py`, `budget_lineage_reconciliation.py`,
   `commands/admin_actions.py`, `control_plane/store_postgres.py`) — same
   "known, never acted on" shape as REC-004, at smaller individual severity
   but larger aggregate footprint. Recommend the dead-code/duplication
   auditors resolve these definitively rather than re-flagging a third time.
   (`REC-007`)
8. **No type checker is configured anywhere in this repo** — worth noting
   for whoever compiles the overall baseline table; this is a fact (absence
   confirmed by grep), not an opinion about whether one is needed.
9. **`ruff format --check .` fails on 1,235 of 1,388 files** — format is
   effectively unenforced repo-wide (CI has no format-check step at all,
   only `ruff check src/ tests/`). Not scored as a REC finding on its own
   (style, not correctness — brief says "do not assign HIGH because code is
   ugly"), but material for whoever writes the baseline/CI-gate summary.
10. **Root-level generated artifact (`intercept_bench.json`) with no
    consumer** — smallest item here, included because "does each root file
    belong at root" was asked explicitly. (`REC-006`)
