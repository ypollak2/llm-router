# Domain 04 — Structure & Semantic Duplication

Auditor: 04 (Structure). Baseline: worktree `<worktree>`,
detached at `3c96d23`. All evidence below is read-only inspection of that worktree
(`find`/`wc`/`grep`/`head`/`sed`/`Read`, no execution beyond text search). No file
was modified. Finding IDs are prefixed `STR-`.

`~/.claude/CLAUDE.md` (git-ignored at `<repo>/CLAUDE.md`)
was read per instruction; its measurement-discipline rules (denominator errors,
"unknown must not be the favourable answer," ratchet tests over point fixes) are
non-structural but shaped how confidence is assigned below — no dead-code claim
here rests on absence of a direct import alone; every DEAD/EFFECTIVELY-DEAD
verdict is backed by a full-repo grep for the symbol, including tests, and where
the module's own docstring or an existing ratchet test already makes the claim,
that is cited and *cross-checked*, not merely repeated.

## Overview

The package is not "duplicated" in the copy-paste sense as often as it is
**fragmented under aliasing**: 189 of 415 `.py` files sit flat directly under
`src/llm_router/` (131,781 total LOC across 415 files), and a large fraction of
the flat namespace is single-concept modules that share a name-prefix with 2-8
siblings (`budget*`, `context*`, `classif*`, `feedback*`). Investigation this
session confirms three distinct patterns, not one:

1. **Genuine dead/stub code wearing a duplicate's name** (cache semantic-cache
   stub; the `decisions/`+`signals/` v0.0.1 mini-engine). Small in file count,
   high confidence, safe to delete.
2. **A designed abstraction with real alternate implementations that the
   production code path never calls** (`hosts.HostAdapter`, most of
   `frameworks/`). Not "duplicate code" — it's an abstraction whose concrete
   complexity was never removed from the call site it was built for, because a
   second, inline implementation grew up next to it instead. This is the
   costliest class: it looks like coverage but buys nothing.
