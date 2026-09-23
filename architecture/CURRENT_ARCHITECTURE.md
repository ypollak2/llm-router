# CURRENT_ARCHITECTURE.md

What exists on 2026-09-23, at `main` @ `783e9bb`. Every claim cites a module,
function or a measured number. Where something was not verified, it says so.

Scale: `src/llm_router/` is 412 files; `router.py` alone is ~5,300 lines.
Archaeology covered the subsystems reachable from the six entry points plus the
knowledge/learning modules. Anything not listed was not traced and must not be
assumed either working or broken.

---

## 1. Entry points

Six ways a routing decision is produced. They do **not** share one engine.

| Entry point | Code | Authority |
|---|---|---|
| UserPromptSubmit hook | `hooks/auto-route.py` — its **own** 6-stage classifier chain, `SIGNALS` table at `:1018` | **Advisory only.** Injects `⚡ ROUTE:` text; Claude may ignore it |
| PreToolUse enforcement | `hooks/enforce-route.py` | Authoritative but scoped — gates *tool calls*; cannot intercept a prose-only answer |
| MCP tools | `tools/routing.py` (`llm_classify:36`, `llm_route:257`, `llm_auto:479`, `llm_stream:630`, `llm_select_agent:737`, `llm_reroute:859`) | Authoritative — executes and returns provider output |
| HTTP gateway | `gateway.py` (`/route:500`, `/v1/chat/completions:688`, `/v1/responses:722`, `/v1/messages:777`, `/api/chat:821`, `/api/generate:836`) | Authoritative. **Refuses `tools`/`tool_choice` with HTTP 400** (`_refuse_tools_if_present:634`) |
| Zero-dep stdlib server | `route_server.py:35,182,277` | Authoritative — shares the same core |
| `router.py` direct | `route_and_call:3715`, `route_and_stream:5055` | The core every other path funnels into |

Plus agent-session wrappers (`tools/agents.py:119,190`) and delegation
(`tools/agentic.py:94`, `tools/fs.py:182`).

**The gateway process is not startable from any packaged entry point.**
`gateway_service.py` has zero callers in `src/` — not `cli.py`,
`install_hooks.py`, `onboard.py`, `quickstart.py` or `commands/serve.py`.

---

## 2. The routing pipeline

`route_and_call` (`router.py:3715`), in order:

1. Budget check — fail fast on monthly cap.
2. **Classification** — `classify.classify_signals()` (`classify.py:585`),
   deterministic weighted regex scoring (intent ×3, topic ×2, format ×1).
   Confidence gate at `_CONFIDENCE_THRESHOLD = 2`; ambiguous escalates to
   `classify.classify()` (LLM classifier via `classifier.py`).
3. Profile resolution — complexity → `budget` / `balanced` / `premium`.
4. **Chain construction** — `router.py:_build_and_filter_chain:329` →
   `dynamic_routing.get_dynamic_model_chain:399`, falling back to
   `profiles.get_model_chain:462`.
5. **~900 lines of sequential list mutation** inside `_build_and_filter_chain`:
   provider filter → Ollama/Codex/Gemini-CLI injection → mid-tier metered
   injection → re-applied block/allow filters → agent-context reorder →
   `user_routing_policy.apply_routing_policy:797` → agentic pin → dedup →
   quota-balanced reorder → precision-tier fronting → subject-specialist
   override.
6. Bandit reorder — `bandit.EpsilonGreedyBandit().reorder()` at `router.py:4249`.
7. Daily-cap downgrade — applied **last**, deliberately.
8. Dispatch loop — `HealthTracker.is_healthy()` (`health.py:197`) skip,
   `_call_text`/`_call_media`, P2 quality-gated escalation, record success/fail.
9. Cost logging, audit row, savings pipeline.

**`chain_builder.py` is dead.** Self-documented since 2026-09-15: *"NOT the live
chain builder… every repository reference to this module's `build_chain()` is a
test."* `provider_registry.py` is dead too, despite a docstring claiming the
routing path reads it.

---

## 3. Capability representation — descriptive, not enforcing

