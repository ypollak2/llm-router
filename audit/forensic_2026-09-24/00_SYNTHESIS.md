# llm-router Forensic Audit — Synthesis

Author: synthesis pass, 2026-09-24. Baseline: worktree `llm-router-forensic` @ `3c96d23`
(read-only; nothing in it was modified by this pass or by any input pass).

Inputs: `00_BRIEF.md`; domain reports `01_recon_metrics.md` … `12_docs_claims.md`;
verification reports `13_verify_security_deps.md`, `13_verify_routing_config.md`,
`13_verify_storage_accounting.md`, `13_verify_structure_docs.md`. Per instruction,
**verification overrides domain reports wherever they disagree**, and the coordinator's
eight corrections (listed in the task brief for this pass) override both. Every claim
below carries its evidence pointer in parentheses: `[DOMAIN-ID]` for a finding ID (see
the findings register), or a direct `file:line`. Numbers carry their `n`, window and
source wherever the source report gave one. Nothing here was independently re-measured
by the synthesis pass beyond arithmetic on numbers the eleven prior passes already
produced; this document reconciles, does not re-audit.

---

## 1. Executive summary

**Is it healthy?** Mostly yes, in the sense that matters most: the parts of this
codebase that would be expensive to get wrong — the LLM provider adapter, the
secret scrubber, the WAL/SQLite-concurrency fix, the environment-variable registry, the
dynamic version resolver, the OKF context-injection choke point, and the fail-open
accounting design — are **genuinely well engineered, already self-audited, and already
tested against the specific incidents that motivated them** `[PRV-02, do-not-change;
07_providers_perf.md]` `[10_security_privacy.md Overview]` `[01_recon_metrics.md §36]`.
This is not a codebase in crisis, and an exceptional team inheriting it today would keep
most of its safety-critical machinery unchanged.

What is *not* healthy is trust in what the product tells the user about itself. Two
findings are CRITICAL and both survive adversarial re-verification with live,
in-process reproduction, not just static reading:

- **A real API key can be exfiltrated to an attacker-chosen host** by opening a project
  directory whose `.env` sets two unvalidated config fields — reproduced end-to-end over
  a real local socket, canary key captured in the `Authorization` header
  `[SEC-002, SEC-003 — CRITICAL, CONFIRMED live]`.
- **`run_command` has zero argument-level containment** via allowlisted general-purpose
  interpreters (`python3 -c`, `node -e`) — this one is *already disclosed and accepted*
  by the maintainers in `SECURITY.md` with a named regression test, so it is CRITICAL by
  capability but not a hidden defect `[SEC-001 — CRITICAL, disclosed/accepted]`.

Below CRITICAL, the pattern repeats: the product's own headline "money saved" number is
built almost entirely (98%) from two tables with **zero provenance filtering**, and is
currently **~3,534× the codebase's own honest, provenance-filtered accessor for the same
data** (`≈$372.66` shown vs `≈$0.11` canonical, live, same instant, same commit)
`[ACC-01 — HIGH]`. Separately and more fundamentally: **this commit's own real
verified-savings figure is $0.00**, and **0 of roughly 1,160 locally-drafted answers on
the maintainer's own machine were ever accepted as a replacement for a Claude turn**
(coordinator context, corroborated independently by `[OBS-03]`'s `draft_acceptance()`
measurement: 0 of 44 offered on the last day it was run). A user reading `llm-router
status` today sees a number built almost entirely from unverified rows, while the
system's own honest instrumentation says the real, verified number is zero.

**Where the complexity comes from.** Not organic per-file bloat. Two specific patterns
account for most of it:

1. **Parallel implementations that were each reasonable in isolation and never
   reconciled.** Four separate pipelines can answer "route this prompt"
   (`router.py`/`gateway.py`/`sdk.py`/`direct_executor.py`), with materially different
   safety guarantees — only one of the four applies budget, quota, gates, and redaction
   `[RTE-004 — HIGH]`. Two classification tables (`hooks/auto-route.py`'s `SIGNALS` and
   `classify.py`'s `_SIGNALS`) both claim to be canonical and have provably diverged,
   including a whole missing task category `[RTE-002 — HIGH]`. Nine files implement
   three unrelated meanings of "budget." Sixteen independent JSONL/log stores exist with
   no single event model `[OBS-01 — HIGH]`. Storage abstractions (`storage/`,
   `hosts.HostAdapter`) were built, tested, and then never adopted by the code they were
   built for — 63 of ~66 SQLite-touching files bypass `storage/` entirely, and
   `commands/install.py` reimplements `hosts/`'s job inline `[STR-003, STR-001 — both
   HIGH]`.
2. **A findings-compound-not-code-compounds problem.** This repository runs an
   unusually rigorous internal self-audit practice (three prior audit generations, 53
   files, ratchet tests named after the incident they close) — and yet the *same*
   defects recur across generations: three scripts that cannot even be parsed by the
   project's own pinned Python were found by the 2026-09-22 audit and are **still
   broken, unfixed, across 52 subsequent commits including two release cuts**
   `[REC-004 — HIGH]`; five zero-inbound modules were flagged by two separate prior
   audit rounds and are still unresolved today `[REC-007]`. The tooling to find problems
   is excellent; the loop that closes them is not.

**What to do first.** In order of leverage-to-risk:

1. Guardrail tests before anything else moves (Phase 0) — pin the exact behaviors this
   audit's own adversarial reproductions depend on, so a later refactor can't silently
   reopen SEC-002/SEC-006/ACC-01/TST-01.
2. Pure deletions (Phase 1) — roughly 7,000+ LOC across `scripts/`'s duplicate cluster,
   `hosts/`, `frameworks/`, `cache/store.py`'s dead stub, `context_signal.py`, and the
   three git-tracked-despite-`.gitignore` stale hook files. Zero behavioral risk, proven
   by exhaustive grep in every case.
3. The two CRITICAL security fixes (validate `openai_compat_base_url`/`llm_router_pxpipe_url`;
   stop forwarding the real provider key to a config-redirected `api_base` by default)
   and the accounting filter fix (`dashboard_data.query_window` needs the same
   provenance filter `savings_stats` already got) — all small, isolated, well-scoped
   changes with existing precedent in the same files.
4. A handful of one-line-to-one-function correctness bugs already reproduced live:
   `last`/`retrospect` always exit 0 `[CFG-010]`; `pyyaml`/`httpx` undeclared as core
   dependencies `[DEAD-01, DEAD-02]`; the namespace-package test-isolation false
   positive `[TST-01, fix verified 17/17 pass]`.

**What not to change.** The litellm-plus-`ProviderQuirk` provider layer; the WAL/
busy-timeout fix in `sqlite_wal.py`; `secret_scrubber.py` as the single canonical
scrubber; `env_registry.py`'s "check the check" design; the dynamic `__version__`
resolver; the `context_injection` choke point and its enforcing test; `failopen.py`'s
design (not its rollout breadth); the `_quarantined_tests/` triage process; and —
importantly — the `run_command` capability trade-off itself, which is a disclosed,
tested, deliberate design decision, not an oversight to "fix" into a false sense of
security without a real architecture change.

**Realistic simplification.** Not a rewrite. The single highest-leverage structural
change — collapsing four routing/execution pipelines into one execution core with
budget/quota/gates/redaction applied uniformly — is real, large, and belongs in Phase 3+
after the classification-engine merge is proven equivalent by test, the same way this
repo already proved `HOOK_LIVE_POLICY` equivalent to the hook at 1000/1000 agreement
before trusting it `[02_runtime_routing.md §13 item 2]`. Everything else in this report
is smaller: pure deletions, doc fixes, a handful of validators, and ratchet-lint
migrations the repo has already precedented for a structurally identical problem
(`scripts/lint_unknown_as_number.py`, baseline-then-count-down) `[04_structure_duplication.md §11]`.

---

## 2. Verification outcome table — every CRITICAL/HIGH finding, original → verified

Per instruction, `13_verify_*.md` overrides the domain report wherever they disagree,
and the coordinator's corrections override both (marked **[COORD]** below).

| ID | Original severity | Verified severity | What changed |
|---|---|---|---|
| SEC-001 | CRITICAL | **CRITICAL** (unchanged) | Confirmed live (`cat` outside project via `run_command` returns secret content); disclosed/accepted, not hidden |
| SEC-002 | CRITICAL | **CRITICAL** (unchanged) | Confirmed with live network capture: canary API key sent as `Authorization: Bearer` to attacker listener |
| DEAD-01 | CRITICAL | **MEDIUM [COORD]** | 13_verify confirmed a real crash chain (via `litellm`'s own unguarded `import yaml`) and left CRITICAL standing, but the coordinator overrides: this is an undeclared-direct-dependency-satisfied-transitively defect, same class as DEAD-02, not a guaranteed crash for every install path |
| RTE-003 | CRITICAL | **LOW [COORD, matches 13_verify]** | A measured (n=1,571), tested (`test_s8_...py`), `doctor`-surfaced statistical property, not an active correctness defect; worst case routes a Q&A prompt to `query` vs `analyze`, both non-destructive |
| RTE-002 | HIGH | **HIGH** (unchanged) | Confirmed: diffed dict literals differ in 3 categories + a whole 7th category (`coordination`) missing from `classify.py` despite `TaskType.COORDINATE` existing |
| RTE-004 | HIGH | **HIGH** (unchanged) | Confirmed, arguably worse: real paid `ModelSpec`s (Gemini/OpenAI) are reachable from the same ungoverned direct-execution path, so real spend bypasses budget/quota, not just privacy |
| RTE-001 | HIGH (domain 02's own initial draft) | **LOW** (revised within domain report, confirmed by 13_verify) | Live check: this file is not registered in any `settings.json`; the hook Claude Code actually runs is a different, current file |
| CFG-001 | HIGH | **MEDIUM** | Split: `status-bar.py`'s wrong default is real and live; `stop-enforce.py`'s half is not installed as a Stop hook in a standard install, so it is dead-code hygiene, not an active gap |
| CFG-002 | MEDIUM-HIGH | **LOW** | Same file class as RTE-001 — the "used by every contributor session" premise is false; the drifted defaults are real but the file never executes |
| CFG-004 | MEDIUM-HIGH | **MEDIUM-HIGH** (unchanged) | Confirmed by an empirical test (not just static reading): real env var beats `.env`, the opposite of the docstring |
| CFG-009 | MEDIUM-HIGH | **MEDIUM-HIGH** (unchanged) | Confirmed by direct execution: `doctor --help` runs the real health check and exits 1 |
| CFG-010 | HIGH | **HIGH** (unchanged) | Confirmed exact reproduction: `llm-router last` prints an error and exits 0 |
| STR-001 | HIGH | **HIGH** (unchanged) | Confirmed: zero `src/` references to `llm_router.hosts` outside the package itself |
| STR-003 | HIGH | **HIGH** (unchanged) | Confirmed count exact: 63 files bypass `storage/` |
| TST-01 | HIGH | **MEDIUM-HIGH** | Fix independently applied and verified (17/17 pass, no regressions); real production/user impact is zero (CI runs full suite), so triage-cost severity, not correctness severity |
| ST-01 | HIGH | **HIGH** (unchanged) | Confirmed: `routing.db` is 0 rows in 12/13 tables live; 2 docs still tell users to delete it as a fix |
| ST-02 | HIGH | **HIGH**, stronger evidence | Call-graph proof (not just row-count) that `LineageStore.append()` has zero production callers, independent of session-boundary wipe behavior |
| ST-04 | MEDIUM-HIGH | **MEDIUM-HIGH** (unchanged) | Count corrected: 45 `ALTER TABLE` statements in `cost.py`, not "25+"; mechanism corrected (`OperationalError` catch, not `PRAGMA table_info`) |
| ST-06 | HIGH | **HIGH** (unchanged) | Confirmed exactly: `StorageService` has 2 callers repo-wide; `budgets.db` is 0 rows in both tables |
| CTX-04 | HIGH | **HIGH** (unchanged, arguably understated) | Near-exact recount (12,709 vs 12,694 dirs — drift consistent with ongoing test writes); true "genuine data" fraction is lower than first reported |
| ACC-01 | HIGH | **HIGH** (unchanged, numbers corrected) **[COORD]** | Real gap is `$372.66` (+ `$110.91` shown separately as unverified) vs `≈$0.11` canonical ≈**3,534×**, not the domain report's hand-computed `$495.04`/4,500×; usage-table contribution is `$5.76`, not `$17.23`; the unverified split **is** visibly shown to the user (contradicting the domain report's "no indication" claim) |
| PERF-01 | HIGH | **MEDIUM [COORD, matches 13_verify]** | `cost._get_db()` uses its own inline 5,000ms `busy_timeout`, not `sqlite_wal.enable_wal`'s 30,000ms; `session_spend.py`'s 2,000ms is 2.5× shorter, not 15×; the swallow-and-write-loss risk is real but smaller than claimed |
| ERR-01 | HIGH | **HIGH** (unchanged, worse) **[COORD]** | Alias-aware AST scan finds true coverage ≈**8.6%** of 1,020 broad excepts (88/1,020), not the domain report's ~16% ceiling — the "162 call sites" and "59 codes" figures do not reproduce and should not be republished |
| OBS-01 | HIGH | **HIGH** (unchanged) | Confirmed exactly: 6 files each redefine `_savings_log_path`; both cited "AC-5" comments confirmed |
| REC-004 | HIGH | **HIGH** (unchanged) | Confirmed byte-for-byte: 3 scripts still fail `py_compile` under the project's own pinned Python 3.11.15 |
| HOST-01 | HIGH | **HIGH** (unchanged, one nuance) | Confirmed live: `llm-router install --host pi` returns "Unknown host(s): pi"; a separate, effectively unreachable legacy `cli.py` path has partial Pi support, so "zero code anywhere" was slightly overstated |
| HOST-03 | HIGH | **HIGH** (unchanged, worse) | The doc is self-contradictory in **4** ways, not the 2 either domain report cited independently |
| DOC-03 | HIGH | **HIGH** (impact confirmed; fix text needs correction) **[COORD]** | The prompt-routing hook IS installed and default-on for Codex; PreToolUse enforcement is NOT ready (`routing_ready("codex")` is `False`); the domain report's proposed fix wording ("Full auto-routing via hooks") would itself overclaim and must not ship as-is |

No other CRITICAL/HIGH finding was contradicted by a `13_verify_*.md` report; all others
listed in the domain reports (SEC-003/004/006, CFG-004/009, ST-01/02/04/06, CTX-04,
REC-004, HOST-01/03, DOC-03, STR-001/003, TST-01, RTE-002/004, ACC-01, ERR-01, OBS-01)
were independently reproduced and are carried into §5 at their verified severity.

---

## 3. Product core map

Basis: inbound-import centrality, presence in `[project.scripts]`/MCP registration/hook
registration (reachable at runtime), and whether a module is exercised only by its own
tests `[01_recon_metrics.md §7]`.

| Tier | Contents | Evidence |
|---|---|---|
| **CORE** | `router.py`, `cli.py`, `server.py`, `config.py`, `types.py`, `cost.py`, `logging.py`, `savings.py`, `classify.py`, `policy.py`, `tool_surface.py`, `paths.py`, `secret_scrubber.py`, `hooks/auto-route.py`, `hooks/enforce-route.py`, `hooks/session-start.py`, `hooks/session-end.py`, `install_hooks.py`, `env_registry.py` | Top of inbound-import ranking + registered hook event + console-script entry point |
| **SUPPORTING** | `commands/*` (44), `tools/*` (17), `hosts/*` (but see STR-001 — never actually called), `dashboard/*`, `ui/*`, `semantic/*`, `signals/*` | Reachable from CORE via CLI/MCP, individually replaceable |
| **OPTIONAL** | `frameworks/*` (extras-gated, mostly stubs), `control_plane/*` (opt-in multi-tenant), `benchmark/*`, `agents/*` | Near-zero inbound from CORE/SUPPORTING |
| **EXPERIMENTAL** | `submissions/routerarena/*`, `_quarantined_tests`' subjects | Explicitly marked as such |
| **LEGACY** | `service.py` (hardcoded `version="5.3.0"`, 10 major versions stale — `REC-001`); `LineageStore` (`ST-02`); `savings_stats.mode` column (`ACC-04`); `.claude/hooks/*` stale copy (`RTE-001`/`CFG-002`) | Actively touched but not the live path, or dead-but-tracked |
| **DEV-ONLY** | `scripts/*` (193 files, of which 25 are stale root-level duplicates — `DEAD-03`) | Not shipped in the wheel |
| **DOC-ONLY** | `docs/`, `guide/`, `architecture/` (16 files, unlinked from README — `DOC-10`), `audit/` (53 files, 3 generations) | No runtime effect |
| **UNCERTAIN (never resolved to DEAD or KEEP across 3 audit generations)** | `feedback_handler.py`, `gateway_service.py`, `budget_lineage_reconciliation.py`, `commands/admin_actions.py`, `control_plane/store_postgres.py` | `REC-007` — flagged 2026-09-21, 2026-09-22, and again here; still zero-inbound |

**Smallest credible product preserving user value** (per the brief's §7 ask): the CORE
row above, plus exactly the SUPPORTING modules a real host install touches
(`commands/install.py`, `hoots/*` MCP tools actually registered under the `consolidated`
tier — 12 of 70 — the security guard modules `agent_loop.py`/`agent_writes.py`/
`safe_subprocess.py`, and the accounting path once `ACC-01` is fixed). Everything in
OPTIONAL, EXPERIMENTAL, and the UNCERTAIN row can be removed, gated behind an explicit
extra, or left exactly as undecided as it is today without reducing what a Claude
Code/Codex/Gemini CLI user actually experiences.

---

## 4. Architecture and runtime-flow summary

Full traced flows (A–J) are in `02_runtime_routing.md §6`; this is the load-bearing
summary.

**There are at minimum four independent pipelines that can answer "route this prompt,"
not one pipeline with host-specific adapters** `[02_runtime_routing.md §13, §47q8]`:

| Engine | Classification source | Execution path | Budget/quota/gates? | Redaction? |
|---|---|---|---|---|
| Hook direct-exec (default ON, highest volume) | `auto-route.py`'s private `SIGNALS` | `direct_executor.execute_chain/execute_agent` | **No** | **No** |
| Hook → MCP tool fallthrough | hook's verdict + `classify.py`/`ROUTER_POLICY` | `router.route_and_call` | Yes | Opt-in |
| `gateway.py` HTTP endpoints | `classify.py` `GATEWAY_POLICY` | `router.route_and_call` or `route_payload` | Yes | Opt-in |
| `sdk.py route()` | `gateway._classify` → `classify.py` `GATEWAY_POLICY` | `chain_builder` + `direct_executor` (same as hook) | **No** | **No** |

The direct-execution paths (used by the highest-volume real traffic, per the hook's own
"80/20 rule" comment) skip `router.py`'s safety machinery entirely — an operator setting
`LLM_ROUTER_REDACTION=on`, believing it protects every call, only gets that protection
on the minority of traffic that happens to route through `router.route_and_call`
`[RTE-004]`.

**One genuinely legitimate host-specific exception exists and should stay**:
`hosts/base.py:routing_env()` propagates tuned `LLM_ROUTER_*` env vars into non-Claude-
Code host MCP configs (secrets excluded), fixing a real, previously-shipped bug where
Cursor/OpenCode/Windsurf/Codex silently fell back to an uninstalled default model
`[RTE-007, KEEP]`. This is the one place §47q8 asks about where host-specific behavior
is warranted — the router/hook execution-path split is not.

**Accounting flow** (`Flow J`) writes through at least 7 independent mechanisms per
turn (`log_routing_decision`, `_log_quota_snapshot_sync`, `session_store.record_event`,
`draft_usage.record_draft/audit`, `savings_logger.log_direct_savings/log_direct_to_db`,
`execution_ledger.record_event`, `attempt_log.record`) feeding into at least 4 money
tables (`usage`, `claude_usage`, `savings_stats`, `codex_usage`/`gemini_usage`) that
`dashboard_data.query_window()` sums with a provenance filter applied to only one of
them `[ACC-01]`. Live on this machine (2026-07-08→2026-09-24): only 217 of 1,640
injected directives (13.2%) correspond to a completed attempt row, and 105/322 (32.6%)
of attempts recorded were failures or grounding-rejections `[02_runtime_routing.md Flow J,
this-machine/this-workload, not a general claim]`.

**Direct-execution security boundary**, traced end-to-end `[10_security_privacy.md §20]`:
`_resolve_path` (used by `read_file`/`write_file`/`edit_file`/`list_files`/
`search_files`) is a real, correctly-implemented containment boundary — adversarially
tested against 10 path-traversal/symlink-escape variants, all blocked. `run_command`
never calls it — an allowlisted general-purpose interpreter escapes the project
boundary entirely, and `LLM_ROUTER_AGENT_WRITES=off` does not close that gap
(`SEC-001`, `SEC-006`). This asymmetry — real containment for 5 tools, none for the
6th — is the single most consequential architectural fact in the direct-execution
surface.

For full call-graph detail (imports, decision objects, storage writes per flow), see
`02_runtime_routing.md` (routing), `06_mcp_hosts.md` (MCP/host registration), and
`10_security_privacy.md §20` (direct-execution guarantee table).

---

## 5. Consolidated findings register — index

Severity is post-verification (§2) and post-coordinator-correction where applicable.
Full evidence for each ID is in its source domain report (and, where marked, the
matching `13_verify_*.md`). No claim here was invented; every ID below traces to a
`file:line` or a live reproduction in its source report.

| ID | Severity | One-line | Source |
|---|---|---|---|
| REC-001 | MEDIUM | `service.py` hardcodes `version="5.3.0"`, 10 major versions stale, live health endpoint | 01 |
| REC-002 | MEDIUM | HEAD is 23 commits past the last tag; this is the second audit in a row this has happened | 01 |
| REC-003 | LOW | 2 prior audit generations (23 files/872KB) still tracked at HEAD, self-disclaimed | 01 |
| REC-004 | HIGH | 3 scripts fail `py_compile` on the project's own Python; found by a prior audit, still unfixed | 01/13 |
| REC-005 | LOW | `audit/README.md` indexes 4 files that were never created | 01 |
| REC-006 | LOW | `intercept_bench.json` orphaned generated artifact at repo root | 01 |
| REC-007 | LOW/UNCERTAIN | 5 modules zero-inbound, flagged by 2 prior audits, still unresolved a 3rd time | 01 |
| RTE-001 | LOW | Stale git-tracked `.claude/hooks/auto-route.py` never executes (same file class as CFG-002) | 02/13 |
| RTE-002 | HIGH | `SIGNALS` table diverged hook vs `classify.py`; "verbatim" claim false, incl. a missing category | 02/13 |
| RTE-003 | LOW | Low-signal default decides 49.8% of routing decisions; measured, tested, `doctor`-surfaced | 02/13 |
| RTE-004 | HIGH | Direct-execution path bypasses budget/quota/gates/redaction; real paid spend reachable | 02/13 |
| RTE-005 | MEDIUM | Failed local attempts still cost wall-clock before Claude fallback (32.6% live, this-machine) | 02 |
| RTE-006 | MEDIUM | Two `PreToolUse` hooks coordinate via one file's undocumented allowlist entry | 02 |
| RTE-007 | LOW (fixed, KEEP) | Host env propagation fixed a real silent-degradation bug; don't regress it | 02 |
| DEAD-01 | MEDIUM | `pyyaml` undeclared core dependency, satisfied transitively today | 03/13 |
| DEAD-02 | MEDIUM | `httpx` undeclared, masked by `litellm`'s transitive pull | 03/13 |
| DEAD-03 | MEDIUM | ~5,100 LOC duplicate root-level `scripts/` cluster, reintroduced by a sync commit | 03 |
| DEAD-04 | LOW (retain) | `control_plane/api.py`+`reconciliation.py` dead, excluded from wheel; product decision pending | 03 |
| DEAD-05 | LOW | `cost.py` 8-function dead-reporter cluster, ratcheted; needs deprecation not silent deletion | 03 |
| DEAD-06 | LOW | `pre-release-checklist.py` points at a file that does not exist | 03 |
| STR-001 | HIGH | `hosts.HostAdapter` Protocol has zero production callers; `install.py` reimplements it inline | 04/13 |
| STR-002 | MEDIUM | `context_signal.py` self-admittedly dead, ratchet-tracked | 04 |
| STR-003 | HIGH | `storage/` abstraction ~2% adopted; 63 files bypass it with raw sqlite3/aiosqlite | 04/13 |
| STR-004 | MEDIUM | `frameworks/` package: 9 files, 1 shim, 6 stubs, zero production callers | 04 |
| STR-005 | LOW | `policies/`/`rules/` directories collide in name with unrelated code | 04 |
| STR-006 | MEDIUM | Two unrelated dataclasses both named `RoutingDecision` | 04 |
| STR-007 | MEDIUM/UNCERTAIN | `decisions/`+`signals/` mini decision-engine not imported by `router.py` | 04 |
| CFG-001 | MEDIUM | `LLM_ROUTER_ENFORCE`: `status-bar.py` bypasses the resolver (live); `stop-enforce.py` half is dead code | 05/13 |
| CFG-002 | LOW | Same stale `.claude/hooks/auto-route.py` as RTE-001, never runs | 05/13 |
| CFG-003 | MEDIUM | `LLM_ROUTER_PROFILE` collision across 2 axes, mitigated but live in `.env.example` | 05 |
| CFG-004 | MEDIUM-HIGH | `safe_config.py`'s documented precedence is backwards vs. actual pydantic-settings order | 05/13 |
| CFG-005 | LOW-MEDIUM | Timeout config cached process-lifetime; undocumented for long-lived servers | 05 |
| CFG-006 | LOW | `.env.example` uses a legacy var name + an implicitly-bound var | 05 |
| CFG-007 | MEDIUM | `env_registry`'s own AST scanner has a blind spot: 12+ vars invisible to its "complete" claim | 05 |
| CFG-008 | MEDIUM | Only 1/44 CLI commands support `--json` | 05 |
| CFG-009 | MEDIUM-HIGH | ~45% of CLI commands ignore `--help` and run real (sometimes side-effecting) behavior | 05/13 |
| CFG-010 | HIGH | `last`/`retrospect` always exit 0, even on their own documented failure path | 05/13 |
| CFG-011 | MEDIUM | CLI arg parsing split 16 argparse/28 hand-rolled, no shared convention | 05 |
| CFG-012 | LOW | `_KNOWN_SUBCOMMANDS` is a second, self-aware, low-risk typo-suggestion list | 05 |
| CFG-013 | MEDIUM | Boolean/mode explosion: ≥128 reachable states across routing-safety knobs | 05 |
| CFG-014 | LOW-MEDIUM | `LLM_ROUTER_HISTORY_RELAY` privacy gate, default-on, zero test/doc coverage | 05 |
| HOST-01 | HIGH | "Pi (pi.dev)" documented host has no working `--host pi` install path | 06/13 |
| HOST-02 | MEDIUM | 4 disagreeing host-support registries, none canonical | 06 |
| HOST-03 | HIGH | `HOST_SUPPORT_MATRIX.md` self-contradicts on Codex 4 ways, not 2 | 06/13 |
| HOST-04 | MEDIUM | Gemini CLI has no `PRE_TOOL` event; doc collapses it into the same "Full" cell as Claude Code | 06/13 |
| HOST-05 | LOW-MEDIUM | 9 near-duplicated per-host installer functions, no table-driven design | 06 |
| MCP-01 | MEDIUM | Tool count claimed "60" in 3 docs, "41" in a 4th; actual 70 | 06/13 |
| MCP-02 | LOW | `tool_tiers.py` docstring's stated default contradicts the actual default | 06 |
| MCP-03 | LOW | `llm_video` has no consolidated door and no fallback-chain entry | 06 |
| MCP-04 | LOW | 3-way duplication: `llm_savings`/`llm_savings_dashboard`/`llm_dashboard` | 06 |
| MCP-05 | LOW/UNCERTAIN | `implemented_tools()` AST scan is off by 5 vs. the measured 70 | 06 |
| PERF-01 | MEDIUM | `session_spend.py` write to shared `usage.db`: 2.5× shorter timeout, swallowed exception | 07/13 |
| PERF-02 | MEDIUM | `context.py` blocks the event loop with `sqlite3.connect` despite an awareness comment 3 lines up | 07 |
| PERF-03 | MEDIUM | 5 more async functions block the event loop (sqlite3/urlopen) | 07 |
| PERF-04 | LOW/informational | Hook adds ~120–170ms per prompt (floor measurement, n=3, single machine) | 07 |
| PRV-01 | MEDIUM | `cache/store.py`'s `SemanticCache` is a dead no-op stub beside the real `semantic_cache.py` | 07/13 |
| PRV-02 | KEEP | Provider adapter + quirks registry + WAL fix are genuinely good — do not touch | 07 |
| PRV-03 | LOW | Three unrelated concepts all called "tier" | 07 |
| PRV-04 | LOW | Stale fallback model ids in `_BUNDLED_DEFAULTS`, guarded activation condition | 07 |
| CTX-01 | LOW (mitigated, KEEP) | Cross-project contamination root cause fixed via `context_injection` choke point | 08 |
| CTX-02 | MEDIUM | `context_injection.py`'s own docstring contradicts its now-fixed state | 08 |
| CTX-03 | informational | ~70 tokens floor injected per prompt (clean HOME); real-world number not measured | 08 |
| CTX-04 | HIGH | Knowledge store is 99.5% test-fixture debris, 300MB/12,709 dirs | 08/13 |
| ST-01 | HIGH | `routing.db` orphaned; 2 docs still tell users to delete it as a fix | 08/13 |
| ST-02 | HIGH | `LineageStore` dual-write system: ~700 LOC, proven zero production callers | 08/13 |
| ST-03 | MEDIUM-HIGH | `model_tracking.jsonl` mislabeled "legacy" — it's actually the only live store | 08 |
| ST-04 | MEDIUM-HIGH | Two uncoordinated schema-migration systems; formal `migrations/` covers 1 table, `cost.py` has 45 ad hoc `ALTER`s | 08/13 |
| ST-05 | MEDIUM-HIGH | 6 independent "session" persistence mechanisms, 2 empty in production, UNCERTAIN not dead | 08 |
| ST-06 | HIGH | `StorageService` false single-entry-point abstraction; 2 files use it; "budget" duplicated 6 ways | 08/13 |
| ST-07 | MEDIUM | Tamper-evident `audit_events` table has 0 rows in a many-months-old install | 08 |
| ST-08 | HIGH | 800+ uncollected per-session shard files; `gc.py` exists but isn't wired to run automatically | 08 |
| ACC-01 | HIGH | Headline "saved" figure sums 2 unfiltered tables into a now-honest one; real gap ≈3,534× | 09/13 |
| ACC-02 | MEDIUM | 96 `claude_usage` rows share a sentinel value pair, $288 of a $366.90 subtotal, unmeasured provenance | 09/13 |
| ACC-03 | MEDIUM | The remediation checkpoint mis-names the dominant contributor (`claude_usage`, not `usage`) | 09/13 |
| ACC-04 | LOW | `savings_stats.mode` column fully documented, fully dead | 09 |
| ACC-05 | LOW | Two independently-maintained "free provider" set literals, one includes `openai_compat` | 09 |
| ACC-06 | MEDIUM | `mcp_session_dashboard` marked `canonical=True` but bypasses `canonical_savings()`/subscription gate | 09/13 |
| ACC-07 | LOW | Subscription detection defaults to the less-conservative (pay-per-token) direction | 09 |
| ACC-08 | LOW | Live numeric surfaces lack the caveat discipline the README itself demonstrates | 09 |
| SEC-001 | CRITICAL (disclosed) | `run_command` has zero argument containment via allowlisted interpreters | 10/13 |
| SEC-002 | CRITICAL | Credential exfiltration via `openai_compat_base_url`/pxpipe + unvalidated `api_base` | 10/13 |
| SEC-003 | HIGH | Project-local `.env` granted equal trust to user-level config; root-cause enabler of SEC-002 | 10/13 |
| SEC-004 | MEDIUM-HIGH | Ollama URL SSRF-adjacent "by design"; contradicts `SECURITY.md`'s "stays on your machine" claim | 10/13 |
| SEC-005 | MEDIUM | `SECURITY.md`'s own risk table is stale on 6/9 rows (understates current safety) | 10 |
| SEC-006 | HIGH | `AGENT_WRITES=off` does not stop `run_command` writes, including outside the project | 10/13 |
| SEC-007 | LOW | AWS-secret scrubber regex partial-redaction edge case | 10 |
| SEC-008 | LOW/UNCERTAIN | `auto-route-debug.log` has no scrub-on-write invariant; sampled clean, not exhaustive | 10 |
| SEC-009 | LOW/informational | `classification_allowlist` fails open by design (documented, intentional) | 10 |
| SEC-010 | LOW/UNCERTAIN | Alert-webhook destination unvalidated (payload scrubbed, caps damage) | 10 |
| SEC-011 | LOW-MEDIUM | `agent_writes` journal pre-images unhardened, unlike sibling `trace.py` | 10 |
| TST-01 | MEDIUM-HIGH | False-positive test-isolation error on 6 namespace packages; fix verified 17/17 pass | 11/13 |
| ERR-01 | HIGH | `failopen.py` covers ≈8.6% of 1,020 broad excepts, not the ~16% first claimed | 11/13 |
| ERR-02 | MEDIUM | Fail-open registry truncated to top-8/top-4 by volume; a rare severe code can be invisible | 11 |
| OBS-01 | HIGH | `savings_log.jsonl` written by 6 independently-implemented path helpers, unresolved dual-writer race | 11/13 |
| OBS-02 | MEDIUM | `auto-route-debug.log` has no rotation/cap, unlike every sibling JSONL store | 11 |
| OBS-03 | informational | `draft_acceptance()` = 0/44 accepted — the system's own honest self-measurement | 11 |
| TST-05 | LOW | 2 test files contain implementation-coupled (call-shape) assertions | 11 |
| DOC-01 | MEDIUM | Tool count "60" stale in 3 docs (actual 70) | 12 |
| DOC-02 | MEDIUM | Default tier documented as 11 tools, actually 12 (omits `llm_local_task`, the write/exec tool) | 12 |
| DOC-03 | HIGH | Codex auto-routing mislabeled "not yet" though shipped and default-on | 12/13 |
| DOC-04 | LOW | "Run tests (1900+)" undercounts the real suite (9,640) by >5× | 12 |
| DOC-05 | LOW | `tool_tiers.py`'s stale default-fallback comment is a latent footgun | 12 |
| DOC-06 | LOW | 4 of 23 implemented providers completely undocumented | 12 |
| DOC-07 | MEDIUM | Per-host savings percentages uncaveated, unlike the README's own nearby anecdote framing | 12 |
| DOC-08 | LOW | `server.py`'s docstring is the root-cause source of the stale "60" figure | 12 |
| DOC-09 | LOW | `.gitignore`'s `docs/` allow-list references a now-nonexistent path | 12 |
| DOC-10 | LOW | `architecture/` (16 files) entirely unlinked from README, overlaps `guide/ARCHITECTURE.md` | 12 |

---

## 6. Top-10 lists (§59–65)

### §59 — Top 10 complexity sources
1. Four independent routing/execution pipelines with different safety guarantees (`RTE-004`)
2. `storage/` abstraction ~2% adopted; 63 files hand-roll sqlite3/aiosqlite (`STR-003`)
3. `hosts.HostAdapter` Protocol built, tested, never called; `install.py` reimplements it inline (`STR-001`)
4. "Budget" spans 9 files and 3 unrelated meanings (cost-cap lineage, per-provider $ limits, per-request token allocation)
5. `cost.py` is 4,296 lines mixing schema DDL, 45 ad hoc migrations, 20+ accessors, provenance logic, and team-identity resolution (`ST-04`)
6. `hooks/auto-route.py` is 4,870 lines with its own private classifier table and its own model-chain table
7. Two uncoordinated schema-migration systems (`ST-04`)
8. 16+ independent telemetry/JSONL stores, no single event model (`OBS-01`)
9. Boolean/mode explosion: ≥128 nominally reachable states across `ENFORCE`/`ZERO_CLAUDE`/`DELEGATE`/`DIRECT_EXECUTION`/etc. with no enumerated valid/invalid matrix (`CFG-013`)
10. `frameworks/` package: aspirational scaffolding, 9 files, 1 real shim, 6 stubs (`STR-004`)

### §60 — Top 10 deletions (complexity removed / risk)
1. 24 root-level `scripts/` duplicates, ~4,600 LOC, R0 (`DEAD-03`)
2. `hosts/` package, ~500 LOC, zero callers, R0 (`STR-001`)
3. `frameworks/` package, 9 files, zero callers, R0 (`STR-004`)
4. `cache/store.py`'s dead `SemanticCache` stub + `__init__.py` exports, ~45 LOC, R0 (`PRV-01`)
5. `LineageStore` dual-write system, ~700 LOC, proven zero production callers, R2 (needs a direction decision — delete vs. finish the migration) (`ST-02`)
6. `context_signal.py` + 3 dependent test files, ~145 LOC, R0, self-admittedly dead (`STR-002`)
7. `.claude/hooks/` 3 tracked-despite-`.gitignore` files, ~1,300 LOC, R0 (`RTE-001`/`CFG-002`)
8. `routing.db` orphaned data file + 2 doc references that instruct deleting it as a fix, R0 (`ST-01`)
9. `scripts/pre-release-checklist.py`, points at a nonexistent file, R0 (`DEAD-06`)
10. `savings_stats.mode` column + its migration, zero writers/readers, R1 (`ACC-04`)

### §61 — Top 10 consolidations
1. Prompt classification: hook `SIGNALS` vs. `classify.py` `_SIGNALS` → one shared engine (`RTE-002`)
2. Budget/cost-cap: 9 flat files → `budget/` package (pure move, zero logic change)
3. Direct SQLite access: 63 files → `storage/service.py` (ratchet-lint migration, ceiling-down) (`STR-003`)
4. `savings_log.jsonl` path helper: 6 redefinitions → `cost.savings_log_path()` (`OBS-01`)
5. Host-support registries: 4 disagreeing sources → `hosts/events.py` + `host_detect.py` merged (`HOST-02`)
6. "Free provider" set: 2 literals → one constant (`ACC-05`)
7. "Session" concept: 6 mechanisms, 2 empty in production — needs a live/dead answer before consolidating (`ST-05`)
8. Two schema-migration systems: formal `migrations/` vs. `cost.py`'s 45 ad hoc `ALTER`s → pick one direction (`ST-04`)
9. Dashboard/savings MCP tools: `llm_savings`/`llm_savings_dashboard`/`llm_dashboard` → one (`MCP-04`)
10. `context/` package: `context.py`, `context_prep.py`, `context_optimizer.py`, `context_injection.py`, `code_context.py` → one package (pure move)

### §62 — Top 10 correctness risks
1. `SEC-002` — credential exfiltration via `openai_compat_base_url`/pxpipe (CRITICAL, confirmed live)
2. `SEC-001` — `run_command` zero containment (CRITICAL, disclosed/accepted)
3. `RTE-004` — direct-execution path bypasses budget/quota/gates/redaction (HIGH)
4. `SEC-006` — `AGENT_WRITES=off` does not stop `run_command` writes (HIGH)
5. `CFG-010` — `last`/`retrospect` always exit 0, live-confirmed (HIGH)
6. `ACC-01` — headline savings figure ≈3,534× its own honest accessor (HIGH)
7. `ERR-01` — only ≈8.6% of broad excepts are accounted for (HIGH)
8. `HOST-01` — Pi host has zero working install path (HIGH)
9. `STR-001`/`STR-003` — dead/unadopted abstractions creating "which implementation is real" ambiguity across the tree (HIGH)
10. `TST-01` — false-positive test-isolation error that actively misdirects triage (MEDIUM-HIGH)

### §63 — Top 10 security risks (ranked by real exploitability, no inflation — per `10_security_privacy.md`)
1. `SEC-002`/`SEC-003` — credential exfiltration; real key sent to attacker host from opening a directory with a malicious `.env`, no other user action. Not previously documented, highest confidence, highest impact.
2. `SEC-001` — `run_command` zero containment. Already disclosed, tested, default-on.
3. `SEC-006` — `AGENT_WRITES=off`/`propose` does not cover `run_command`; contradicts a specifically-named safety knob's own promise.
4. `SEC-004` — Ollama URL "allowed by design" for arbitrary external hosts; sends full prompt+tool-result content, not credentials.
5. `SEC-011` — unhardened permissions on `agent_writes` journal pre-images (can contain a secret from a pre-edit file).
6. `SEC-008` — debug log has no scrub-on-write invariant (no proven leak found; sampled, not exhaustive).
7. `SEC-005` — `SECURITY.md`'s own risk table is stale (credibility risk, not exploitability).
8. `SEC-007` — AWS-secret regex partial-redaction edge case (low likelihood, low impact).
9. `SEC-010` — alert-webhook destination unvalidated (payload scrubbed, caps damage).
10. `SEC-009` — `classification_allowlist` fails open by design (documented, intentional — listed for completeness, not as a bug).

### §64 — Top 10 documentation problems (incorrect mental models)
1. `DOC-03`/`HOST-03` — Codex auto-routing mislabeled "not yet"; the doc self-contradicts 4 ways
2. `ST-01` — `routing.db` troubleshooting instructions are a no-op that gives false confidence
3. `HOST-01` — fictional "Pi (pi.dev)" host with a fully-documented, non-functional activation command
4. `DOC-01`/`DOC-08`/`MCP-01` — tool count "60" repeated identically in 3–4 places, actual is 70
5. `DOC-02` — default tier documented as 11 tools, actually 12; the omitted one is the write/exec-capable tool
6. `CFG-004` — `safe_config.py`'s documented config precedence is backwards vs. the code
7. `DOC-07` — per-host savings percentages presented with no caveat, next to a section that models the correct caveat
8. `SEC-005` — `SECURITY.md`'s own gap table is stale (in the safer direction, but still wrong)
9. `DOC-04` — "Run tests (1900+)" undercounts the real suite by >5×
10. `DOC-10` — `architecture/` (16 files) entirely unlinked from README, overlapping `guide/ARCHITECTURE.md`

### §65 — Top 10 testing problems (false confidence)
1. `TST-01` — reproducible false-positive isolation error, order-dependent, misdirects triage
2. `tests/install/test_m11_declared_dependencies.py::_declared()` unions any optional extra into "declared" — the exact guard built to prevent DEAD-01/02's class cannot catch a third instance
3. `ERR-01` — failopen coverage is 8.6%, not the ~16% the codebase's own docstring implied
4. `CFG-007` — `env_registry`'s own AST scanner has a structural blind spot for indirect reads (12+ vars invisible to its "complete" claim)
5. No concurrency test exists for the acknowledged `AC-5` dual-writer race in `savings_log.jsonl` (`OBS-01`)
6. `ACC-06` — a registry entry marked `canonical=True` that a test does not actually enforce
7. `test_matrix_no_longer_claims_hooks_are_impossible_elsewhere` only checks the doc *mentions* the module, never that its claims *agree* with it — this is why `HOST-03`'s 4-way contradiction went uncaught
8. `TST-05` — implementation-coupled (call-shape) assertions provide a false sense of coverage for session-context wiring
9. No type checker configured anywhere in the repo (fact, confirmed by grep — not itself scored, but a genuine testing-strategy gap)
10. CI's `ruff check` scope excludes `scripts/` and `_quarantined_tests/` entirely — this is *why* `REC-004`'s 3 broken scripts survived 52 commits and 2 release cuts with a green CI

---

## 7. Deletion ledger (§49)

Grouped per instruction: safe now / after deprecation / after migration / needs
evidence / retain. LOC figures are as measured in the source domain report.

### Safe now (R0 — pure deletion, zero production callers proven by exhaustive grep)

| Candidate | Est. LOC | Evidence | Pre-check before deleting |
|---|---:|---|---|
| 24 root-level `scripts/` duplicates | ~4,600 | `DEAD-03`: single-commit reintroduction, dated, diverged from canonical copies | Re-diff each pair once more for a root-only fix the subdir copy lacks (none found in this pass) |
| `hosts/` package (`base.py`, `cursor.py`, `events.py`, `gemini_cli.py`, `hook_io.py`) | ~500 | `STR-001`: zero `src/` references outside itself | Confirm no doc/README claims this as a public extension point |
| `frameworks/` package (9 files) | n/a (9 files) | `STR-004`: zero production importers; real Agno usage imports `integrations.agno` directly | Confirm README doesn't claim these framework integrations as shipped (`DOC-06` cross-reference) |
| `cache/store.py`'s `SemanticCache`/`SemanticCacheEntry` + `__init__.py` re-export | ~45 | `PRV-01`, confirmed dead by `13_verify_structure_docs.md` | Re-grep `docs/`/`scripts/` (not just `src/`/`tests/`) immediately before deleting |
| `context_signal.py` + 3 dependent test files | ~145 + tests | `STR-002`: module's own docstring already says it's dead; ratchet-tracked | Confirm `hooks/auto-route.py`'s own `_is_context_dependent` isn't missing a fix `context_signal.py` has |
| `.claude/hooks/auto-route.py`, `usage-refresh.py`, `version-guard.py` | ~1,300 | `RTE-001`/`CFG-002`, confirmed by `13_verify_routing_config.md`: never registered, never executes | Confirm via `git blame` no other machine's `settings.json` could point here |
| `routing.db` (data file) + 2 doc references (`TROUBLESHOOTING.md`, `HOST_SUPPORT_MATRIX.md`) | n/a (data) | `ST-01`, confirmed: 0 rows in 12/13 tables | One more grep pass over `guide/` for any other `routing.db` mention |
| `scripts/pre-release-checklist.py` | ~500 | `DEAD-06`: uncalled, points at a nonexistent file | Confirm no onboarding doc references it (checked clean) |
| `intercept_bench.json` (repo root) | n/a | `REC-006`: no consumer, generated output | Confirm no doc/CHANGELOG cites a number from this file |

### After deprecation (public PyPI API — needs a release-cycle window, not a silent removal)

| Candidate | Evidence | Why not now |
|---|---|---|
| `cost.py`'s 8-function reporter cluster (`log_savings`, `format_spend_for_display`, `get_usage_summary`, `log_quota_snapshot`, `get_router_efficiency`, `get_classifier_overhead`, `get_cache_hit_stats`, `log_quality_trend`, `refresh_baseline_pricing_from_api`) + `trace_id.derive_trace_id` + `judge_cascade.should_judge_inline`/`should_cascade` | `DEAD-05`/L-03, already ratchet-tracked, zero `src/` callers confirmed independently | Public names on a package published to PyPI — deletion is a breaking change for any downstream importer, per the repo's own stated policy |

### After migration (real behavior/data decision needed, not pure deletion)

| Candidate | Direction options | Risk |
|---|---|---|
| `LineageStore` dual-write system (~700 LOC) | (a) delete outright (zero production callers, real data lives in `model_tracking.jsonl` instead), or (b) finish the migration the code comment says was never done and delete `model_tracking.py` instead | LOW if (a); the repo's own evidence favors (a) — `model_tracking.jsonl` is the one with real, current data (`ST-02`, `ST-03`) |
| Direct SQLite access — 63 files → `storage/service.py` | Ratchet-lint (baseline=63, count-down-only), one file at a time, matching the repo's own precedent for `scripts/lint_unknown_as_number.py` | MEDIUM-HIGH if migrated carelessly (each site has its own transaction assumptions); LOW if incremental with existing tests as the safety net |
| Two schema-migration systems | Fold `cost.py`'s battle-tested `ALTER TABLE` pattern into the formal `migrations/` package, or vice versa — pick one, don't leave both live | `ST-04` |

### Needs evidence (UNCERTAIN — do not silently classify as DEAD, per brief §1)

| Candidate | Open question |
|---|---|
| `feedback_handler.py`, `gateway_service.py`, `budget_lineage_reconciliation.py`, `commands/admin_actions.py`, `control_plane/store_postgres.py` | Flagged by 2 prior audit generations, still zero-inbound today — a runtime-coverage run or a dynamic-dispatch check has never actually been done (`REC-007`) |
| `sessions.db`/`session_summaries` (0 rows) | Is the agentic multi-session feature live anywhere, or is this schema aspirational? (`ST-05`) |
| `StorageService`/`budget_store.py`/`budgets.db` (0 rows) | Is any budget-enforcement code path live? (`ST-06`) |
| `audit.db`'s tamper-evident log (0 rows) | Depends on the `ST-06` answer — the audit log may only fire on a budget-related trigger that never occurred here (`ST-07`) |
| `decisions/`+`signals/` mini decision-engine | Needs a runtime trace of a live routed request confirming `router.py` never reaches it (`STR-007`) |
| `moonshot`/`minimax`/`zhipu`/`arcee` providers | Document them, or if genuinely unmaintained, say so and consider deletion (`DOC-06`) — Providers-domain confirmation needed first |

### Retain (explicit, with reasoning)

| Item | Why |
|---|---|
| `control_plane/api.py` + `reconciliation.py` (490 LOC, excluded from wheel) | Deletion requires a product decision on whether `control_plane/audit.py` will ever be written for this distribution (`DEAD-04`/M-10) |
| `audit/2026-09-21/`, `audit/2026-09-22/` (23 files, ~800KB) | Archive (one dated summary per generation), don't delete outright — the record of "what was found and fixed" has value the raw duplicate detail does not (`REC-003`) |
| `ProviderQuirk` Protocol, `StorageAdapter` Protocol *design* (not its adoption gap), `env_registry.py`, `secret_scrubber.py`, `context_injection` choke point, `failopen.py` design, `_quarantined_tests/` triage process | See §13 do-not-change register below |

---

## 8. Consolidation ledger (§50)

| Concepts | Current implementations | Canonical | Deleted concepts | Risk |
|---|---|---|---|---|
| Prompt classification | `hooks/auto-route.py` `SIGNALS` + `classify.py` `_SIGNALS` (diverged) | One shared `_SIGNALS`/scorer module | The non-canonical copy, after a byte-identity/behavioral-equivalence test passes | MEDIUM (needs a replay/equivalence test before merging, per `RTE-002`) |
| Semantic response cache | `cache/store.py::SemanticCache` (dead stub) + `semantic_cache.py::SemanticCache` (real) | `semantic_cache.py` | `cache/store.py`'s stub + `cache/__init__.py`'s re-export | LOW |
| Host installation | `hosts/` Protocol+adapters (uncalled) + `commands/install.py` inline logic (real) | `commands/install.py`'s inline approach | `hosts/` package (or the reverse, if a future host-adapter refactor is explicitly planned) | LOW-MEDIUM |
| Framework adapters | `integrations/agno.py` (real) + `frameworks/*` (stubs, 1 shim) | `integrations/agno.py`, until a 2nd adapter goes concrete | `frameworks/` package | LOW |
| "Budget"/cost-cap subsystem | 6 flat files (`budget.py`, `budget_backend*.py`, `budget_envelope.py`, `budget_key.py`, `budget_lineage_reconciliation.py`) + `budget_store.py` | New `budget/` package, files renamed in place | None — pure move | LOW |
| Direct SQLite access | 63 files, raw `sqlite3`/`aiosqlite` | `storage/service.py` + `storage/adapters/*` | None — migration, not deletion | HIGH effort, LOW per-site risk if incremental |
| `savings_log.jsonl` path lookup | 6 independent `_savings_log_path`/`_savings_log_file` definitions | `cost.savings_log_path()` | The 6 redefinitions | LOW if the 6 are behaviorally equivalent (3 of 6 spot-checked identical; verify the rest before merging) |
| Host-support registry | `host_detect.py` (3 hosts) + `install.py`'s `_HOST_SNIPPETS` (12) + `hosts/events.py` `HOSTS` (4) + `HOST_SUPPORT_MATRIX.md` (7, one fictional) | `hosts/events.py` + `host_detect.py` merged, doc generated from it | The other 2 hand-authored enumerations | LOW-MEDIUM (moderate effort, unifies 3 registries into 1 canonical + thin views) |
| "Free provider" set | `cost.py:1117` inline set + `savings_report.py:39 _FREE_PROVIDERS` | One module constant (`cost.FREE_PROVIDERS`) | The second literal | LOW |
| Provenance vs. verified/unverified filtering | `cost.production_only()` (usage/claude_usage/codex_usage/gemini_usage) + `savings.VERIFIED_SAVED_SQL` (savings_stats only) | Not a merge — a coverage gap. Apply BOTH concepts to the tables that have them, not neither | None deleted | See `ACC-01` |
| `context/` family | `context.py`, `context_prep.py`, `context_optimizer.py`, `context_injection.py`, `code_context.py` (5 flat files) | New `context/` package, files renamed in place | None — pure move | LOW |
| Savings/dashboard MCP tools | `llm_savings` (admin.py), `llm_savings_dashboard` (dashboard.py), `llm_dashboard` (admin.py) | One, after confirming `llm_savings_dashboard`'s real caller count | Whichever of the 3 is confirmed uncalled | LOW-MEDIUM, needs a follow-up grep (`MCP-04`) |

---

## 9. Target architecture (§51) and fitness (§52 / §71–72)

### Current vs. target tree (minimal-change; per `04_structure_duplication.md §11`, adopted as the base proposal)

```
src/llm_router/                          src/llm_router/  (target)
  budget.py                                budget/                  # NEW — consolidates 6 flat files
  budget_backend.py                          __init__.py, backend.py, backend_postgres.py,
  budget_backend_postgres.py                 envelope.py, key.py, lineage_reconciliation.py, store.py
  budget_envelope.py
  budget_key.py
  budget_lineage_reconciliation.py
  budget_store.py

  context.py                               context/                 # NEW — consolidates 5 flat files
  context_prep.py                            __init__.py, session.py, prep.py, compress.py,
  context_optimizer.py                       knowledge.py, code.py
  context_injection.py                     context_signal.py         # DELETED (STR-002)
  code_context.py

  policies/  (6 YAML)                      policy_presets/            # RENAMED, content unchanged
  rules/  (13 Markdown)                    host_rule_templates/       # RENAMED, content unchanged

  hosts/  (uncalled Protocol)              hosts/                     # DELETE, unless install.py is
                                                                        # refactored to use it — pick one
  frameworks/  (mostly stubs)              frameworks/                # DELETE or freeze as docs/ROADMAP.md
                                                                        # entry until a 2nd real adapter ships

  cache/store.py (dead stub)               cache/                     # KEEP classification.py only;
                                                                        # DELETE store.py's SemanticCache

  decisions/ + signals/                    decisions/ + signals/      # PENDING a runtime trace (STR-007) —
                                                                        # DELETE if confirmed unreached, else
                                                                        # document why it's intentionally parallel

  <router.py, gateway.py, sdk.py,          execution/                 # LONGER-HORIZON (Phase 4+): one
   direct_executor.py — 4 pipelines>          core.py (route_and_call, budget/quota/gates/redaction)
                                               direct.py (thin adapter over core.py for the free/local
                                                          fast path, NOT a parallel safety-free pipeline)
                                             — collapses RTE-004's 4-pipeline split into 1 governed core
                                               with thin callers; this is the one change in this document
                                               that is genuinely architectural, not a move/rename

  <everything else unchanged>              commands/, tools/, semantic/, agentic/, agents/, storage/, etc.
                                             — no evidence in any domain report justifies restructuring
                                               these beyond the STR-003 storage-adoption ratchet
```

**Permitted/forbidden dependency rule to add** (new — evidenced by `STR-003`): any
module writing SQLite must import `storage.service` or `storage.adapters.sqlite_adapter`;
a direct `import sqlite3`/`import aiosqlite` outside `storage/` becomes a lint-enforced
violation, ratcheted down from a `BASELINE = 63` the same way `scripts/lint_unknown_as_number.py`
already ratchets a different metric in this codebase.

### Per-package responsibility (new/changed packages only)

| Package | Responsibility | Allowed deps | Forbidden deps |
|---|---|---|---|
| `budget/` | Cost-cap/quota accounting lineage only (not per-provider $ limits, not per-request token allocation — those get their own names per `PRV-03`) | `storage/`, `config.py`, `types.py` | Direct `sqlite3`/`aiosqlite` (must go through `storage/`) |
| `context/` | Session buffer, prompt assembly, compression, OKF knowledge injection, AST code-context — 5 distinct stages, one package | `okf.py`, `repo_facts.py`, `secret_scrubber.py` | None new |
| `execution/` (Phase 4+ target) | The one place a routed prompt is executed against a model, governed uniformly (budget/quota/gates/redaction always applied) | `providers.py`, `provider_quirks.py`, `budget/`, `gates.py`, `redaction_routing.py` | A second execution path that skips any of the four governance checks |

### Fitness scenarios (§52/§71-72) — files touched today, evidenced

| Scenario | Files touched today | Why |
|---|---|---|
| Add a model with tools + structured output | ~3-4: `pricing.py` (lint-enforced), `config/models.yaml`, `policies/standard.yaml`, optionally `model_aliases.py` | `providers.py`'s single litellm adapter means tool/structured-output support is not a separate integration point |
| Add a genuinely new provider | Cheap (same file set + one `ProviderQuirk` subclass + a `tiers.py` entry) **if and only if litellm already speaks the wire protocol** — otherwise unbounded, no raw-HTTP fallback exists | `07_providers_perf.md §71` |
| Add a host | 1 file in practice (`install.py`, another `json_targets.append`) but architecturally should be 1 via `hosts/<name>.py` — both are single-file answers via completely different, uncoordinated mechanisms | `STR-001`, `HOST-05` |
| Add a policy/task type | 7-8 files minimum (`types.py`, `classify.py`'s 3+ `ClassifyPolicy` instances, `hooks/auto-route.py`'s own tables, `tool_surface.py`, `chain_builder.py`, `enforce-route.py`, tests) | `02_runtime_routing.md §71` |
| Change accounting | 1 canonical query layer exists (`dashboard_data.py`) by design, but it currently misses provenance filtering for 3 of 4 tables — the "one place" claim is aspirationally true, not yet actually true (`ACC-01`) |
| Change storage | Depends entirely on whether the table is one of the 63 direct-SQLite bypass sites — 1 file if migrated to `storage/`, 2-3 if not (schema+write site+read site all live in the same un-abstracted file) | `STR-003` |
| Remove a provider | ~5 (mirror of "add") | |
| Remove a host | 1 file via `install.py`'s real path; `hosts/` could be deleted with zero effect on removal either | `STR-001` |
| Remove an experimental subsystem (e.g. `frameworks/`) | 1 package deletion + 3 test-file import-site updates | `STR-004` |
| Add a persisted field | 1 file if the table already routes through `storage/`; realistically 2-3 if it's one of the 63 bypass sites | `STR-003` |

### One-source-of-truth audit (§73, rolled up across domains)

| Concept | Canonical source? | Status |
|---|---|---|
| Version | `llm_router.__version__` (dynamic resolution) | **GOOD** — genuine SSOT, deliberately designed; `service.py`'s 2 hardcoded literals are the only violation (`REC-001`) |
| Env vars read by `src/` | `env_registry.py` + AST-scanner test | **MOSTLY GOOD** — self-defending design, real blind spot for indirect reads (`CFG-007`) |
| Pricing per model | `pricing.py`, CI-lint-enforced | **GOOD** — genuinely enforced, has caught the same stale-Opus-rate bug 5 times historically |
| Model/provider capability | `capabilities.py` ("the single shared source of truth") | **GOOD**, no drift found |
| MCP tool tier membership | `tool_surface.py` | **GOOD** for the code; narrative docs (`server.py` docstring, `guide/TOOLS.md`, README) drift from it (`MCP-01`, `DOC-01`) |
| Host support | 4 disagreeing registries | **NOT SSOT** — `HOST-02` |
| Routing policies (profile × task → model list) | `policies/standard.yaml` → `profiles.ROUTING_TABLE`, test-mirrored | **GOOD** |
| "Which models are cheap" | 4 independent hand-maintained tables, currently agreeing but unenforced | **NOT SSOT** but not yet drifted (`07_providers_perf.md §73`) |
| Classification (task type) | 2 diverged tables | **NOT SSOT** — `RTE-002` |
| Savings/cost calculation | `savings.canonical_savings()` exists and is correct where used; 17/20 registered surfaces bypass it | **NOT SSOT in practice** — `ACC-01`, `ACC-06` |
| Config precedence | 3 modules, 3 different stated orders, one (`safe_config.py`) wrong relative to the code it describes | **NOT SSOT** — `CFG-004` |
| CLI command list | `_KNOWN_SUBCOMMANDS`, explicitly self-scoped as non-authoritative (typo-suggestion only) | **ACCEPTABLE** — low blast radius by design (`CFG-012`) |
| Doc capability tables | `HOST_SUPPORT_MATRIX.md` self-contradicts 4 ways within one file | **NOT SSOT** — `HOST-03` |

---

## 10. Refactor plan (§54) with risk classes R0–R5

| Class | Definition | Examples from this audit |
|---|---|---|
| **R0** | Pure deletion, zero behavioral risk, proven unused by exhaustive grep | `scripts/` duplicate cluster, `hosts/`, `frameworks/`, `cache/store.py` stub, `context_signal.py`, `.claude/hooks/`, `routing.db`, `pre-release-checklist.py` |
| **R1** | Doc-only or comment-only fix, zero behavioral risk | `DOC-01..10`, `SEC-005`, `CFG-004`'s docstring, `MCP-02`, `DEAD-06`'s cross-references |
| **R2** | Internal rename/move, no behavior change, import updates only | `budget/` package move, `context/` package move, `policies/`→`policy_presets/`, `rules/`→`host_rule_templates/` |
| **R3** | Internal consolidation merging duplicate implementations; behavior-preserving by construction but needs an equivalence check before the old copy is deleted | Classification-engine merge (`RTE-002`), `savings_log_path()` merge (`OBS-01`), free-provider set merge (`ACC-05`) |
| **R4** | A policy/behavior change requiring a product decision; low-to-medium user impact, needs a flag or migration window | `SEC-004`'s Ollama-host default (same-host-only vs. explicit remote opt-in), `ACC-07`'s subscription-detection default direction, `ST-02`'s LineageStore direction |
| **R5** | Breaking/user-facing change — a public number changes, a public API is removed, a default changes in a way most users will notice | `ACC-01`'s fix (the headline "saved" number will drop sharply — this is a correction, not a regression, per the project's own doctrine), `DEAD-05`'s public-API deprecation, `STR-003`'s full storage-layer migration once complete |

### Phased plan (§54)

- **Phase 0 — baseline + guardrails.** Add regression tests for every behavior this
  audit's live reproductions depend on, *before* anything else moves: `AGENT_WRITES=off`
  scope (does not cover `run_command`), `openai_compat_base_url`/pxpipe URL validation
  (does not exist yet — this test is written to fail, driving Phase 2), the
  `test_m11_declared_dependencies.py::_declared()` tightening, and a `dashboard_data.py`
  provenance-filter test mirroring `test_a31b_no_raw_savings_sum.py`.
- **Phase 1 — proven deletions.** The "safe now" (R0) deletion-ledger rows, in one PR
  per row group, full suite green after each.
- **Phase 2 — local simplification.** Small, isolated fixes: `CFG-010`'s `sys.exit`
  wrapping, `DEAD-01`/`DEAD-02`'s dependency declarations + `_declared()` fix,
  `TST-01`'s `__init__.py` additions, `REC-001`'s version-literal fix, `SEC-002`/`003`/`004`/`006`'s
  security patches, `ACC-01`'s provenance filter.
- **Phase 3 — semantic consolidation.** Classification-engine merge (after an
  equivalence test proves parity — same bar `HOOK_LIVE_POLICY` already cleared at
  1000/1000), `savings_log_path()` merge, storage-bypass ratchet-lint kickoff (baseline
  recorded, count-down begins).
- **Phase 4 — package restructuring.** `budget/` and `context/` package moves; `hosts/`
  and `frameworks/` final resolution; the `execution/` core consolidation (the one
  genuinely architectural change, R5-adjacent — needs its own design review, not bundled
  into this phase's mechanical moves).
- **Phase 5 — public surface reduction.** `DEAD-05`'s deprecation window opens; MCP
  tool-count documentation becomes generated/tested, not hand-maintained.
- **Phase 6 — docs.** `HOST_SUPPORT_MATRIX.md` and `guide/TOOLS.md` regenerated from
  code (`tool_surface.py`, `hosts/events.py`) rather than hand-authored; `architecture/`
  vs. `guide/ARCHITECTURE.md` resolved (`DOC-10`).
- **Phase 7 — README.** Ship `PROPOSED_README.md` (Section 15 of this document) once
  Phases 1-2's fixes have landed, so its claims are accurate on day one rather than
  needing a second correction pass.

---

## 11. Atomic commit plan (§55) — Phase 0, 1, 2

Each commit: title / files / why / behavior preserved / validation / expected net change.

**Phase 0**

1. **`test: pin AGENT_WRITES=off's actual scope (does not cover run_command)`**
   Files: new `tests/test_sec006_agent_writes_scope.py`.
   Why: `SEC-006` has no existing regression test; without one, a later fix to close
   the gap has no way to prove it worked, and a later refactor could accidentally widen
   the gap further.
   Behavior preserved: yes (test-only, currently documents the gap as failing-if-fixed
   or passing-as-is — written to assert current behavior, then flipped in Phase 2).
   Validation: `pytest tests/test_sec006_agent_writes_scope.py -q`.
   Expected net change: +1 test file, 0 production LOC.

2. **`test: pin dashboard_data.query_window's missing provenance filter (ACC-01)`**
   Files: new `tests/test_acc01_provenance_filter.py`, modeled on the existing
   `test_a31b_no_raw_savings_sum.py` shape.
   Why: establishes the exact assertion Phase 2's fix must satisfy, and prevents
   re-regression once fixed.
   Behavior preserved: yes.
   Validation: test currently fails (documents the gap); this is expected and is the
   Phase-0 acceptance criterion — a red test that will go green in Phase 2.
   Expected net change: +1 test file.

3. **`test: tighten test_m11_declared_dependencies.py::_declared() to core deps only`**
   Files: `tests/install/test_m11_declared_dependencies.py`.
   Why: `_declared()` currently unions every optional extra into "declared for core,"
   which is why DEAD-01/DEAD-02 were never caught — this fix must land before the
   dependency declarations themselves, so the new test actually red-flags the current
   state.
   Behavior preserved: no production code touched; this test will now correctly FAIL
   against current `pyproject.toml` until commit 6 (Phase 2) lands.
   Validation: `pytest tests/install/test_m11_declared_dependencies.py -q` — expected
   RED after this commit, GREEN after commit 6.
   Expected net change: ~10-15 line test diff.

**Phase 1**

4. **`chore: delete stale root-level scripts/ duplicate cluster (DEAD-03)`**
   Files: delete 24 files listed in `03_dead_code_deps.md`'s DEAD-03 table; no change
   to `scripts/{ci,release,bench,dev}/`.
   Why: single-commit reintroduction (2026-08-19), zero CI/doc references, diverged
   from the maintained subdirectory copies.
   Behavior preserved: yes — nothing in CI or documented workflows points at the
   deleted paths (grep-confirmed).
   Validation: `.github/workflows/{ci,benchmarks,self-audit}.yml` all green; repo-wide
   grep for the 24 deleted filenames returns only the 2 known non-blocking references
   (`pre-release-checklist.py`, removed in the same PR; `sync_downstream.py`, updated
   in the same PR to exclude these paths from future syncs).
   Expected net change: -4,600 LOC, -24 files.

5. **`chore: delete unadopted hosts/ Protocol, dead cache stub, and context_signal.py (STR-001, PRV-01, STR-002)`**
   Files: delete `src/llm_router/hosts/*`, `src/llm_router/cache/store.py`'s
   `SemanticCache`/`SemanticCacheEntry` + their `cache/__init__.py` re-export, delete
   `src/llm_router/context_signal.py` + its 3 dedicated test files.
   Why: each proven zero-production-caller by exhaustive grep in its own domain
   report; `context_signal.py`'s own docstring already says it's dead.
   Behavior preserved: yes.
   Validation: full `pytest -q` green (the 3 deleted test files' assertions cover only
   the deleted module; no other test references it).
   Expected net change: -~700 LOC across 3 unrelated dead components in one PR (grouped
   because each is independently trivial; kept as 3 logical diffs in one commit for
   easy revert if any single one surfaces an unexpected caller).

6. **`chore: delete stale .claude/hooks/ tracked-despite-gitignore files, routing.db, pre-release-checklist.py, intercept_bench.json (RTE-001/CFG-002, ST-01, DEAD-06, REC-006)`**
   Files: `git rm .claude/hooks/{auto-route,usage-refresh,version-guard}.py`,
   `~/.llm-router/routing.db` is a data file (delete on the machine, not in git;
   correct the 2 doc references in `guide/TROUBLESHOOTING.md`,
   `guide/HOST_SUPPORT_MATRIX.md`), `git rm scripts/pre-release-checklist.py`,
   `git rm intercept_bench.json`.
   Why: none of the four executes in a standard install or is referenced by any
   consumer.
   Behavior preserved: yes.
   Validation: `git log -1 -- .claude/hooks/` confirms deletion; doc build (if any)
   still resolves; no CI step references `intercept_bench.json` or
   `pre-release-checklist.py`.
   Expected net change: -~1,800 LOC / 4 files + 2 doc-line corrections.

**Phase 2**

7. **`fix: propagate exit codes from last/retrospect/snapshot (CFG-010)`**
   Files: `src/llm_router/cli.py` (3 dispatch branches).
   Why: `main() -> int` functions with documented `return 1` paths are called without
   `sys.exit()`, so the process always exits 0 — the exact bug class already fixed once
   for `verify` (`CHZ-PKG-005`).
   Behavior preserved: the only behavior change is a script now correctly seeing
   nonzero on the documented failure path.
   Validation: the parametrized `--help`/exit-code test proposed under `CFG-009` (or a
   narrower one scoped to these 3 commands) asserts `SystemExit(1)` on the known
   failure path for `last`.
   Expected net change: 3-line diff.

8. **`fix: declare pyyaml and httpx as core runtime dependencies (DEAD-01, DEAD-02)`**
   Files: `pyproject.toml` (`[project.dependencies]`).
   Why: both are imported unguarded at module scope on the MCP server's live import
   path; today they are satisfied only by luck (an installed optional extra or
   litellm's own transitive graph), not by declaration.
   Behavior preserved: yes — this only makes an already-necessary dependency explicit;
   no import path changes.
   Validation: `tests/install/test_m11_declared_dependencies.py` (tightened in commit
   3) now passes; construct a venv with only `[project.dependencies]` installed and
   import `llm_router.server` to confirm no `ModuleNotFoundError`.
   Expected net change: 2-line `pyproject.toml` diff.

9. **`fix: add __init__.py to six namespace packages to close the test-isolation false positive (TST-01)`**
   Files: `src/llm_router/{ui,hooks,policies,static,rules,commands}/__init__.py` (new,
   empty or re-exporting anything already re-exported elsewhere).
   Why: `_no_module_state_leak`'s heuristic cannot distinguish a namespace package's
   genuine `__file__ is None` from a test-stub mock; this was independently verified
   to fix the issue with zero regressions (17/17 pass) by the verification pass.
   Behavior preserved: yes — verified: `import llm_router.hooks.agent_loop` and
   `import llm_router.commands.doctor` both succeed identically after the fix; hooks
   shipped as hyphenated standalone scripts are unaffected (hyphens were never valid
   Python identifiers regardless of `__init__.py`).
   Validation: `pytest tests/test_first_forty_w2.py -q` alone, before and after — the
   spurious `ERROR at teardown` disappears; full suite unaffected.
   Expected net change: +6 near-empty files.

10. **`fix: validate openai_compat_base_url and llm_router_pxpipe_url; stop forwarding the real provider key to a config-redirected api_base by default (SEC-002, SEC-003)`**
    Files: `src/llm_router/config.py` (add `field_validator`s reusing/generalizing
    `validate_ollama_url`'s pattern), `src/llm_router/provider_quirks.py`
    (`OpenAICompatQuirks`/`AnthropicPxpipeQuirk.transform_request` set an explicit,
    inert `api_key` unless the operator opts in to forwarding the real one).
    Why: reproduced live — a canary API key was captured at an attacker-controlled
    listener with no user action beyond a project directory containing a malicious
    `.env`.
    Behavior preserved: legitimate local-inference users on localhost/LAN are
    unaffected by a same-host-default; a genuinely remote self-hosted server needs an
    explicit override — this is the correct new prompt to add, not a regression.
    Validation: a new adversarial test mirroring `test_r3_allowlist_is_not_containment.py`'s
    rigor — assert the validator rejects a non-local host, and assert no real provider
    `api_key` reaches `litellm.acompletion` kwargs when `api_base` is set from config.
    Expected net change: ~2 validators + ~2 call-site changes; this is R4 (needs a
    product decision on the exact allowlist shape, not purely mechanical) but is
    time-critical given the CRITICAL severity.

11. **`fix: apply production_only()/provenance filtering to dashboard_data.query_window's usage/claude_usage/codex_usage/gemini_usage branches (ACC-01)`**
    Files: `src/llm_router/dashboard_data.py` (`query_window`, `query_daily`).
    Why: these branches currently have zero `is_simulated`/`production_only` filtering,
    unlike `savings_stats`, which this exact commit's predecessor (`3c96d23`) just
    fixed — the fix must be extended to the other 3 tables, not left as the one table
    that got it.
    Behavior preserved: this is R5 — the headline number will drop sharply (from
    ≈$372.66 to a small fraction of that), which is a correction per the project's own
    stated doctrine (`cost.py:482-486`: "NULL is the honest value... unknown rows drop
    out of money figures rather than being counted as real"), not a regression.
    Validation: the Phase-0 test (commit 2) goes from RED to GREEN; manually re-run the
    live-DB measurement from `09_accounting.md` post-fix and confirm the new number
    matches `get_realized_savings("all","all")`'s canonical figure to within the
    expected residual (verified rows only).
    Expected net change: ~10-20 line diff in one file; large, expected, disclosed
    change to a live number — ship with a CHANGELOG entry, not silently.

---

## 12. Simplification and quality metrics — baseline → justified target (§56–57)

No arbitrary percentages; every target below is tied to a specific mechanism already
proven in this codebase or a specific finding it closes.

| Metric | Baseline (measured) | Justified target | Mechanism |
|---|---|---|---|
| Files bypassing `storage/` with raw sqlite3/aiosqlite | 63 (of ~415 `src/` files) | 0, via ratchet-lint (baseline recorded, count-down-only) | Repo's own precedent: `scripts/lint_unknown_as_number.py` (`STR-003`) |
| Broad-except coverage by `failopen.py` in the "money, routing, verification, telemetry" module set the tool's own docstring already names | ≈8.6% of 1,020 repo-wide (verified) | 100% of the *scoped* module set (not all 1,020 — explicitly out of proportion per `ERR-01`'s own recommendation), via a new ratchet test in the R13/S9b pattern | `ERR-01` |
| `LLM_ROUTER_*` vars with zero test AND zero doc reference | 53 of 198 (`llm_router` category) | 0 among user-facing vars; internal-only vars explicitly marked as such | `CFG-*` §2/§3 |
| Independent "routing decision" execution pipelines | 4 (`router.py`/`gateway.py`/`sdk.py`/`direct_executor.py`) | 1 governed core + thin callers (Phase 4, R5-adjacent, needs its own design review) | `RTE-004`, target architecture §9 |
| Diverging classification tables | 2 (`hooks/auto-route.py` `SIGNALS` vs. `classify.py` `_SIGNALS`) | 1, after an equivalence test proves parity | `RTE-002`, precedent: `HOOK_LIVE_POLICY` at 1000/1000 agreement |
| Root-level `scripts/` stale-duplicate LOC | ~5,100 (incl. `pre-release-checklist.py`) | 0 | `DEAD-03`, `DEAD-06` — Phase 1 |
| Tracked-but-dead audit corpus | 53 files / 872KB, 3 generations, one disclaiming the other two | 2 dated summary files (one per superseded generation) | `REC-003` |
| Dead/unadopted packages removed | `hosts/` (~500 LOC), `frameworks/` (9 files), `cache/store.py` stub (~45 LOC), `context_signal.py` (~145 LOC) | 0 remaining after Phase 1 | §7 deletion ledger |
| Independently-implemented telemetry/JSONL stores | 16+, no single event model | Not a number target — a coverage target: every new store must justify why it isn't a row in an existing one before being added (process fix, not a count) | `OBS-01` |
| Hand-maintained MCP tool-count claims | 4 disagreeing numbers (60, 60, 60, 41) across `server.py`, README, `guide/TOOLS.md`, `tool_tiers.py` | 1 computed, test-pinned number | `MCP-01`, `DOC-01`/`DOC-08` |
| CI lint scope vs. actual Python LOC | `ruff check src/ tests/` = 0 errors, but scopes only ~275K of ~310K total `.py` lines (excludes `scripts/`'s 36,777 lines and `_quarantined_tests/`) | `scripts/` added to CI lint scope, at minimum a `py_compile` smoke check | `REC-004`'s enabler — this is why 3 broken scripts survived 52 commits |
| Format-check enforcement | `ruff format --check .` fails on 1,235/1,388 files; no CI format-check step exists | Not a defect (style, not correctness — brief explicitly says don't score HIGH for ugly code) but worth a baseline note for anyone proposing a format-enforcement pass later | `01_recon_metrics.md §4` |

---

## 13. Do-not-change register (§58)

Compiled from every domain's explicit KEEP/do-not-change nomination. These are the
target shape the rest of the codebase should be pulled toward, not candidates for
simplification.

| Item | Why | Source |
|---|---|---|
| `env_registry.py` + `tests/test_env_registry.py` | Hand-committed declaration checked against an independently-implemented AST scanner, specifically to avoid the "validator checks itself" trap; explicitly scopes its own claim rather than overclaiming | 01, 05 |
| `llm_router.__version__`'s dynamic resolution (`__init__.py:25-46`) | A genuine single source of truth, deliberately designed with a stated rationale ("report the version of the code that is ACTUALLY RUNNING") | 01 |
| `ProviderQuirk` Protocol + 5 concrete quirks | Real substitution point, real callers, identity-default so it costs nothing when unused — textbook correct use of a Protocol | 04, 07 |
| `BudgetBackend` Protocol (sqlite + postgres impls) | Legitimate multi-deployment substitution point, both implementations actually wired | 04 |
| `providers.py`'s single litellm adapter + `provider_quirks.py` | One wire-format adapter shared by every provider; adding a litellm-supported provider requires zero adapter code | 07 |
| `sqlite_wal.py`'s WAL/busy-timeout fix | Measured before/after evidence (4/12 concurrent constructions failed before; 66/2400 events silently lost before the PRAGMA-return-value check was added) | 07 |
| `secret_scrubber.py` as the single canonical scrubber | Wide, consistent fan-in from `alerts.py`, `trace.py`, `attempt_log.py`, `persist_redaction.py`, `session_store.py`, `library/store.py`, 2 hook modules; verified against 13 credential shapes, all correctly redacted | 10 |
| `context_injection.py`'s choke point + `test_okf_choke_point.py` | Correctly fixed a real historical cross-project-contamination incident; every execution path is enforced to import it | 08 |
| `tool_surface.py` + `hosts/events.py`'s `routing_ready()` | The single canonical home for tier membership/deprecation mapping/resolution, and the only place in the repo that commits, in a test, to an honest and narrow claim about what auto-routing actually works today | 06 |
| `hosts/base.py:routing_env()` + its secret-exclusion list | Fixed a real, previously-shipped silent-degradation bug (non-Claude-Code hosts falling back to an uninstalled model); must survive any host-layer refactor | 02 |
| `install_manifest.py` | A genuinely good structural fix (per-write-operation manifest, uninstall replays in reverse) over the per-host special-casing that used to miss subsets on every audit round | 02 |
| `failopen.py`'s design (never-raises, unknown≠zero, unpersisted-loss counter) | Well-reasoned, already fixed once from a worse state (T-07); the gap is rollout breadth (`ERR-01`), not the design | 11 |
| `_quarantined_tests/` triage process | Actively working — 6 of 15 files resolved correctly in two commits, one of which uncovered a real "shipped but unimportable module" bug a naive cleanup pass would have destroyed evidence of | 11 |
| `savings.net_saved()` + the `AUD-06` signed-subtraction discipline; `VERIFIED_SAVED_SQL`/`is_verified_saving` parity pair | Correctly implemented, well-tested; do not edit one without the other | 09 |
| `cost.production_only()`/`is_simulated` provenance mechanism | Correct design (fail-closed, `NULL≠production`); the defect is under-adoption (`ACC-01`), not the mechanism | 09 |
| `LLM_ROUTER_PROFILE`'s value-domain-filter collision mitigation | A textbook example of defusing a hazard you can't immediately remove (value-domain filtering + one-shot deprecation warning + GH-issue-numbered comment trail) | 05 |
| `_KNOWN_SUBCOMMANDS`'s honest scoping | Explicitly, correctly labeled as non-authoritative — worst-case blast radius is a wrong typo suggestion, not a dispatch bug | 05 |
| `SECURITY.md`'s disclosure culture + `tests/test_r3_allowlist_is_not_containment.py` | Unusually honest self-audit of `run_command`'s real capability; this document's gap table is stale (`SEC-005`) but the underlying disclosure practice should be extended, not abandoned | 10 |
| The `run_command` capability trade-off itself | A deliberate, disclosed, tested design decision (capability over containment) — do not "fix" it into a false sense of security without an explicit architecture decision on the alternative | 10 |
| R13/S9b-style ratchet tests generally | Proven pattern: `R13` went from 62 source-text assertions to 1, verified by running the actual detector, not trusting a description of it | 11 |

---

## 14. Go/no-go gates (§38)

Each gate is a condition that must hold before the next phase proceeds — none of them
ask the user anything; they are all mechanically checkable.

| Gate | Condition | Blocks |
|---|---|---|
| **Gate 1** (Phase 0 exit) | Full `pytest` baseline remains 9,640/0 failed/0 errors after adding the 3 guardrail tests; the 2 new tests (`SEC-006` scope, `ACC-01` filter) exist and their current RED/GREEN status is recorded | Phase 1 |
| **Gate 2** (Phase 1 exit) | Full suite green; grep-confirmed zero remaining references to every deleted path (`hosts/`, `frameworks/`, `cache.SemanticCache`, `context_signal`, `.claude/hooks/*`, the 24 `scripts/` duplicates, `routing.db`); CI green including `scripts/` now in lint scope | Phase 2 |
| **Gate 3** (Phase 2 exit) | Every security patch (`SEC-002`/`003`/`004`/`006`) has a passing adversarial regression test reproducing the *original* exploit and proving it is closed (not just "the new validator exists"); `DEAD-01`/`DEAD-02`'s fix is verified via an actual bare-`[project.dependencies]`-only venv import of `llm_router.server`; `CFG-010`'s fix is verified via `SystemExit(1)` assertion, not just a code read | Phase 3 |
| **Gate 4** (Phase 3+ exit — semantic consolidation) | The classification-engine merge passes an equivalence test at 100% agreement against both prior engines' historical decisions (matching the `HOOK_LIVE_POLICY` precedent of 1000/1000), before the non-canonical copy is deleted | Phase 4 |
| **Gate 5** (README ship) | Every numeric claim in `PROPOSED_README.md` cites a `file:line` or a test in the claim ledger; zero UNSUPPORTED/MISLEADING items remain against the same standard `12_docs_claims.md` already applied; `DOC-03`'s Codex row and `MCP-01`/`DOC-02`'s tool counts are verified against live code immediately before publish (numbers move; verify at ship time, not from this document) | Publishing the new README |

---

## 15. Scorecard (§78)

Per dimension: evidence, strengths, weaknesses, actions. No numeric score, per instruction.

### Architecture
- **Evidence:** `02_runtime_routing.md`, `04_structure_duplication.md`.
- **Strengths:** `ProviderQuirk`/`BudgetBackend` Protocols are textbook-correct
  substitution points; `env_registry.py`, `tool_surface.py`, `pricing.py` are genuine,
  enforced single sources of truth; `context_injection`'s choke point correctly closed
  a real cross-project-contamination bug.
- **Weaknesses:** 4 independent routing/execution pipelines with different safety
  guarantees (`RTE-004`); `storage/` and `hosts.HostAdapter` built and never adopted
  (`STR-003`, `STR-001`); "budget" spans 9 files and 3 unrelated meanings.
- **Actions:** Phase 3-4's classification merge and `execution/` core consolidation;
  the storage-bypass ratchet-lint.

### Security / Privacy
- **Evidence:** `10_security_privacy.md`, `13_verify_security_deps.md`.
- **Strengths:** unusually honest self-disclosure (`SECURITY.md`'s `run_command`
  section, verified accurate on its core claim); `secret_scrubber.py` verified against
  13 credential shapes with only one narrow edge case; no committed secrets, no
  `shell=True`/`eval`/`exec`/`pickle` in `src/`; path-traversal/symlink containment for
  5 of 6 file tools is real and adversarially verified.
- **Weaknesses:** 2 CRITICAL findings, both live-reproduced — credential exfiltration
  via `openai_compat_base_url`/pxpipe (new, undocumented) and `run_command`'s zero
  containment (documented, but default-on); `AGENT_WRITES=off` does not cover
  `run_command`; `SECURITY.md`'s own gap table is stale.
- **Actions:** Phase 2 commit 10 (URL validation + key-forwarding fix) is the single
  highest-priority action in this entire report.

### Correctness / Reliability
- **Evidence:** `02`, `05`, `07`, `11`.
- **Strengths:** the WAL/busy-timeout fix is measured, not assumed; the direct-
  execution deadline-aware fix reduced a documented 43% timeout rate to a measured
  32.6% on this machine.
- **Weaknesses:** `last`/`retrospect` always exit 0 despite documented failure paths
  (live-reproduced); `session_spend.py` swallows a write failure with no signal; only
  ≈8.6% of 1,020 broad excepts are accounted for.
- **Actions:** Phase 2 commits 7 and 8; a scoped `ERR-01` ratchet test for the money/
  routing/telemetry module set specifically.

### Testing
- **Evidence:** `11_tests_errors_obs.md`, `13_verify_structure_docs.md`.
- **Strengths:** a stratified 73-file sample found 0 DUPLICATE/LOW-VALUE/OBSOLETE files
  in the live tree; 27 CRITICAL CONTRACT + 22 HIGH-VALUE REGRESSION tests, most named
  after the incident they close; `_quarantined_tests/`'s triage process is real and
  working, not a dumping ground.
- **Weaknesses:** a reproducible false-positive isolation error on 6 namespace
  packages (fix verified 17/17 pass, not yet landed); `test_m11_declared_dependencies.py`'s
  own guard has a blind spot that let DEAD-01/02 through; CI's lint scope excludes
  `scripts/` entirely, which is exactly why `REC-004`'s 3 broken scripts survived 52
  commits.
- **Actions:** Phase 2 commit 9; Phase 0 commit 3; add `scripts/` to CI's `ruff check`
  scope (at minimum a `py_compile` smoke check).

### Observability
- **Evidence:** `11_tests_errors_obs.md §33-34`.
- **Strengths:** `routing_report.py`'s `draft_acceptance()`/`unterminated_invocations()`
  correctly exclude test/benchmark noise and are self-aware about being "logs as state";
  `counter_registry.py` unifies several honest counters behind `doctor`/`status`.
- **Weaknesses:** 16+ independent JSONL/log stores with no single event model;
  `savings_log.jsonl` written by 6 redefinitions of the same path helper with an
  acknowledged, untested dual-writer race; `auto-route-debug.log` (the store 3 counters
  treat as ground truth) has no rotation/cap, unlike every sibling store.
- **Actions:** Top of §61's consolidation ledger (`savings_log_path()` merge); `OBS-02`'s
  rotation fix, reusing `attempt_log.py`'s already-fixed approach.

### Documentation
- **Evidence:** `12_docs_claims.md`, `13_verify_structure_docs.md`.
- **Strengths:** the README's Savings section and its Direct-Execution section are
  genuinely excellent, specific, falsifiable, and self-caveating — a model other
  sections should be held to.
- **Weaknesses:** the Codex auto-routing claim is not just stale but self-contradicts 4
  ways within one file (`HOST_SUPPORT_MATRIX.md`); the MCP tool count is wrong in 3-4
  places from one stale source comment; `routing.db` troubleshooting instructions are a
  documented no-op.
- **Actions:** Phase 6-7; the new README (§16 of this document) fixes the highest-
  impact items on day one.

### Accounting / Trust
- **Evidence:** `09_accounting.md`, `11_tests_errors_obs.md §33 (OBS-03)`, coordinator
  context.
- **Strengths:** the audited commit (`3c96d23`) is itself a real, well-evidenced fix —
  splitting `savings_stats` into verified/unverified, with the split visibly disclosed
  to the user (`totals.unverified_saved_usd` is shown as a separate line, contradicting
  an earlier over-broad claim that no such disclosure existed); `cost.production_only()`
  is a correct, fail-closed design.
- **Weaknesses:** that fix has a scope boundary invisible from the commit message — it
  touches exactly one of four money tables; the headline figure is ≈3,534× the
  provenance-filtered canonical figure for the same data, same instant; verified
  lifetime savings are **$0.00**; 0 of ~1,160 local drafts on the maintainer's own
  machine were ever used to replace a Claude turn.
- **Actions:** Phase 2 commit 11 is the single highest-leverage trust fix in this
  report; the README (§16) must state the verified/unverified distinction plainly,
  not bury it.

### Process / Release
- **Evidence:** `01_recon_metrics.md`.
- **Strengths:** a genuinely rigorous internal self-audit practice across 3 generations;
  the version-resolution and release-tag mechanisms are sound where they're followed.
- **Weaknesses:** HEAD has now been audited/shipped 23 commits past its last tag twice
  in a row; the same defect (3 unparseable scripts) was found once, documented, and
  survived 52 commits and 2 release cuts because CI's lint scope can't see it; the
  audit corpus itself has grown to 53 files/872KB across 3 generations with the newest
  explicitly disclaiming the older two.
- **Actions:** `REC-002`'s suggestion (a `doctor`/CI-visible "commits since last tag"
  check); `REC-003`'s audit-corpus archival; extend CI to at least smoke-test `scripts/`.

---

## 16. Explicit answers to the three §77 questions

### §77.1 — "If we froze feature development for two weeks and invested exclusively in
simplification, which changes would produce the largest reduction in future
engineering cost while preserving the capabilities users actually rely on? Provide a
concrete ordered plan."

Ordered by complexity-removed-per-hour-invested, highest first. Nothing in this plan
touches a capability a user relies on — every item is R0-R2 (pure deletion, doc fix, or
mechanical rename) except items 6 and 7, which are gated behind an equivalence test
before anything is deleted.

1. **Days 1–2 — the proven-dead ledger, in one deletion pass.** 24 root-level `scripts/`
   duplicates (~4,600 LOC, `DEAD-03`), `hosts/` package (~500 LOC, zero production
   callers, `STR-001`), `frameworks/` package (9 files, zero production callers,
   `STR-004`), `cache/store.py`'s dead `SemanticCache` stub (~45 LOC, `PRV-01`),
   `context_signal.py` + its 3 test files (~145 LOC, self-admittedly dead, `STR-002`),
   the 3 git-tracked-despite-`.gitignore` stale `.claude/hooks/` files (~1,300 LOC,
   confirmed never registered, `RTE-001`/`CFG-002`), `routing.db` + its 2 doc
   references (`ST-01`), `scripts/pre-release-checklist.py` (`DEAD-06`),
   `intercept_bench.json` (`REC-006`). Combined: **~7,000+ LOC removed, zero
   behavioral risk**, because every one of these was proven unused by exhaustive grep
   in its own domain report. This is the single highest-leverage day of the two weeks.
2. **Day 3 — already-reproduced one-function correctness fixes.** `CFG-010`'s
   `sys.exit` wrapping for `last`/`retrospect`/`snapshot`; `DEAD-01`/`DEAD-02`'s
   `pyproject.toml` dependency declarations plus tightening
   `test_m11_declared_dependencies.py::_declared()`; `TST-01`'s six `__init__.py`
   additions (independently verified 17/17 pass, no regressions); `REC-001`'s
   `service.py` version-literal fix. All isolated, all have an existing or trivially
   added regression test.
3. **Days 4–5 — the two CRITICAL security fixes and the accounting trust fix.**
   Validate `openai_compat_base_url`/`llm_router_pxpipe_url` and stop forwarding the
   real provider key to a config-redirected `api_base` by default (`SEC-002`,
   `SEC-003`, `SEC-004`); apply `cost.production_only()`-style provenance filtering to
   `dashboard_data.query_window`'s three unfiltered tables (`ACC-01`). Small diffs in
   already-identified files, but this is the highest-leverage trust fix in the whole
   report — the headline "saved" number is currently ~3,534× its own honest accessor
   for the same data (§2).
4. **Day 6 — mechanical DELETE-over-REUSE merges.** `savings_log.jsonl`'s 6
   independently-redefined `_savings_log_path()` helpers → import
   `cost.savings_log_path()` (`OBS-01`, 3 of 6 already spot-checked identical); the two
   "free provider" set literals → one constant (`ACC-05`).
5. **Days 7–9 — the classification-engine merge, the largest future-cost reduction
   that isn't a pure deletion.** `hooks/auto-route.py`'s `SIGNALS` and `classify.py`'s
   `_SIGNALS` have provably diverged (3 categories differ, one whole category —
   `coordination` — is missing from the "canonical" side) despite the docstring's
   "backfilled VERBATIM" claim (`RTE-002`). This is root-caused as the reason routing
   behavior differs by *which entry point* a prompt arrives through — a live,
   ongoing source of future engineering cost every time either table is edited without
   the other. Gate: an equivalence test proving parity across historical decisions
   before the non-canonical table is deleted, the same bar `HOOK_LIVE_POLICY` already
   cleared at 1000/1000 agreement (`02_runtime_routing.md §13 item 2`).
6. **Days 10–12 — start, not finish, the `storage/` adoption ratchet.** Record
   `BASELINE = 63` (files bypassing `storage/` with raw `sqlite3`/`aiosqlite`,
   `STR-003`) as a count-down-only lint, matching this repo's own precedent
   (`scripts/lint_unknown_as_number.py`). Migrate the 5–10 highest-traffic files
   (`context.py`, `execution_ledger.py`, `budget.py`) within the freeze as proof of the
   pattern; the remaining ~50 are explicitly out of scope for two weeks and belong to
   the ratchet, not this sprint.
7. **Days 13–14 — pure package moves (lowest priority, cheapest, ranked last on
   purpose).** `budget.py`/`budget_backend*.py`/`budget_envelope.py`/`budget_key.py`/
   `budget_lineage_reconciliation.py`/`budget_store.py` → `budget/` package;
   `context.py`/`context_prep.py`/`context_optimizer.py`/`context_injection.py`/
   `code_context.py` → `context/` package. Zero logic change, pure import-path
   consolidation — real discoverability gain, but the smallest engineering-cost
   reduction per hour of any item in this plan, which is why it is scheduled last, not
   first.

### §77.2 — "Which pieces of llm-router would you delete if you personally became
responsible for maintaining this repository for the next five years? Every proposed
deletion must still be supported by evidence."

Each item keeps its deletion class per brief §1 (PROVEN DEAD / EFFECTIVELY DEAD /
LEGACY / UNCERTAIN — never silently promoted).

| Class | Item | Evidence | Action |
|---|---|---|---|
| **PROVEN DEAD** | `cache/store.py`'s `SemanticCache`/`SemanticCacheEntry` + `__init__.py` re-export | Zero callers anywhere in `src/`/`tests/` by exhaustive grep, confirmed independently by `13_verify_structure_docs.md` | Delete now |
| **PROVEN DEAD** | `context_signal.py` | Module's own docstring already states it's dead; ratchet-tracked by `test_l03_dead_public_api_ratchet.py` | Delete now, with its 3 dedicated test files |
| **PROVEN DEAD** | `.claude/hooks/auto-route.py`, `usage-refresh.py`, `version-guard.py` | Live check: not referenced by any `settings.json`/plugin manifest in the worktree; the hook Claude Code actually runs is a different, current file (`RTE-001`, confirmed by `13_verify_routing_config.md`) | Delete now |
| **PROVEN DEAD** | `routing.db` (data) + its references in `guide/TROUBLESHOOTING.md`/`guide/HOST_SUPPORT_MATRIX.md` | 0 rows in 12/13 tables; zero `src/` code references (`ST-01`) | Delete file, fix 2 doc lines |
| **PROVEN DEAD** | `savings_stats.mode` column + its migration | Fully documented, zero writers, zero readers, confirmed by SQL query against the live ledger (`ACC-04`) | Delete column + migration (check it isn't redundant with the `realized` gate first) |
| **LEGACY (superseded, dated root cause)** | 24 root-level `scripts/` duplicates | Single-commit reintroduction (2026-08-19) of files already relocated in an 2026-08-02 reorg; canonical copies in `scripts/{ci,release,bench,dev}/` are the ones still receiving fixes (`DEAD-03`) | Delete now |
| **LEGACY (superseded, broken pointer)** | `scripts/pre-release-checklist.py` | Uncalled, instructs the reader to run `scripts/release.py`, which does not exist; superseded by `scripts/release/pre-release-verify.sh` | Delete now |
| **EFFECTIVELY DEAD** | `hosts/` package (`HostAdapter` Protocol + `CursorAdapter`) | Zero `src/` references outside itself; `commands/install.py` (the real, shipping path) reimplements the identical job inline (`STR-001`) | Delete, unless a future host-adapter refactor of `install.py` is explicitly planned — pick one, don't keep both |
| **EFFECTIVELY DEAD** | `frameworks/` package (9 files) | Zero production importers; real Agno usage imports `integrations.agno` directly, not `frameworks.agno` (`STR-004`) | Delete or downgrade to a `docs/ROADMAP.md` entry |
| **EFFECTIVELY DEAD** | `LineageStore` dual-write system (~700 LOC: `lineage_store.py`, `decision_logger.py`, `hooks/lineage_integration.py`) | Call-graph proof of zero production callers to `.append()`/`.record()`, independent of session-boundary wipe behavior; the real data lives in `model_tracking.jsonl` instead, which the code's own comment mislabels "legacy" (`ST-02`, `ST-03`, confirmed with stronger evidence by `13_verify_storage_accounting.md`) | Delete the `LineageStore` path (not `model_tracking.py` — it's the one with real data) |
| **LEGACY (public PyPI API — deprecate, not delete outright)** | `cost.py`'s 8-function reporter cluster (`log_savings`, `format_spend_for_display`, `get_usage_summary`, `log_quota_snapshot`, `get_router_efficiency`, `get_classifier_overhead`, `get_cache_hit_stats`, `log_quality_trend`, `refresh_baseline_pricing_from_api`) + `trace_id.derive_trace_id` + `judge_cascade.should_judge_inline`/`should_cascade` | Zero `src/` callers confirmed independently of the existing ratchet test; public names on a package published to PyPI (`DEAD-05`/L-03) | Open a deprecation window before removal — a unilateral delete breaks downstream importers |
| **LEGACY (excluded from wheel, retain pending a product decision)** | `control_plane/api.py` + `reconciliation.py` (490 LOC) | Both do an unconditional `import` of a `control_plane.audit` module that does not exist in this distribution; already excluded from the wheel and ratcheted (`DEAD-04`/M-10) | I would force the decision within year one: either write `control_plane/audit.py` for this distribution or delete these two modules — carrying "excluded but present" for 5 years is worse than either choice |
| **LEGACY (archive, not delete)** | `audit/2026-09-21/` and `audit/2026-09-22/` (23 files, ~800KB) | The current audit's own `audit/README.md` already disclaims their numbers as wrong twice over (`REC-003`) | Squash each generation into one dated summary file; keep the raw detail in git history, not the live tree |
| **UNCERTAIN — I would resolve, not carry, within year one** | `feedback_handler.py`, `gateway_service.py`, `budget_lineage_reconciliation.py`, `commands/admin_actions.py`, `control_plane/store_postgres.py` | Zero-inbound in the import graph; flagged by 2 independent prior audit generations (2026-09-21, 2026-09-22) and now a 3rd time here, still unresolved (`REC-007`) | Run the runtime-coverage/dynamic-dispatch check none of the 3 audit passes had time for, then delete whatever proves dead — letting a 4th audit generation re-flag these unchanged would itself be a management failure, not a technical one |

### §77.3 — "What would you refuse to refactor because the current implementation is
already the simpler or safer design?"

- **`run_command`'s capability-over-containment trade-off.** A program-name allowlist
  cannot give both full local-model capability and real sandboxing — the current design
  picks capability, discloses the trade-off in `SECURITY.md`, and backs it with a named
  regression test (`tests/test_r3_allowlist_is_not_containment.py`). "Fixing" this into
  an argument-confined sandbox would be a materially larger, riskier change for a
  problem the maintainers have already reasoned through and disclosed honestly
  (`SEC-001`, confirmed disclosed-and-accepted by `13_verify_security_deps.md`). I would
  refuse to refactor it without an explicit, separate decision to trade away capability.
- **The single litellm adapter + `ProviderQuirk` Protocol** (`providers.py`,
  `provider_quirks.py`). Adding a litellm-supported provider today costs zero adapter
  code; a per-provider-adapter architecture (the brief's own default worst-case
  assumption) would be strictly more code for the same capability, with no evidence
  anywhere in this audit that the current design has failed to scale (`PRV-02`,
  `07_providers_perf.md §18`).
- **`secret_scrubber.py`'s regex-per-secret-shape design.** Underengineered in the
  abstract (§48's own framing), but its history — three independently-drifted copies,
  consolidated once into this one canonical module — argues for keeping one table, not
  replacing it with a fundamentally different (e.g. entropy-based) detector on the
  strength of a single narrow edge case (`SEC-007`, a 42-vs-40-character AWS-secret
  regex gap) that a one-line regex widening already fixes.
- **`sqlite_wal.py`'s explicit WAL + busy-timeout pattern.** Measured, not assumed —
  4/12 concurrent constructions failed before the fix, 66/2400 events were silently
  lost before a `PRAGMA`-return-value check was added (`PRV-02`). The fix is that more
  writers should *adopt* this pattern (only 3 of 9 sites currently do, per
  `13_verify_storage_accounting.md`'s PERF-01 correction), not that the pattern itself
  needs redesigning.
- **`env_registry.py`'s "check the check" AST-scanner design.** `CFG-007`'s gap
  (12+ indirectly-read vars invisible to the scanner) is a coverage gap in the existing
  mechanism — extend `_INDIRECT_READS` or teach the scanner one more AST pattern.
  Replacing the whole mechanism would discard the one part of this codebase's
  self-audit tooling that is independently implemented specifically to avoid the
  "validator checks itself" trap it's already been burned by twice.
- **`context_injection.py`'s single choke-point design.** Already the correct shape —
  one place, enforced by `test_okf_choke_point.py` — for a problem (cross-project
  contamination via `$HOME` cwd) that a more distributed "each caller remembers to
  scope itself" design already caused once and would risk reopening (`CTX-01`).
- **`install_manifest.py`'s per-write-operation manifest.** Explicitly built as the
  structural fix for a class of bug ("uninstall was assembled per-host and repeatedly
  missed subsets — every audit round found another gap") that a return to per-host
  special-cased uninstall logic would reopen (`02_runtime_routing.md` Flow H).
- **`failopen.py`'s design** (never-raises, unknown≠zero, unpersisted-loss counter).
  `ERR-01`'s gap is rollout breadth (≈8.6% of 1,020 broad excepts instrumented), not a
  flaw in the mechanism itself — the fix is more callers, not a different design.
- **The two-axis distinction between provenance filtering
  (`cost.production_only()`/`is_simulated`) and verified/unverified filtering
  (`savings.VERIFIED_SAVED_SQL`).** These answer different questions ("was this row
  real traffic" vs. "was this row's saving observed to replace a Claude turn") and
  `ACC-01`'s own consolidation-ledger entry is explicit that merging them into one flag
  would lose real information — the fix is applying both to the tables that currently
  have neither, not collapsing the distinction.
- **`_KNOWN_SUBCOMMANDS`'s deliberate non-authoritative scoping.** It is a typo-
  suggestion list, explicitly documented as such, with a worst-case blast radius of "a
  wrong typo suggestion," never a dispatch failure (`CFG-012`). Merging it into the
  dispatch chain to "have one list" would remove that honest, low-risk scoping for no
  behavioral benefit.