3. **Intentional, documented forks that are a routing-safety liability, not a
   structure bug** (`classify.py` "single source of truth" vs
   `hooks/auto-route.py`'s hand-kept copy of the same tables — "Option B,
   left untouched"). Structurally these are fine (each file has one job); the
   risk is behavioral drift between two loci nobody two-way-syncs.

No single deletion here removes a majority of the mess — the fixes are a
handful of package moves/renames (§11) plus roughly six proven or
near-proven deletions (§49-style list in "top items for synthesis").

---

## §9 Semantic Duplication Map

| # | Family (brief's list) | Files | Canonical concept(s) | # impls | Verdict |
|---|---|---|---|---|---|
| 1 | `cache.py` vs `cache/` | `cache/__init__.py`(36), `cache/classification.py`(270), `cache/store.py`(45), **`semantic_cache.py`**(562, flat, outside `cache/`) | Two questions: "did we classify this exact prompt" (ClassificationCache) vs "did we already answer something semantically similar" (SemanticCache) | `ClassificationCache`=1 real impl (used); `SemanticCache`=**2 impls under one name**: `cache/store.py` (v0.0.1 stub, always-miss) and `semantic_cache.py` (full Ollama-embedding impl, 19 real callers) | **PROVEN DEAD**: `cache/store.py`'s `SemanticCache`/`SemanticCacheEntry` have zero callers anywhere outside `cache/__init__.py`'s own re-export (full-repo grep). The real semantic cache was built as an unrelated top-level module under the identical class name and never reconciled with the package that was supposed to own it. |
| 2 | `benchmark/` vs `benchmarks.py` | `benchmark/__init__.py`(172)+`regression.py`(318)+`runners/routerarena.py`(175) vs `benchmarks.py`(675)+`benchmark_fetcher.py`(627) | `benchmark/` = pluggable CLI runner/regression-detector protocol ("Plan 07 Cat G", `llm-router benchmark run <name>`); `benchmarks.py`+`benchmark_fetcher.py` = benchmark-*data-driven routing-table reordering* (a routing input, reorders `profiles.py` model chains) | Not duplicates — two unrelated concepts | **NOT DUPLICATE, MISNAMED**: the *directory* holds the operational/plural concept and the *singular filename* holds a different, also-plural-sounding concept. Naming is backwards from what either name suggests. Rename, don't merge. |
| 3 | `contract.py` vs `contracts.py` | `contract.py`(135, "Implicit routing contracts... enforced automatically within `route_and_call`", imported by `router.py`) vs `contracts.py`(285, "WS0... frozen migration contracts... ported from the upstream project... contains no behavior... nothing in the runtime imports this module yet") | `contract.py` = live per-call verification-gate object; `contracts.py` = a **frozen value-pin** of another codebase's (the upstream project's) contract shapes, kept for drift detection during a migration | `contracts.py` confirmed still zero production importers (`grep` over `src/`: none; only `tests/test_contracts.py`, `tests/test_identity_gate.py`) | **DUPLICATE NAME, NOT DUPLICATE PURPOSE, BUT LIVE RISK**: `contracts.py`'s `CapabilityDecision` dataclass is a byte-for-byte field copy of `capabilities.py`'s real `CapabilityDecision` (verified: `required`, `evidence`, `confidence`, `legacy_match` — identical). It is a manual pin with no automated equality check found between the two definitions in this session's search — if `capabilities.py`'s shape changes, `contracts.py` silently goes stale (WS0 says exactly this is the risk it exists to prevent, but nothing enforces it). Rename to something that doesn't collide with `contract.py` (e.g. `frozen_migration_contracts.py`) regardless of the drift question. |
| 4 | `dashboard/` vs `dashboard_data.py` | `dashboard/server.py`(1338)+`tui.py`(1009) vs `dashboard_data.py`(864, flat) | `dashboard_data.py` self-declares "Single source of truth for dashboard data queries," created after "~4 distinct drift bugs" from panels hand-rolling SQL | Intended: 1 query layer, N consumers | **NOT DUPLICATE BY DESIGN — ADOPTION UNVERIFIED**: did not get to trace whether `dashboard/server.py` and `dashboard/tui.py` actually call into `dashboard_data.py` for every panel, or partially bypass it the way `storage/` is bypassed (row 9). Given the project's own history of exactly this bug recurring, this needs a follow-up grep (`grep -n "import sqlite3\|cursor.execute" dashboard/server.py dashboard/tui.py` against `dashboard_data.query_*` call sites) before trusting the "single source of truth" claim. Flagged UNCERTAIN, not verified. |
| 5 | Classification components | `classify.py`(674), `classifier.py`(322), `semantic_classify.py`(478), `classification_allowlist.py`(137), `cache/classification.py`(270), plus `hooks/auto-route.py`'s own copy of `classify.py`'s tables | 4 distinct concepts sharing "classif*": (a) regex signal→task-type classifier (`classify.py`, self-titled "the single source of truth"), (b) LLM-based complexity-tier classifier (`classifier.py`), (c) embedding-based successor to (a) (`semantic_classify.py`, explicitly "the discriminative successor to the regex `_SIGNALS` engine in classify.py"), (d) per-tenant provider allow-list by task type (`classification_allowlist.py`) | (a) has **2 loci**: `classify.py` + a hand-kept duplicate table in `hooks/auto-route.py` (documented: "Option B, since the hook routes live sessions and was left untouched") | **INTENTIONAL FORK, ROUTING-SAFETY RISK, NOT A NAMING ACCIDENT**: (a)/(b)/(c)/(d) are 4 real concepts correctly separated by *purpose*, but (a)'s "single source of truth" claim is false while `hooks/auto-route.py` keeps an independent, manually-synced copy — two classification tables that can silently diverge (matches the repo's own S8 finding in CLAUDE.md about a classifier's fall-through default deciding ~50% of traffic). (c) further complicates: it exists to *replace* (a) but nothing here confirms whether it has replaced (a) in `router.py`/`gateway.py`, only in name. Needs a §13 routing-engine pass, flagged here as a structure finding because it means "the classifier" has no single answer in this codebase. |
| 6 | Context prep/optimization/signals | `context.py`(747), `context_prep.py`(150), `context_optimizer.py`(176), `context_injection.py`(163), `context_signal.py`(145), `code_context.py`(491) | 6 distinct concepts: session buffer+SQLite summaries; token-budget-aware prompt assembly; 2-stage compression; repo-knowledge injection; "is this prompt context-dependent" signal; AST code-context extraction | 5 live + 1 dead | **NOT DUPLICATE LOGIC, FRAGMENTED NAMING + ONE CONFIRMED-DEAD MEMBER + ONE ADMITTED GAP**: `context_signal.py` is dead (see STR-002 below). `context_injection.py`'s own docstring: repo knowledge reaches only 2 of 7 execution paths ("router.py" and "direct_executor.execute_chain"); the tool loop, Codex agent, Claude agent, Gemini agent, and `llm_local_task` run with **no repo knowledge at all** — "not a policy anyone chose." This is a real, user-visible inconsistency, not a structure nicety: which execution path you hit determines whether the model sees your repo. |
| 7 | Execution signals/ledgers | `signals/`(base.py, keyword.py, pii.py), `decisions/engine.py`, `execution_signal.py`, `operational_signal.py`, `attempt_log.py`, `execution_ledger.py` | `signals/`+`decisions/engine.py` = a **separate v0.0.1 "first-match boolean composition over signal scores" mini decision-engine** ("v0.0.2 will add the YAML loader…") that `router.py` (5,359 lines, the actual routing entry point) does not import at all (confirmed: no `signals`/`decisions` import in `router.py`'s import block); `execution_signal.py`/`operational_signal.py` are a mirrored pair (`operational_signal.py`'s docstring: "opposite bias from `context_signal`... Mirrors `context_signal.py`" — i.e. it mirrors a module that is itself dead, see STR-002); `attempt_log.py`/`execution_ledger.py` are the real per-attempt persistence used by the routing path | `decisions/`+`signals/` = scaffold with test-only + self-referential callers (`tests/test_decision_boosts.py`, `tests/scenarios/*`) | **EFFECTIVELY DEAD PARALLEL DECISION ENGINE**: `decisions/engine.py` + `signals/` package (6 files total) implement a second, unfinished routing-decision mechanism that the real router does not call. Not proven fully dead (test-only callers exist and could be exercising something a later stage reads indirectly — not traced this session), but a caller-graph dead end from `router.py`'s own imports is strong circumstantial evidence. Recommend a targeted trace before deletion (§1 rule: no silent UNCERTAIN→DEAD). |
| 8 | Feedback | `feedback.py`(319, real-time streaming/ETA/timeline events), `feedback_handler.py`(249, orchestrates 1-3 with CLI display), `quality_feedback.py`(478, post-route quality scoring feeding back into model selection) | 3 distinct pipeline stages (during-call UX, CLI display orchestration, post-call quality scoring) | 3, not duplicates | **NOT DUPLICATE**: correctly staged, but the name "feedback" now covers three unrelated audiences (end-user UX, CLI renderer, routing-quality signal) with no package boundary — same terminology-collision pattern as "budget" (row 9) and "context" (row 6). |
| 9 | Budget | `budget.py`(477), `budget_backend.py`(861), `budget_backend_postgres.py`(488), `budget_envelope.py`(398), `budget_key.py`(120), `budget_lineage_reconciliation.py`(114), `budget_store.py`(77), `provider_budget.py`(253), `token_budget.py`(375) | 3 unrelated meanings of "budget": (i) cost-cap/quota accounting lineage — `budget.py`("Budget Oracle" pressure), `budget_backend*.py`(`BudgetBackend` Protocol, 2 real impls: Sqlite + optional Postgres), `budget_envelope.py`(per-identity caps + parent/child propagation), `budget_key.py`(key shape), `budget_lineage_reconciliation.py`(ledger-invariant check), `budget_store.py`(now "delegates to StorageService"); (ii) per-provider external-$ spend limits — `provider_budget.py`; (iii) per-request context-window token allocation — `token_budget.py` | (i) is one coherent, incrementally task-ID-tagged subsystem (T2-M1→T2-XL1, #70) — NOT accidental duplication; `budget_backend_postgres.py` confirmed live (imported conditionally by `budget_backend.py` behind a Postgres-extra guard, plus `env_registry.py`), not dead despite its own "EXPERIMENTAL" label | **TERMINOLOGY COLLISION, NOT DUPLICATION**: (i) should become a `budget/` package (6 files, one coherent subsystem). (ii) and (iii) are legitimate, different concepts that should be renamed to stop reading as part of (i) — `provider_budget.py`→something like `provider_spend_limits.py`, `token_budget.py`→`token_allocation.py` or moved next to the `context_prep.py` family it actually serves. |
| 10 | Usage/accounting | `claude_usage.py`(513, live claude.ai API scrape via browser), `claude_jsonl_usage.py`(123, reads local `~/.claude/projects/**/*.jsonl`), `attribution.py`(230, self-titled "Canonical routing attribution — one definition, consumed by every surface," created because "two product surfaces reported contradictory answers... read different tables and applied different rules") | 3 distinct sources: live web scrape, local JSONL parse, canonical cross-table attribution | `attribution.py` is itself a consolidation done in response to a prior duplication bug | **NOT DUPLICATE — THIS IS THE PATTERN TO COPY**: `attribution.py`'s docstring is a template for how `dashboard_data.py` (row 4) and `budget` (row 9) should each also declare one canonical query surface. Its existence, and its stated reason for existing, should be cited as the precedent when recommending consolidation elsewhere. |
| 11 | Monitoring vs observability | `monitoring/`(`live_tracker.py`= hourly session snapshots + routing-accuracy trend; `periodic.py`) vs `observability/`(`core.py`=OpenTelemetry span/metric exporter; `summary.py`; `surface_status.py`) | In-house accuracy-trend tracking vs OTEL export | Not inspected for overlap beyond `__init__`/docstrings (time-boxed) | **LIKELY NOT DUPLICATE, UNVERIFIED**: names read as cleanly split by audience (internal analytics vs external telemetry export) but `observability/summary.py`/`surface_status.py` vs `monitoring/periodic.py` were not diffed for a competing "is routing healthy" signal. Flagged UNCERTAIN — do not merge without that check. |
| 12 | Memory vs knowledge vs context | `memory/profiles.py`(cross-session user profile), `context.py`(session buffer+summaries), no dedicated `knowledge/` package — "knowledge" lives inside `context_injection.py`'s "OKF" (repo knowledge) concept | 3 different scopes: durable user profile, session-scoped conversation memory, per-call repo/project knowledge | Correctly separated by scope | **NOT DUPLICATE, TERMINOLOGY GAP**: "knowledge" (OKF) has no package of its own — it is one function inside `context_injection.py`. Given the brief's explicit interest in OKF (§15), this is a location smell more than a duplication: the thing the brief keeps asking about by name doesn't have a module by that name. |
| 13 | Agents vs agentic | `agents/`(base.py, budget.py, registry.py, session.py — 5 files) vs `agentic/`(acceptance.py, adapters.py, delegate.py, engine.py, ledger.py, planner.py, react.py, savings.py, service.py, telemetry.py, worktree.py — 12 files) vs flat `agentic_registry.py`(Ollama tool-calling capability prober, a *third*, unrelated "registry") | `agents/` reads as host-facing agent session/registry (thin); `agentic/` is the actual direct-execution engine (ReAct loop, worktree isolation, acceptance/verification, savings accounting); `agentic_registry.py` is neither — it probes which local models can reliably tool-call | Not fully traced this session (caller graph between `agents/` and `agentic/` not confirmed) | **NAMING COLLISION, LIKELY NOT LOGIC DUPLICATION**: `agents/registry.py` and `agentic_registry.py` sharing "registry" while meaning different things (agent-session registry vs model-capability registry) is a concrete case of the brief's "generic names" warning (§11) even though the underlying logic is plausibly non-duplicative. Recommend renaming `agentic_registry.py` → `tool_calling_capability_registry.py` or moving it under `agentic/` as `agentic/model_capability.py` to stop it reading as a third agent registry. |
| 14 | Policy vs rules vs gates | `policy.py`, `policy_runtime.py`, `policy_diff.py`, `org_policy.py`, `user_routing_policy.py`, `cli_init_policy.py`, `gates.py`, `reason_gate.py` (code) **vs** `src/llm_router/policies/`(6 YAML preset files: conservative/aggressive/balanced/standard/cost_aggressive/routerarena_tuned — no code) **vs** `src/llm_router/rules/`(13 Markdown per-IDE routing-rule templates — cursor-rules.md, gemini-rules.md, etc. — no code) | Routing-policy logic (code) is completely unrelated to the two top-level directories that share its vocabulary | 0 overlap in content, 100% overlap in name | **STR-005 — WORST NAMING COLLISION IN THE TREE**: a maintainer grepping for "policy" or "rules" at the package level finds a YAML-preset directory and a Markdown-template directory respectively, neither of which contains the actual policy/gate code (`policy.py`, `gates.py`, `reason_gate.py`, `org_policy.py` all live flat at package root, unrelated to `policies/`). Zero duplication risk (different artifact types), 100% discoverability cost. Rename `policies/`→`policy_presets/` and `rules/`→`host_rule_templates/` (or move under `docs/`/`assets/`), independent of any other consolidation. |
| 15 | Capabilities vs classification | `capabilities.py`(`CapabilityRequirement`, `CapabilityDecision`, `RelevantContext`/`RelevantFile`) vs `classify.py`/`classifier.py`/`semantic_classify.py` (row 5) | Capability derivation (what tools/output-shape a task needs) is a documented downstream consumer of classification, not a duplicate of it | Correctly separated | **NOT DUPLICATE.** Only issue is the `CapabilityDecision` name collision with `contracts.py` already covered in row 3. |
| 16 | Host modules vs integrations | `hosts/`(base.py=`HostAdapter` Protocol, cursor.py=`CursorAdapter`, events.py, gemini_cli.py, hook_io.py — 6 files) vs `integrations/`(agno.py, helicone.py, litellm_budget.py — 4 files) vs flat `claude_agent.py`/`codex_agent.py`/`codex_host.py`/`gemini_cli_agent.py`/`host_detect.py` | `hosts/` = a designed Protocol-based host-adapter abstraction with concrete `CursorAdapter`; `integrations/` = third-party platform bridges (Helicone tracing, LiteLLM budget, Agno framework); flat files = the actual per-host agent-execution + detection code that ships | `hosts/` has **zero production callers** | **STR-001 (top finding, see §10) — the abstraction exists, the real code bypasses it.** `commands/install.py` (the real, shipping host-installation flow, 1,400+ lines) writes every host's MCP config (`~/.cursor/mcp.json`, Copilot, OpenClaw, VS Code, Trae, Gemini CLI) via hand-rolled inline JSON merge functions (`_merge_json_mcp_block`, hardcoded `json_targets.append(...)` path lists) and never references `hosts.CursorAdapter`, `hosts.HostAdapter`, or any other symbol from the `hosts/` package. Full-repo grep for `llm_router.hosts` outside `hosts/` itself: zero matches in `src/`; only test files import it. `host_detect.py` (flat, "which agent hosts are installed," used in production) is a *third*, independent implementation of "know about a host," coexisting with `hosts/`'s `_HOSTS`-style tables and doing a similar job with none of the same code. |
| 17 | Commands vs CLI helpers | `commands/`(44 files, one per subcommand: `budget.py`, `doctor.py`, `install.py`, `savings_report.py`, etc.) vs flat `cli.py`(entry point, `console_scripts` → `llm_router.cli:main`), `cli_help.py`, `cli_init_memory.py`, `cli_init_policy.py` | `cli.py` is the dispatch entry (`[project.scripts] llm-router = "llm_router.cli:main"`); `commands/*.py` are the per-subcommand implementations `cli.py` presumably dispatches to; `cli_init_*.py` are one-shot init helpers | Not fully traced (did not confirm `cli.py`'s dispatch table names every `commands/*.py` module, nor whether any `scripts/*.py` — 106 files — duplicates a `commands/*.py` subcommand) | **UNCERTAIN, FLAGGED FOR FOLLOW-UP**: 106 files in `scripts/` (63 of which contain their own `def main`/`argparse`) is a second command surface outside the packaged CLI; whether any of those 63 duplicate a `commands/*.py` subcommand (e.g. a `scripts/routing_rate.py` the repo's own CLAUDE.md tells you to use "do not write another ad-hoc parser" — suggesting this has happened before) was not checked this session. Recommend domain 01 (Recon) or 08 (Dead code/scripts) confirm; noting here because it is squarely a §11 boundary question (packaged CLI surface vs maintenance-script surface). |
| 18 | Storage vs direct SQLite | `storage/`(`service.py`, `models.py`, `adapters/{base,sqlite,yaml,json}.py`, `routing/validators.py` — 10 files, `StorageAdapter` Protocol with 3 real concrete impls) | `storage/service.py` is documented (via `budget_store.py`'s docstring) as the abstraction that "enforces atomicity, error recovery" for all file/DB I/O | **63 files** outside `storage/` import `sqlite3`/`aiosqlite` directly | **STR-003 (top finding) — the abstraction has ~2% adoption.** Only `budget_store.py` (and whatever else routes through `StorageService` — not exhaustively enumerated) uses the layer; 63 other modules including core routing-path code (`context.py`, `feedback.py`, `execution_ledger.py`, `budget.py`, `budget_backend.py`, `attribution.py`, `quota_tracker.py`, `cost.py`, `telemetry.py`, most of `commands/` and `hooks/`, `agentic/telemetry.py`, `agents/session.py`, `semantic/store.py`, `control_plane/store.py`, `lineage/lineage_store.py`) each hand-roll their own `sqlite3`/`aiosqlite` connection/cursor/transaction handling. This is the single largest concrete driver of "the same fact persisted twice, inconsistent atomicity guarantees, migration risk" that later brief sections (§16, §35) will need to reason about — it belongs to structure because it is fundamentally a package-boundary failure: `storage/` was never made the *only* door to SQLite, so it isn't one. |

---

## §10 Abstraction audit

Format: Current chain A→B→C→D (what actually happens) vs Potential A→C
(what a same-behavior simplification would look like), and what B/D bought.

### STR-001 — `hosts.HostAdapter` Protocol (biggest single ROI-negative abstraction found)
- **Current**: A (`commands/install.py` runs) → B (*should* call*) `hosts.CursorAdapter.install()` / a `HostAdapter`-conforming object per host → C (host's MCP config file written) → D (uninstall mirrors the same adapter).
- **Actual**: A (`commands/install.py` runs) → **C directly**: inline `_merge_json_mcp_block()` + a hardcoded `json_targets` list covering Cursor, Copilot CLI, OpenClaw, VS Code, Trae, Gemini CLI, each with its own literal path and root-key string, inside one 1,400-line file. `hosts/` (`base.py`+`cursor.py`+`events.py`+`gemini_cli.py`+`hook_io.py`, 6 files, a real `Protocol` and a real `@dataclass CursorAdapter`) is never called.
- **What B was supposed to remove**: per-host config-writing boilerplate, via one substitution point (`HostAdapter`) that new hosts implement once.
- **What B actually removes today**: nothing — the boilerplate it was designed to remove still exists, verbatim, in `install.py`, uncalled-through the adapter.
- **Potential A→C**: either (a) delete `hosts/` and accept `install.py`'s inline approach as the real design, or (b) refactor `install.py` to actually call `hosts/` adapters and delete the duplicate inline logic — but NOT both left standing. Recommend (a) DELETE unless a host-adapter refactor of `install.py` is already planned elsewhere in this audit's remediation phase (check domain 08/49 deletion ledger before finalizing — I did not check whether another auditor's brief section depends on `hosts/` surviving).

### STR-004 — `frameworks/` package (`FrameworkAdapter` Protocol, 9 files)
- **Current**: A (agent-framework author wants routed calls) → B (`frameworks.<name>` adapter) → C (`llm_router.router`).
- **Actual production traffic**: A (real Agno users) → C directly via `llm_router.integrations.agno.RouteredModel`/`RouteredTeam` — confirmed the only real production import path (`tests/test_agno_integration.py`, `tests/qa/test_agno_deep.py` both import `llm_router.integrations.agno`, not `llm_router.frameworks.agno`). `frameworks/agno.py` is a **re-export shim over `integrations/agno.py`** ("re-exports from llm_router.integrations.agno... adds the FrameworkAdapter shim for future consistency"), and it is the *only* one of the 7 named framework adapters (`agno`, `hermes`, `langgraph`, `crewai`, `openai_agents`, `claude_agent_sdk`, `pydantic_ai`) that re-exports anything real — the package's own docstring lists the rest as "skeleton," "stub," or "PR welcome."
- **What B contributes**: a unified import path (`llm_router.frameworks.*`) that zero production code uses, plus 6 empty scaffolds.
- **Verdict**: EXPERIMENTAL/ASPIRATIONAL, correctly classified in §7 as such — not a structure bug so much as a package that shouldn't exist yet, or should be a single `frameworks.md` roadmap doc until a second adapter goes from stub to concrete.

### STR-006 — `storage.StorageAdapter` Protocol (well-designed, badly adopted)
- **Current (designed)**: A (any subsystem needing persistence) → B (`StorageService`, choosing among `SqliteAdapter`/`YamlAdapter`/`JsonAdapter` via the `StorageAdapter[T]` Protocol) → C (atomic write + error recovery).
- **Actual**: A → C directly, 63 times over, via raw `sqlite3`/`aiosqlite`. B has 3 legitimate concrete implementations (unlike `hosts/`'s zero) — the Protocol itself is well-shaped; the problem is 100% adoption, not design. This is the inverse of STR-001: here B is worth keeping, the fix is migrating callers *into* it, not deleting it.

### STR-007 — `decisions.engine` + `signals/` (mini decision-engine, v0.0.1)
- **Current (designed)**: A (a "signal score") → B (`decisions.engine`'s "first-match boolean composition") → C (a routing bias).
- **Actual routing path**: `router.py` (5,359 lines) does not import `llm_router.signals` or `llm_router.decisions` anywhere in its top-level imports. The only non-test importer of `decisions/` is `signals/__init__.py` itself (self-referential). This reads as a parallel, never-finished decision mechanism ("v0.0.2 will add the YAML loader and AND/OR/NOT operator nodes" — never happened by this commit) sitting beside the real routing logic in `classify.py`/`gates.py`/`contract.py`. UNCERTAIN pending a full call-graph trace (not: PROVEN DEAD) because test-only usage across 4 test files could still be exercising a code path this session didn't trace fully (e.g. via `pytest` fixtures that patch router internals). Flag for domain 06 (Routing engine) to confirm with a runtime trace.

### Well-designed abstractions found (KEEP, cited for balance — not every Protocol here is a smell)
- `ProviderQuirk` (`provider_quirks.py`) — `Protocol` + `IdentityQuirk` default + 5 named concrete quirks (`OpenAIReasoningQuirks`, `OllamaQuirks`, `OpenRouterQuirks`, `OpenAICompatQuirks`, `AnthropicPxpipeQuirk`), each solving a real, cited, provider-specific bug (D.1–D.3 in its own docstring). Real substitution point, real callers, identity-by-default so it costs nothing when unused. **Textbook correct use of a Protocol.**
- `BudgetBackend` Protocol — 2 real, both-wired implementations (`SqliteBudgetBackend` default, `PostgresBudgetBackend` behind an extras-guarded conditional import in `budget_backend.py:801-805`). Legitimate multi-deployment substitution point.
- `ControlPlaneStore` — sqlite + postgres impls (`control_plane/store.py`, `control_plane/store_postgres.py`), consumed by `commands/cp.py`. Same shape as BudgetBackend, consistent pattern, fine.
- `Signal` Protocol (`signals/base.py`) — 2 concrete signals (`keyword.py`, `pii.py`); small but real (caveat: this lives inside the STR-007 mini-engine whose overall wiring into `router.py` is unconfirmed — the Protocol design itself is fine independent of that).

---

## §11 Package structure

### Generic/misleading top-level names (evidence-based, not vibes)
| Name | Contents | Problem |
|---|---|---|
| `policies/` | 6 YAML preset files, zero code | Shares the name of the *actual* policy code (`policy.py`, `policy_runtime.py`, `org_policy.py`, `gates.py`), which lives flat, unrelated. |
| `rules/` | 13 Markdown per-IDE templates, zero code | Shares the name-space with routing "rules"/"gates" vocabulary used throughout the code (`gates.py`, `reason_gate.py`) with zero actual overlap. |
| `library/` | 6 files, not inspected in depth this session | Classic generic-utility-package name flagged by brief §11 as a pattern to check; deferred — recommend a follow-up read of `library/__init__.py`'s exports before the target tree is finalized. |
| `tools/` | 17 files — likely MCP tool implementations (brief's `tools` warning is about generic-utility dumping grounds; this looks purpose-specific from the file count/domain, not generic, but not fully confirmed this session) | Deferred — confirm each file maps 1:1 to an MCP tool registration (domain 07/29's job) rather than being a generic-helpers dump. |

### Confirmed cross-package import isolation (evidence: grep for `from llm_router.<pkg>` outside each package)
`frameworks/` and `hosts/` are the only two top-level packages with **zero** production (`src/`) importers outside themselves — every other package has at least one real cross-package caller. This corroborates STR-001 and STR-004: these two packages are structurally isolated from the rest of the tree, which is itself diagnostic (a package nothing calls is either dead, aspirational, or wired through a mechanism this grep can't see — checked for both via install.py/registry inspection above, and confirmed genuinely uncalled, not dynamically loaded).

### Target package tree (proposed, minimal-change)

```
src/llm_router/
  budget/                 # NEW: consolidates budget.py, budget_backend*.py,
                           #   budget_envelope.py, budget_key.py,
                           #   budget_lineage_reconciliation.py, budget_store.py
                           #   (one coherent T2-* lineage, currently 9 flat files
                           #   sharing a prefix with 2 unrelated concepts — see §9 row 9)
  context/                 # NEW: context.py→session.py, context_prep.py→prep.py,
                           #   context_optimizer.py→compress.py,
                           #   context_injection.py→knowledge.py, code_context.py→code.py
                           #   (context_signal.py DELETED, see STR-002)
  policy_presets/           # RENAMED from policies/ (YAML only, no code change)
  host_rule_templates/      # RENAMED from rules/ (Markdown only, no code change)
  hosts/                    # DELETE (STR-001) unless install.py is refactored to use it —
                           #   pick one, do not keep both
  frameworks/               # DELETE or freeze-as-doc (STR-004) until a 2nd real adapter ships
  cache/                    # KEEP classification.py; DELETE store.py (STR-002-class dead stub)
  decisions/ + signals/     # PENDING §13 trace (STR-007) — DELETE if router.py confirmed
                           #   never to reach them at runtime, else document why it's parallel
  <everything else unchanged># commands/, tools/, semantic/, agentic/, agents/, storage/, etc. —
                           #   no evidence this session justifies restructuring them
```

**Permitted/forbidden dependency rule to add** (didn't exist before, evidenced by
the `storage/` bypass, STR-003): any module writing SQLite must import
`storage.service` or `storage.adapters.sqlite_adapter` — direct `import sqlite3`
/`import aiosqlite` outside `storage/` should be a lint-enforced violation
(the repo already has this taste — `scripts/lint_unknown_as_number.py` is exactly
this pattern of ratchet-lint applied elsewhere per `CLAUDE.md`; the same mechanism
applies directly here: a `BASELINE = 63` ratchet-down lint, not a flag day).

---

## §12 Domain model

| Canonical concept | Type(s) found | Location(s) | Divergence |
|---|---|---|---|
| Routing decision (a completed routing choice) | `RoutingDecision` | `model_tracking.py:32` (persistence record: 13 fields incl. `classification_method`, `chain_position`, `quota_pressure`, `quality_feedback`) **and** `terminal_style.py:95` (UI value object: 7 fields incl. `format_hud()` method, no `timestamp`/`chain_position`/persistence fields at all) | **Two classes, same name, disjoint field sets, disjoint purposes** (log record vs display formatter). `grep -rn "class RoutingDecision"` returns both with no shared base, no conversion function found between them — a caller must know which import path it means. |
| Capability decision (does this task need X capability) | `CapabilityDecision` | `capabilities.py:64` (live, real: `required`, `evidence`, `confidence`, `legacy_match`) **and** `contracts.py:222` (frozen pin, field-identical copy, "nothing in the runtime imports this module yet") | Not a logic fork (identical shape today) but a **silent-drift risk**: no test found in this session's search that asserts the two stay equal; if `capabilities.py` changes its dataclass, `contracts.py` has no mechanism to notice. |
| Provider spend/limit ("budget") | `BudgetState`(`budget.py`), `BudgetEnvelope`(`budget_envelope.py`), `BudgetKey`(`budget_key.py`), plus an unrelated per-provider `$`-limit record in `provider_budget.py`, plus an unrelated token-count record in `token_budget.py` | 5 files, 3 semantically different "budget" objects | Not dict-soup (each is a real dataclass), but the *name* "budget" alone cannot disambiguate object identity without opening the file — a `grep -rn "budget"` finding tool would surface 9 files for what is really 3 questions. |
| Dict/dataclass conversion churn | 79 files define at least one `@dataclass`; 9 files define `to_dict`/`from_dict`/use `asdict()` | Not itself alarming at this scale for a 415-file package, but combined with the 63-file SQLite bypass (§9 row 18, each hand-rolling its own row↔dict↔dataclass mapping) this is very likely where "same fact, different shape in 3 places" bugs like the ones `attribution.py`'s docstring already documents (30-day dashboard vs `routing_decisions` table contradiction) come from. Not independently re-verified this session beyond the `attribution.py` precedent — flagged for domain 07 (product core)/16 (storage) to quantify. |

---

## §40 Terminology table

| Term as used | Meaning A | Meaning B | Meaning C | Canonical recommendation |
|---|---|---|---|---|
| classify / classifier | Regex signal→task-type engine (`classify.py`) | LLM-based complexity-tier engine (`classifier.py`) | Embedding-based successor to A (`semantic_classify.py`) | Rename B → `complexity_tier.py` to stop the `classify`/`classifier` near-homograph; keep "classify" for the task-type family only. |
| budget | Cost-cap/quota accounting (T2-* lineage, 6 files) | Per-provider external-$ spend limit (`provider_budget.py`) | Per-request token-window allocation (`token_budget.py`) | Reserve "budget" for the T2-* cost-cap lineage only (move into `budget/` package); rename B and C. |
| context | Session buffer + summaries (`context.py`) | Token-budget prompt assembly (`context_prep.py`) | Repo/OKF knowledge injection (`context_injection.py`) | Keep as one `context/` package (proposed above) with the prefix dropped per-file; the shared prefix is fine *inside* a package, the problem today is it's the only thing distinguishing 5 flat files. |
| policy / rules / gates | Routing-decision logic code (`policy.py`, `gates.py`, `reason_gate.py`, `org_policy.py`) | YAML preset bundles (`policies/` dir) | Per-IDE Markdown templates (`rules/` dir) | Rename the two non-code directories (see §11); reserve "policy"/"gate" vocabulary for code. |
| registry | Agent-session registry (`agents/registry.py`) | Local-model tool-calling capability prober (`agentic_registry.py`) | Benchmark plug-in registry (`benchmark/__init__.py`'s `BenchmarkRunner` table) | Fine as a generic pattern name *within* each package; the collision is only acute for `agentic_registry.py` sitting flat next to `agents/registry.py` under near-identical names for unrelated things — move or rename per STR row 13. |
| contract(s) | Live per-call verification-gate object (`contract.py`) | Frozen migration-era value pin, ported-from-another-repo (`contracts.py`) | — | Rename `contracts.py` (see §9 row 3). |
| benchmark(s) | Operational CLI runner/regression-detector (`benchmark/`) | Routing-table reordering by historical score (`benchmarks.py`+`benchmark_fetcher.py`) | — | Rename per §9 row 2 — swap which one gets the plural. |
| monitoring / observability | In-house accuracy-trend tracking (`monitoring/`) | OTEL span/metric export (`observability/`) | — | Acceptable as-is pending the unverified-overlap check in §9 row 11. |

---

## §47 Overengineering questions — answered against evidence found

- **"Does this need a Protocol, or would a function work?"** `hosts.HostAdapter` is the clean negative answer: designed as a Protocol with one concrete implementation the real caller never invokes. If `install.py`'s inline approach is kept (recommended, since it's the one that ships), the Protocol should be deleted, not "used more."
- **"One typed model or dicts?"** `RoutingDecision` exists as two unrelated typed models under one name (§12) — the problem here isn't dicts, it's that typing didn't prevent the collision; a rename fixes it, not more typing.
- **"One provider registry?"** `provider_registry.py`(342) exists; a 5-file sample grep for `"openrouter"` still found provider-specific knowledge duplicated across `config.py`, `router.py`, `provider_quirks.py`, `tiers.py`, `commands/doctor.py` — consistent with brief §18's expected finding, not independently resolved here (domain 06/18's job) but corroborating evidence supplied.
- **"One persistence layer?"** No — see STR-003. This is the single clearest "should be one, isn't" finding in the whole domain-04 sweep.
- **"Delete instead of redesign?"** Applies directly to `hosts/`, `frameworks/`, `cache/store.py`, and (pending confirmation) `decisions/`+`signals/` — none of these need a better abstraction, they need either deletion or their existing callers rewired to actually use them.

---

## §50 Consolidation ledger

| Concepts | Current impls | Canonical | Deleted concepts | Risk |
|---|---|---|---|---|
| Semantic response cache | `cache/store.py::SemanticCache` (dead stub) + `semantic_cache.py::SemanticCache` (real) | `semantic_cache.py` | `cache/store.py`, and the `SemanticCache`/`SemanticCacheEntry` re-exports in `cache/__init__.py` | LOW — zero confirmed callers of the stub; verify no doc references `cache.SemanticCache` import path before deleting the re-export. |
| Host installation | `hosts/` Protocol+adapters (uncalled) + `commands/install.py` inline logic (real) | `commands/install.py`'s inline approach | `hosts/` package (6 files) | LOW-MEDIUM — confirm no other in-flight audit workstream or remediation phase plans to route `install.py` through `hosts/` instead (check before deleting; if the target architecture *wants* the adapter pattern, refactor `install.py` into it instead of deleting `hosts/` — either resolution is fine, leaving both is not). |
| Framework adapters | `integrations/agno.py` (real) + `frameworks/*` (mostly stubs, agno.py a re-export shim) | `integrations/agno.py`, until a second framework adapter is actually implemented | `frameworks/` package (9 files) or downgrade to a roadmap doc | LOW — zero production callers found. |
| "Is this prompt context-dependent" signal | `context_signal.py` (self-declared dead, ratchet-tracked) + `hooks/auto-route.py::_is_context_dependent` (the live one) | `hooks/auto-route.py`'s implementation | `context_signal.py`, and its mirror `operational_signal.py` should be re-justified independently (it mirrors a dead module's *bias*, not its code — confirm it doesn't also need retirement) | LOW — already has a ratchet test (`tests/test_l03_dead_public_api_ratchet.py`) acknowledging it; deletion is a cleanup of already-known debt, not a new discovery. |
| Budget/cost-cap subsystem | 6 flat files (`budget.py`, `budget_backend.py`, `budget_backend_postgres.py`, `budget_envelope.py`, `budget_key.py`, `budget_lineage_reconciliation.py`) + `budget_store.py` | New `budget/` package, same files renamed-in-place | None — pure move, zero logic change | LOW — mechanical package move; update imports only. |
| Direct SQLite access | 63 files | `storage/service.py` + `storage/adapters/*` | None — this is a migration, not a deletion; each of the 63 call sites needs individual review | HIGH effort, LOW-per-site risk if done incrementally with the existing `StorageAdapter` Protocol (already correctly designed) — recommend ratchet-lint (baseline=63, count-down) rather than a flag-day migration, matching this repo's own established pattern for exactly this kind of "big number, incremental fix" problem. |

---

## §52 Fitness scenarios / §71-72 new-feature & removal file counts

Counted as: files that *would need to change* to make the addition correctly
wired end-to-end, based on the call graph traced this session (provider/host
counts corroborated by direct grep of a real existing provider/host; not a
guess).

| Scenario | Files touched today (evidence) | Why |
|---|---|---|
| Add a provider with tools + structured output | ≥5: `provider_registry.py`, `provider_quirks.py` (if it needs a quirk), `config.py`, `tiers.py`, `pricing.py`, plus README/doc surfaces (out of scope for this domain) — based on the 5-file spread found for the existing `openrouter` provider via grep, excluding tests/docs | Provider metadata has no single source of truth (§9 row 9's counterpart problem for providers, corroborating brief §18 — not independently re-solved here). |
| Add a host (coding-tool integration) | 1 file in practice (`commands/install.py`, adding another `json_targets.append(...)` line/branch) **but architecturally should be 1** (`hosts/<newhost>.py` + registration) — the fact that the real number and the "intended" number are both 1 but via *completely different mechanisms* is itself the STR-001 finding restated as a fitness test: two valid single-file answers to the same question, only one of which is exercised. | Confirmed via `install.py`'s structure (a linear list of per-host blocks) vs `hosts/`'s Protocol (never invoked). |
| Add a usage metric | ≥2-3: wherever `dashboard_data.py`'s query functions live (1 file, since it's the declared single source of truth) + the underlying table/schema it reads from (likely `migrations/versions/` for a new column, 1 file) + the dashboard UI surface consuming it (`dashboard/server.py` or `tui.py`) — NOT independently traced end-to-end this session; based on `dashboard_data.py`'s own stated design intent. | `dashboard_data.py` explicitly exists to make this a small, bounded change instead of "~4 distinct drift bugs." |
| Add a persisted field | 1 schema file (`migrations/versions/`, currently only 1 version exists — `001_create_llm_router_health.py`) **if** the field's table already routes through `storage/`; realistically 2-3 files if the table is one of the 63 direct-SQLite modules, since the write site, the read site, and any dataclass shape (§12) all live in the same un-abstracted file with no separation of schema from access code. | Direct consequence of STR-003 — the fitness cost of "add a field" is proportional to whether that table happens to be one of the 63 bypass sites. |
| Remove one provider | ≥5 (mirror of "add a provider" — same scatter, same files, in reverse) | Same evidence as above. |
| Remove one host | 1 file if via `install.py`'s real path (delete the block); the `hosts/` package could be deleted independently with zero effect on removal, since it isn't in the removal's call path either — direct evidence for STR-001's "this abstraction removes nothing" verdict, extended to the removal direction too. |
| Remove one MCP tool | Not traced this session (belongs to domain 05/29's MCP surface, not structure) — noted as a gap, not answered. |
| Remove an experimental subsystem (e.g. `frameworks/`) | 1 package deletion (9 files) + `pyproject.toml`'s optional-dependency extras if any reference `frameworks` specifically (not checked) + test files that import it (3 found: `tests/test_shipped_modules_import.py`, `tests/test_agno_integration.py`, `tests/qa/test_agno_deep.py` — though 2 of those import `integrations.agno`, which would survive; only the `frameworks`-specific import sites need updating). | Directly evidenced this session (§10 STR-004). |

---

## Findings register (§66 format)

```
ID: STR-001
Category: Abstraction / dead integration point
Severity: HIGH
Confidence: HIGH
Location: Files: src/llm_router/hosts/base.py, src/llm_router/hosts/cursor.py,
  src/llm_router/hosts/gemini_cli.py, src/llm_router/hosts/events.py,
  src/llm_router/hosts/hook_io.py, src/llm_router/commands/install.py
  Symbols: HostAdapter (Protocol), CursorAdapter
  Lines: hosts/base.py:42 (Protocol def); commands/install.py:1015-1443 (inline
  per-host JSON-merge logic that duplicates what the Protocol was built for)
Observation: A full Protocol-based host-adapter abstraction exists (base.py +
  concrete CursorAdapter) but the real, shipping installation flow
  (commands/install.py) never calls it — it writes every host's MCP config
  inline instead.
Evidence: grep -rn "llm_router\.hosts" src tests --include="*.py" (excluding
  hosts/ itself) returns matches ONLY in tests/*.py, none in src/. install.py
  contains its own _merge_json_mcp_block() and a hardcoded json_targets list
  covering Cursor/Copilot/OpenClaw/VS Code/Trae/Gemini CLI paths.
Why this exists, if discoverable: Likely built ahead of an intended
  refactor of install.py that never happened, or built by a different
  contributor than the one who wrote install.py's per-host branches.
Why this matters: A maintainer adding a new host today has two conflicting,
  equally-plausible places to add it (a hosts/<name>.py adapter, or another
  install.py branch) — only one actually ships.
User-visible impact: none directly (install.py works); but a contributor
  following the "designed" pattern (implementing HostAdapter) would ship a
  feature that installs nothing.
Engineering impact: 6 files of unreachable complexity; doubles the mental
  model for "how do hosts get installed."
Is behavior currently used? NO (hosts/ package itself) / YES (install.py's
  inline logic)
Recommended action: DELETE hosts/ package, OR refactor install.py to route
  through it — pick one. Proposed target: delete hosts/ unless another
  audit workstream depends on the adapter pattern surviving (check the
  synthesis-stage deletion ledger before finalizing).
Behavioral compatibility risk: NONE if hosts/ is deleted (zero production
  callers). If install.py is refactored instead, MEDIUM (touches every
  host's install path).
Security risk: none identified.
Performance impact: none.
Estimated complexity removed: ~6 files, ~400-600 LOC (hosts/ package total,
  approx from `wc -l` of its files, not independently re-summed here).
Validation required: confirm no doc/README references the hosts/ adapter
  pattern as a public extension point (would make this a documented API,
  not just internal dead code) before deleting.
Dependencies on other findings: none.
```

```
ID: STR-002
Category: Dead code (self-admitted, ratchet-tracked)
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/context_signal.py
  Symbols: is_context_dependent, _mask_relative_pronouns
  Lines: module docstring (top of file)
Observation: The module's own docstring states: "NOT the one in
  production... Verified 2026-09-15: neither imports it. The advisory uses
  its own _is_context_dependent (hooks/auto-route.py)... Every reference
  outside this module is a test or mutation configuration."
Evidence: grep -rn "context_signal" src tests --include="*.py" confirms
  every non-docstring reference outside the module itself is in tests/
  (test_context_signal.py, test_s3_enforcement_layers_agree.py,
  test_s3b_relative_that_is_not_a_deixis.py) or config
  (test_gate13_mutmut_config_intact.py). One production file,
  operational_signal.py, references it only in a comment ("Mirrors
  context_signal.py"), not an import. tests/test_l03_dead_public_api_ratchet.py
  itself already documents this as known: "L-05 needed no change:
  context_signal.py's own docstring already states plainly [dead]."
Why this exists, if discoverable: superseded by hooks/auto-route.py's own
  _is_context_dependent during the S3b enforcement-layer work (2026-09-15).
Why this matters: it is not undiscovered dead code — it is tracked,
  intentional debt kept alive by tests that exercise its logic directly
  (possibly to protect a regex/pronoun-masking algorithm that's still
  useful as a reference implementation, or purely to keep a ratchet
  green). Deleting it requires updating those 3 test files, not just the
  module.
User-visible impact: none.
Engineering impact: low — one file, already known.
Is behavior currently used? NO (not in the production routing path).
Recommended action: DELETE (module + its 3 dedicated test files), after
  confirming the algorithm it contains (`_mask_relative_pronouns`) has no
  independent value hooks/auto-route.py's own version lacks — the tests
  currently protect the STANDALONE module, not the production behavior, so
  deleting both together changes nothing observable.
Behavioral compatibility risk: NONE (proven unreachable from production).
Security risk: none.
Performance impact: none.
Estimated complexity removed: 1 file (145 LOC) + 3 test files.
Validation required: confirm hooks/auto-route.py's _is_context_dependent
  is not itself missing a fix that context_signal.py already has (the
  docstring says a "redundant recheck was deliberately removed" from
  enforce-route.py — confirm that removal, not this module, is what's
  authoritative).
Dependencies on other findings: none.
```

```
ID: STR-003
Category: Abstraction adoption failure / storage boundary
Severity: HIGH
Confidence: HIGH
Location: Files: src/llm_router/storage/service.py,
  src/llm_router/storage/adapters/{base,sqlite_adapter,yaml_adapter,json_adapter}.py
  and 63 files outside storage/ (context.py, feedback.py, execution_ledger.py,
  budget.py, budget_backend.py, attribution.py, quota_tracker.py, cost.py,
  telemetry.py, most of commands/ and hooks/, agentic/telemetry.py,
  agents/session.py, semantic/store.py, control_plane/store.py,
  lineage/lineage_store.py, dashboard_data.py, result_cache.py, and more —
  full list captured in the plan-file evidence trail)
  Symbols: StorageAdapter (Protocol[T]), SqliteAdapter, YamlAdapter,
  JsonAdapter, StorageService
Observation: storage/ is documented (via budget_store.py's own docstring:
  "REFACTORED (Phase 2): Now delegates to StorageService abstraction
  layer... enforces atomicity, error recovery") as the storage abstraction
  layer, but 63 files import sqlite3/aiosqlite directly instead of going
  through it.
Evidence: grep -rl "import sqlite3\|import aiosqlite" src/llm_router
  --include="*.py" | grep -v "^src/llm_router/storage/" → 63 matches,
  enumerated above.
Why this exists, if discoverable: storage/ was likely introduced after
  most of these modules already existed (budget_store.py's "REFACTORED
  (Phase 2)" language implies a later migration that only covered its own
  file), and the migration was never carried through the rest of the tree.
Why this matters: an abstraction meant to guarantee atomicity/error-
  recovery for ALL persistence only actually guarantees it for whichever
  handful of modules were migrated — the other 63 have no such guarantee
  and each implements (or fails to implement) its own transaction
  discipline.
User-visible impact: inconsistent crash/corruption recovery behavior
  across subsystems (not independently measured this session — flagged
  for domain 13/16 concurrency-and-durability audit to quantify against
  real failure scenarios).
Engineering impact: HIGH — every new persisted concept has a 50/50 choice
  of "the right way" vs "the way everything else does it," and the
  wrong-but-common way wins by sheer weight of precedent.
Is behavior currently used? YES (both paths are live; this is not dead
  code, it's parallel, unequal-guarantee code).
Recommended action: SIMPLIFY via ratchet-lint (baseline=63, count-down-
  only), migrating call sites incrementally rather than a flag-day
  rewrite — matches this repo's own established pattern
  (scripts/lint_unknown_as_number.py, BASELINE=113, same shape) per
  CLAUDE.md. Proposed target: storage/ becomes the only door to SQLite;
  add a lint/CI check enforcing the ratchet.
Behavioral compatibility risk: MEDIUM-HIGH if migrated carelessly (each
  of the 63 sites has its own transaction assumptions); LOW if migrated
  one file at a time with existing tests as the safety net.
Security risk: none directly, though inconsistent atomicity is adjacent
  to the "SQLite locked" edge case the brief's §14 asks about.
Performance impact: unknown — StorageService's overhead vs raw sqlite3
  was not benchmarked this session.
Estimated complexity removed: none removed by deletion (this is a
  consolidation, not a deletion) — estimated LOC *avoided in future*
  maintenance is large but not quantifiable without per-site review.
Validation required: per-migration, existing tests for that module must
  still pass; a global "no raw sqlite3 outside storage/" lint test should
  be added alongside the first migrated file, not after the last.
Dependencies on other findings: overlaps domain 13 (concurrency/
  durability) and domain 16 (state/storage) — this finding supplies the
  file-count evidence; those domains should supply the correctness/
  concurrency verdict per table.
```

```
ID: STR-004
Category: Semantic duplication / unused package
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/frameworks/{__init__,agno,base,
  claude_agent_sdk,crewai,hermes,langgraph,openai_agents,pydantic_ai}.py
  Symbols: FrameworkAdapter (Protocol)
Observation: frameworks/ package (9 files) has zero production (src/)
  importers; its own __init__.py docstring states only "agno" is
  "concrete" (a re-export shim over llm_router.integrations.agno), while
  hermes is "skeleton," and langgraph/crewai/openai_agents/
  claude_agent_sdk/pydantic_ai are stubs ("PR welcome").
Evidence: grep -rln "from llm_router\.frameworks\|llm_router\.frameworks\."
  src --include="*.py" | grep -v "^src/llm_router/frameworks/" → empty.
  Real Agno usage (tests/test_agno_integration.py, tests/qa/test_agno_deep.py)
  imports llm_router.integrations.agno directly, not
  llm_router.frameworks.agno.
Why this exists, if discoverable: appears to be forward-looking scaffolding
  for a "unified frameworks namespace" (per its own docstring) that never
  got production adoption.
Why this matters: 9 files of surface area, most of it non-functional
  stubs, for a namespace nothing outside its own tests uses.
User-visible impact: none (not on any executed path).
Engineering impact: moderate — inflates the package count and gives a
  false impression of framework-integration breadth (7 named frameworks
  "supported," 1 of which is a shim over something else, and 0 of which
  are reachable from production code).
Is behavior currently used? NO.
Recommended action: DELETE or DEPRECATE to a roadmap doc until a second
  adapter goes from stub to concrete AND gets a production caller.
Proposed target: none (removed) or docs/ROADMAP.md entry.
Behavioral compatibility risk: LOW (zero production callers; 3 test files
  would need updating, 2 of which already bypass frameworks/ anyway).
Security risk: none.
Performance impact: none.
Estimated complexity removed: 9 files.
Validation required: confirm README/marketing copy does not claim these
  framework integrations as shipped (a domain 41/45 README-claims
  question, flagged here as a dependency).
Dependencies on other findings: domain 41 (README claim ledger) should
  check whether "Agno, Hermes, LangGraph, CrewAI..." are claimed as
  supported anywhere user-facing.
```

```
ID: STR-005
Category: Naming collision (package-level)
Severity: LOW
Confidence: HIGH
Location: Files: src/llm_router/policies/*.yaml (6 files),
  src/llm_router/rules/*.md (13 files), src/llm_router/policy.py,
  src/llm_router/gates.py, src/llm_router/reason_gate.py,
  src/llm_router/org_policy.py
Observation: Two top-level directories (policies/, rules/) share exact
  vocabulary with the routing-policy code (policy.py, gates.py,
  reason_gate.py, org_policy.py) while containing zero code — one is YAML
  presets, the other is per-IDE Markdown rule templates.
Evidence: `ls src/llm_router/policies` → 6 .yaml files only; `ls
  src/llm_router/rules` → 13 .md files only; actual policy/gate code
  confirmed to live flat at package root via grep for
  "class.*Policy\|class.*Gate" outside those two directories.
Why this exists, if discoverable: organic growth — YAML presets and IDE
  rule templates needed a home and were given the same word as the
  routing concept they configure/describe.
Why this matters: pure discoverability cost; a maintainer or new
  contributor grepping the package tree for "policy" gets a false lead.
User-visible impact: none.
Engineering impact: low but nonzero, onboarding friction.
Is behavior currently used? YES (both dirs' contents are real assets),
  just misleadingly named.
Recommended action: MOVE/RENAME — policies/ → policy_presets/, rules/ →
  host_rule_templates/ (or relocate under docs/ or assets/).
Behavioral compatibility risk: LOW — pure rename; check for hardcoded
  path references to src/llm_router/policies/*.yaml or rules/*.md in
  loader code and packaging manifests (MANIFEST.in / pyproject
  package-data) before renaming.
Security risk: none.
Performance impact: none.
Estimated complexity removed: 0 LOC removed; pure clarity gain.
Validation required: grep for hardcoded "llm_router/policies" and
  "llm_router/rules" path strings in loader/install code and packaging
  config before renaming, to avoid breaking a runtime path lookup.
Dependencies on other findings: none.
```

```
ID: STR-006
Category: Domain model — duplicate type name, disjoint shape
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/model_tracking.py, src/llm_router/terminal_style.py
  Symbols: RoutingDecision (both)
  Lines: model_tracking.py:32, terminal_style.py:95
Observation: Two unrelated dataclasses share the class name
  RoutingDecision: one is a 13-field persistence/logging record
  (timestamp, task_type, chain_position, quota_pressure, quality_feedback,
  etc.), the other a 7-field UI/display value object with a format_hud()
  method (model, confidence, task, cost, escalated, etc.) — no shared
  base class, no conversion function found between them.
Evidence: sed -n '/class RoutingDecision/,/^class \|^def /p' on both files
  shows fully disjoint field sets.
Why this exists, if discoverable: independently authored for different
  purposes (structured logging vs terminal display) without checking for
  an existing name.
Why this matters: any tooling, comment, or new contributor referring to
  "the RoutingDecision type" is ambiguous without a fully-qualified
  import; a well-intentioned "let's not have two RoutingDecision classes,
  merge them" instinct would actually break both call sites since they
  encode different things.
User-visible impact: none directly.
Engineering impact: moderate — confuses any grep-based investigation
  (including this one, until manually disambiguated) and complicates
  future refactors that touch "routing decision" logic.
Is behavior currently used? YES (both, independently).
Recommended action: RENAME one (recommend terminal_style.py's →
  RoutingDecisionDisplay or DecisionHud) to remove the collision; no
  behavior change needed, pure identifier rename.
Behavioral compatibility risk: LOW — internal rename, check for any
  serialized/pickled reference to the class name (unlikely for a display
  object) before renaming.
Security risk: none.
Performance impact: none.
Estimated complexity removed: 0 (clarity gain only).
Validation required: grep for "RoutingDecision" import sites in both
  files' consumers to confirm the rename doesn't miss a call site.
Dependencies on other findings: relates to STR-005/§40 terminology table.
```

```
ID: STR-007
Category: Semantic duplication — parallel unfinished subsystem
Severity: MEDIUM
Confidence: MEDIUM (caller graph not fully traced — flagged UNCERTAIN
  per brief §1, not asserted as PROVEN DEAD)
Location: Files: src/llm_router/decisions/engine.py,
  src/llm_router/signals/{__init__,base,keyword,pii}.py
  Symbols: decisions.engine (module), Signal (Protocol)
Observation: A "v0.0.1... first-match boolean composition over signal
  scores" decision engine exists as its own package pair, but
  src/llm_router/router.py (5,359 lines, the real routing entry point)
  has no top-level import of llm_router.signals or llm_router.decisions.
Evidence: grep -n "^from llm_router\|^import llm_router" router.py does
  not include signals or decisions; grep -rln for decisions/ outside
  itself finds only signals/__init__.py (self-referential) and 4 test
  files (test_decision_boosts.py, tests/qa/test_performance.py,
  tests/scenarios/test_cli_scenarios.py, tests/scenarios/
  test_cross_cutting.py).
Why this exists, if discoverable: module's own docstring frames it as a
  planned v0.0.1→v0.0.2 evolution ("v0.0.2 will add the YAML loader and
  AND/OR/NOT operator nodes") that appears not to have progressed by this
  commit.
Why this matters: if genuinely unreached at runtime, it is dead weight
  wearing "decision engine" — a name that invites confusion with the
  actual routing-decision logic in classify.py/gates.py/contract.py.
User-visible impact: unknown pending trace.
Engineering impact: moderate if dead; if NOT dead (e.g. reached via a
  mechanism this session's static grep missed — dynamic import, test
  fixture monkeypatch), no impact and this finding should be downgraded.
Is behavior currently used? UNCERTAIN.
Recommended action: hold at UNCERTAIN; recommend domain 06 (routing
  engine, runtime-trace mandate) run an actual traced request through
  router.py and confirm whether signals/decisions is ever touched before
  any deletion decision is made.
Behavioral compatibility risk: unknown until traced.
Security risk: none identified.
Performance impact: none identified.
Estimated complexity removed: up to 6 files if confirmed dead.
Validation required: runtime trace of a live routed request (domain 06's
  mandate) confirming signals/decisions is or isn't on the call path.
Dependencies on other findings: depends on domain 06's runtime trace.
```

---

## Top items for synthesis (candidates for global Top-10 lists)

1. **STR-003** — `storage/` abstraction has ~2% real adoption (63 of ~66+ SQLite-touching files bypass it). Best "abstraction exists, not adopted" candidate for the global complexity-sources Top 10, and the strongest single driver of future "same fact, two shapes" bugs.
2. **STR-001** — `hosts.HostAdapter` Protocol: zero production callers, the real install path (`commands/install.py`) reimplements the same job inline. Best "abstraction removes zero concrete complexity" candidate.
3. **cache/store.py `SemanticCache` stub** (§9 row 1) — cleanest PROVEN-DEAD deletion candidate in the whole domain: always-returns-None, zero callers, a fully-working duplicate already exists elsewhere under the same name. Best deletion-ledger candidate: small, safe, zero behavioral risk.
4. **STR-004** — `frameworks/` package: 9 files, 1 partial re-export, 6 stubs, zero production callers. Second-best deletion-ledger candidate; also a README-claims risk (domain 41 should check).
5. **§9 row 5 — `classify.py` "single source of truth" vs `hooks/auto-route.py`'s hand-synced duplicate table** ("Option B, left untouched"): best correctness-risk candidate — this is a structure finding that is really a routing-safety time bomb (two classification tables, one drift away from disaster), consistent with this repo's own documented S8 finding about classifier fall-through defaults deciding half of traffic.
6. **STR-002** — `context_signal.py`: best "the codebase already told us" finding — a module whose own docstring plus an existing ratchet test both already say "this is dead." Cheapest possible deletion (module + 3 known test files), and a good example for the report's methodology section of trusting-but-verifying an existing self-diagnosis.
7. **§9 row 9 / STR consolidation** — "budget" spanning 3 unrelated meanings across 9 files: best terminology-table entry and best package-move consolidation candidate (pure move, zero logic risk, immediate clarity gain).
8. **§9 row 18 / STR-003's twin** — `decisions/`+`signals/` mini decision-engine, uncalled from `router.py`'s own imports: hand to domain 06 for the runtime trace that would upgrade this from UNCERTAIN to a second major deletion.
9. **STR-005** — `policies/`/`rules/` directory-name collision with the actual policy/gate code: zero risk, pure rename, good "quick win" for the phased plan's Phase 1 (proven-safe deletions/renames).
10. **§10's positive control** — `ProviderQuirk` Protocol (6 real impls, identity-default, each solving a cited bug): include in the do-not-change register as the example of a Protocol earning its cost, to calibrate the report against over-flagging every abstraction as a smell.

## Explicit gaps / not completed this session (say so, don't silently drop)

- §9 rows 4 (`dashboard/` vs `dashboard_data.py` real adoption), 11 (`monitoring/` vs `observability/` overlap), 13 (`agents/` vs `agentic/` full caller graph), 17 (`commands/` vs `scripts/` overlap — 63 of 106 scripts have their own `main`) are flagged UNCERTAIN, not resolved — each needs one more targeted grep pass that time did not permit this session.
- §11's `library/` and `tools/` packages were not opened/read this session — deferred, not silently assumed clean.
- §12 domain model is illustrative (2 confirmed collisions), not exhaustive — a full canonical-type sweep (route/decision/model/provider/capability/prompt/request/response/attempt/execution/policy/quota/cost/savings/session/host/tool/agent/fallback across all 415 files) was not completed at the brief's full scope.
- §52 fitness-scenario file counts for "remove one MCP tool" was not answered (belongs to domain 05/29's surface, not traced here).