Two unrelated things share the name.

- **`model_registry.py`** — `ModelMetadata:59` with `context_window` and a
  capability tuple (vision / function-calling / json / reasoning), hardcoded at
  `:275-350`. **Nothing outside this module and its tests reads either field.**
  `router.py` imports one constant from it, `GOOGLE_PROVIDERS` (`router.py:1854`).
- **`capabilities.py`** — `CapabilityRequirement` / `detect_capabilities:165`
  describes what a **task** needs, by regex. Gated off by default
  (`capability_routing_enabled:218`, `LLM_ROUTER_CAPABILITY_ROUTING`, "shadow
  mode"). Its only production consumer is `hooks/chain_builder.py:292` — itself
  a dead path — plus `cost.py:1853`, which only **logs** it, annotated *"never
  read by live routing"*.

**There is no capability filter on the live path that can exclude a model as
incapable. Ordering only.**

---

## 4. Provider abstraction

No `Provider` class. One function: `providers.call_llm:137`, dispatching through
**LiteLLM**, with `provider_quirks.get_quirk()` for per-provider transforms.
Subprocess-backed CLIs are separate: `codex_agent.run_codex:251`,
`claude_agent.run_claude:135`, `gemini_cli_agent.py`.

Model list is **static + probed**: `model_registry` catalog and `profiles.py`
chains; availability via `discover.py` (`is_ollama_available:39` live-probes
localhost:11434 with a 5 s cache; `get_available_providers:174`;
`_scan_*:269-297` cached to `discovery.json:378`, 1 h TTL).

---

## 5. Fallback and escalation

- **Failure fallback** — walk the next model in the already-built chain on any
  exception. `HealthTracker` circuit-breaks a provider after repeated failures.
- **P2 quality-gated escalation** (`router.py:2538-3107`) — a judge scores the
  cheap answer; below `LLM_ROUTER_ESCALATE_THRESHOLD` (0.4), not trivially
  short, inside a 20 s deadline, and not already escalated this turn, it
  advances to the **next model already in the chain** — not an independent
  strong-model lookup.

**Whether P2 has ever fired is unknowable**: judge scores are persisted in 0 of
1601 rows (§8).

---

## 6. Delegation, tool execution, agentic

llm-router **can** write files and run commands.

- `hooks/agent_writes.py` — `LLM_ROUTER_AGENT_WRITES` ∈ `propose` (**default**,
  diff only), `apply`, `off`.
- `tools/fs.py` — `llm_fs_edit_many:182`, `llm_fs_rename:136`, sandboxed under
  `project_root` (`_assert_under_root:70`), **not registered** unless
  `LLM_ROUTER_FS_TOOLS=on`.
- Command execution has its own allowlist; blocks `rm -rf`/`mkfs`/`dd` shapes
  but not `git push`, `pip install` or `curl|sh`.

### MGEE — the milestone-gated escalating execution engine

`src/llm_router/agentic/` (1,978 lines). **This is the most important existing
asset for the proposed design.**

| Module | What it provides |
|---|---|
| `engine.py` (282) | Monotonic escalation over a finite tier ladder, bounded attempts per (milestone, tier) ⇒ **provably terminates** as COMPLETE or a *surfaced* failure |
| `acceptance.py` (362) | Objective **executable** checks — `cmd` / `lint` / `diff` / `canary`. A milestone is DONE only on a passing check, **never** the model's self-report. `reproducible()` re-runs to detect flaky |
| `planner.py` (134) | A model proposes the milestone breakdown; every acceptance check is **constrained to the objective vocabulary and validated**. A milestone proposing a subjective check is rejected |
| `ledger.py` (183) | `TaskLedger` + `Milestone`; passed milestones **frozen** into a done-frontier, never re-executed. Escalation resumes at the first pending milestone with frozen artifacts as read-only context |
| `react.py`, `delegate.py`, `worktree.py`, `adapters.py` | ReAct loop, delegation, isolated worktree, `CodexAdapter` |

Live via `tools/agentic.py` → `service.run_delegation` → `engine.Agent`.
38 test files reference it.

**Its design doc does not exist.** Four docstrings cite `docs/agentic-router.md`;
there is no such file. The termination proof is unreadable.

---

## 7. Context, knowledge, retrieval

- **OKF** (Open Knowledge Format) — `okf.py`. Indexes ModelCapability,
  SourceFile (written as a side-effect of successful routing), SessionNote.
  **Never stores model prose** (`okf.py:135`, "the hallucination amplifier").
  Per-project. On by default.
- **Scope resolution** — `semantic/scope.py:resolve_scope()`, the single
  canonical resolver; `resolve_scope_or_none():105-125` returns None rather than
  guessing. Fixes OKF-SCOPE-01, where the long-lived MCP server's `cwd=$HOME`
  pulled another repo's files into a "capital of Portugal" prompt.
- **Semantic classifier** — `semantic_classify.py`, prototype/centroid cosine,
  called from `classify.py:646` as a low-confidence fallback.
- **Semantic cache** — `semantic_cache.py`. The passport incident (one
  passport's answer served for another) is **fixed, not disabled**: threshold
  0.95 → `DEFAULT_THRESHOLD = 0.98`, plus `_discriminator():252` extracting
  numbers/polarity and refusing a hit when they differ. The code states the
  threshold alone "cannot fix this"; the discriminator is "the actual fix."
- **Code entity index** — `semantic/store.py` (SQLite): `find_definitions`,
  `find_importers`, `known_files`, `content_hash():143`. **This is a proto
  Project Semantic Graph.**
- **Retrieval** — none with ranking. `context_optimizer.optimize_context()`
  does structural compression then recency truncation (old turns capped at 200
  chars), skipped entirely for free/local models. `context_injection.inject():36`
  injects OKF concepts at a fixed `limit=3`.
- **Context reuse across calls** — **none.** `session_store._content_hash()`
  dedups transcript writes; `semantic/store.content_hash()` dedups file
  re-indexing; `result_cache.py` caches final responses. No artifact store keyed
  by a context fingerprint.
- **Provider prompt caching** — `usage.cache_hit` / `cache_savings_usd` columns
  exist; **no writer found.** A claim gap.

---

## 8. Telemetry, cost, and what the data actually contains

`usage.db`, 16 tables. Measured 2026-09-23.

| Table | Rows |
|---|---|
| `claude_usage` | 37,874 |
| `savings_stats` | 8,928 |
| `quota_snapshots` | 2,960 |
| `execution_events` | 2,287 |
| `routing_decisions` | **1,601** |
| `compression_stats` | 1,069 |
| `model_quality_trends` | 462 |
| `usage` | 290 |
| `corrections`, `session_summaries`, `benchmark_results`, `semantic_cache`, `gemini_usage`, `codex_usage` | **0** |

`routing_decisions` (n=1601) — the table the bandit learns from:

| Column | Reality |
|---|---|
| `provenance` | NULL 1387 / `runtime` 214 |
| `success` | 1 in **1593**, 0 in 8 (99.5%) |
| `judge_score` | **non-NULL in 0** |
| `cost_usd` | non-NULL in all, **2 distinct values** (0.0 ×214, 0.01 ×1387) |
| `latency_ms` | non-NULL in all, 215 distinct — real |
| `subject` | **NULL in 1601** — yet the bandit keys on (profile, subject, model) |
| `session_id`, `prompt_sequence` | **0 populated** |
| `final_model` | gpt-4o 1057, claude-opus 330, lfm2.5:8b 99, qwen3-coder:30b 96 — **87% is two models** |
| Date range | 2026-07-08 → 2026-09-23. **Last 7 days: 13 rows.** Last 30: 214 |

`model_quality_trends` (462 rows) looks like outcome tracking but has 74 distinct
timestamps for 462 rows with recurring identical values — consistent with seeded
backfill. Stale since 2026-08-19.

**No table has `parent_task_id`, `workflow_id` or `node_id`.** Every row is one
isolated call.

---

## 9. Learning and adaptation mechanisms

| Module | Wired? |
|---|---|
| `bandit.py` + `telemetry.py` | Yes — `router.py:4249`, on by default. `MIN_SAMPLES_FOR_SIGNAL = 30` |
| `calibration.py` | Yes, heavily |
| `judge.py` | Wired (`cost.py:1912`) — **produces zero live rows** |
| `routing_quality.py` (703) | Yes, 7 callers |
| `quality_feedback.py` (478) | Yes — `should_skip_model` can suppress a model |
| `model_tracking.py` (502) | Yes — the main writer |
| `retrospective.py` (971) | Yes — but output is human-readable text, not a routing input |
| `feedback.py` (319), `feedback_handler.py` (249) | **Zero non-test callers. CLASS-A.** |

---

## 10. Persistence, config, CLI, hooks, tests

- **Storage** under `paths.state_path(...)`, one root, per-project subdirs keyed
  by `project_slug()`/`scope_key()`. On this machine: `knowledge/` **298 MB**
  (with **12,448** project subdirs, many named after test fixtures — test writes
  leaked into the real store), `routing_quality.jsonl` 23 MB (+22 MB `.bak`),
  `usage.db` 6 MB, `auto-route-debug.log` 4.7 MB.
- **CLI** — `cli.py:main()` dispatches ~40 lazily-imported subcommands.
- **Hooks** — `install_hooks.py` writes into `~/.claude`. T-15 made four host
  paths resolve at access time; two writers were missed and fixed 2026-09-23
  (`~/.claude.json`, the Claude Desktop config), plus a subprocess shell-out to
  the real `claude` binary that no path constant could redirect.
- **Tests** — ~9,550 collected. Gate must run under a clean `HOME`: three tests
  passed only on a machine with prior state and left CI red across two releases.

---

## 11. agenticgraphs (`vitruvian-graphs` 0.10.0, AGR spec v1.9)

| Aspect | Reality |
|---|---|
| Definition | Pure YAML per graph, JSON-Schema validated. No callables. **Runtime-constructible** |
| Node kinds | `agent`, `verifier`, `human`, `router`, `subgraph`, `search` |
| Edges | `{from,to,when,kind}`; `kind` ∈ `flow`/`error`/`compensate`; `when` is a sandboxed AST expression (`safeexpr.py`) |
| **Cycles** | **Supported, guarded.** The lint (`validate.py:1043-1050`) **rejects unconditional back-edges**; every loop needs a `when` guard plus `termination.max_steps` |
| Retries | Per-node `retries.max` (0–5); a retryable node bound to a non-idempotent ability must declare `reissue_effects` |
| Failure taxonomy | 10 typed categories on `RunReport` |
| Parallelism | ThreadPoolExecutor for same-`parallel_group`; **`fan_out` shards run sequentially** |
| State | One shared mutable blackboard dict; optional end-of-run JSON Schema validation |
| Checkpoints | JSONL journal + `resume_from`; caller owns the file |
| Human gate | `kind: human` with `approval.contract`; the runner **refuses to sign by default** |
| Composition | `kind: subgraph` **inline expansion**, depth ≤ 3, acyclic across refs |
| **Runtime mutation** | **None.** Topology frozen before the run loop |
| Observability | `RunReport`: `trace`, `frames`, `tool_calls`, `usage` with **`usd_measured: bool`** |
| Library | 83 graphs, 15 domains, 17 motifs, 44 test files (~501 tests) |
| **Model selection** | **None.** Three env vars → one OpenAI-compatible URL. No registry, no routing, no cost logic |

Self-admitted gaps: **61 of 83 graphs** sit at the weakest verification depth
(the model's own assert-graded account); only **20 of 83** reach executable
`command` depth; `edit_files`, `run_suite`, `rollback`, `execute_step` are
**declared but not bound to real endpoints** — "narrated, not executable". Live
pass rate 81% (qwen3-coder:30b), 9% (qwen3.5); 18 of 139 contracts pass on no
model tested.

**No integration with llm-router exists.** No reference to it in the repo.
