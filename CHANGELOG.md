# Changelog

All notable changes to `llm-router` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

| Where | What |
|---|---|
| This file | The current major line — v11.0.0 onward |
| [CHANGELOG-ARCHIVE.md](CHANGELOG-ARCHIVE.md) | v10.1.5 back to v6.3.0 |
| [GitHub Releases](https://github.com/ypollak2/llm-router/releases) | v6.2 and earlier |

## [Unreleased]

_Nothing yet._

## [13.0.4] — Security: remove the unauthenticated SSE entry point (2026-08-28)

### Security

- **`llm-router-sse` is removed.** `server.main_sse`'s own docstring forbids
  exposing it as a console script — the prior entry point "bound 0.0.0.0 with no
  auth and exposed the full 60-tool MCP surface — including filesystem tools and
  wallet — to anyone reachable on the network" — and lists three conditions that
  must all hold before re-adding it. It was nonetheless present in
  `[project.scripts]` and shipped in 13.0.2 and 13.0.3 with none of them met: no
  auth middleware (that is `main_sse_secured`, which the script did not point
  at), and a bind of `os.environ.get("HOST", "0.0.0.0")` that never consulted
  `_allow_public_bind()` — a gate defined a few lines below it.

  Anyone who ran `llm-router-sse` published the full MCP tool surface,
  unauthenticated, on every interface. **If you have run it, stop the process
  and upgrade.** Nothing else invokes it, so an install that never ran the
  command was not exposed.

  Defence in depth: `main_sse` now defaults to `127.0.0.1` and refuses a
  non-local bind unless the shared gate allows it.

### Fixed

- **Statusline reported "✗ no provider" for every Ollama-only setup** (#50).
  `$SAVINGS_LOG` was read four times and never assigned, so `open("")` threw
  into a swallowed except and the health check could never see local activity —
  breaking the documented "route free to local Ollama with no cloud keys" path.
- **The dashboard's access URL was redacted by its own scrubber** (#48). The
  token pattern matched both the `token=` and `url=` log fields, so the only
  documented way to obtain a working URL printed `[REDACTED-TOKEN]`. The URL is
  now printed outside the logging pipeline; redaction itself is unchanged.
- **`doctor` reported three different states as "no routing decisions"** (#55).
  An unreadable table, an empty table on a machine that recorded activity
  elsewhere, and a genuinely idle machine printed the same sentence — so a user
  who had just made four calls was told to make some calls. Root cause: four
  different functions named `log_routing_decision` write to four destinations.
- **Session snapshots asserted zeros they had not measured** (#56). Downstream
  of the above; `accuracy: 1.0` derived from zero samples is what made the files
  look real. Facts now carry a `measured` flag.
- **`--help` crashed or was ignored on three console scripts** (#51, #52).
  `onboard` and `quickstart` ran their interactive flows and died on `EOFError`
  under a non-TTY stdin. `--help` is now handled first, before any other work,
  and is verified to open no socket.
- **The Textual install hint named a package that does not exist** (#47).
  `pip install llm_router[tui]` was wrong three ways; now
  `pipx inject llm-routing textual`.
- **`set-enforce` changed every running session on the machine** (#49). It is
  now scoped to the session that ran it (`--global` restores the old
  behaviour), and the messages describe what actually happens.
- **"routed" could mean a hint rather than an execution** (#53). Reserved for
  real executions; hint counts say "classified" and name their source.
- **The enforcement block demanded an attribution the agent could not honestly
  give** (#54). The route-indicator line is now offered only when the routed
  answer is what the user actually receives, and the "violations are logged and
  escalated" language is gone — nothing was escalated.

### Added

- **DIRECT-execution timeouts are surfaced** (#57). They previously failed
  silently to a debug log while routing fell through to Claude, so a local path
  that never once succeeded looked like nothing was wrong. `doctor` now
  distinguishes "raise `LLM_ROUTER_OLLAMA_TIMEOUT` to N" from "this machine is
  too slow for local routing".

## [13.0.3] — The MCP command that could never resolve (2026-08-26)

Fixes GH#41 and GH#43, and four defects found while reproducing GH#42.

### The entry point (#41)

- **Every MCP registration named a command that does not exist.** `[project.scripts]`
  declares only the hyphenated `llm-router`, so `shutil.which("llm_router")` returned
  `None` on *every* install type — pipx, pip and uv alike. Thirty call sites depended on
  it. The Claude Desktop and claw-code paths fell back to the literal string
  `"llm_router"`; the main registration fell back to
  `uv run --directory <site-packages>`, which is what produced `CONNECTION_CLOSED` on a
  clean pipx install of 13.0.2. Every pull integration — VS Code, Cursor, Windsurf, Kimi,
  Gemini CLI, Copilot CLI, OpenCode, OpenClaw, Trae, Pi, Codex — was registered dead the
  same way, along with three configs committed in this repo.

- **`doctor` reported 0 issues while the server was dead.** Every MCP check asked only
  whether the key `llm_router` was present in `mcpServers`; none read the command back.
  It now verifies the registered command exists, is executable, and — for
  `uv run --directory DIR` — that `DIR` is a real project root.

- **Both IDE config templates were invalid JSON.** `localize()` rewrites tool names to the
  1.0 surface (`llm_code` → `llm(task="code")`) and was running that substitution over a
  raw JSON document, injecting unescaped quotes into the `"description"` string. Since the
  templates are written verbatim, `install --ide` produced `.vscode/mcp.json` and
  `.windsurf/mcp.json` that no IDE could parse. Both are now built with `json.dumps`.

### The security disclosure (#43)

- **`LLM_ROUTER_DIRECT_EXECUTION` is now documented in README.md.** #36 was closed by
  writing it up in `SECURITY.md`, which the sdist excludes — so for anyone installing from
  PyPI the entire disclosure was invisible: a default-on feature handing a local model
  `write_file`/`edit_file`/`run_command` unsupervised, with no shipped text naming it or
  its off switch. A test now asserts the disclosure survives into the built artifacts.

### Install/uninstall symmetry (#42)

The two behaviours reported already had fixes in the 13.0.2 tag, and the published sdist
is byte-identical to that tag, so the report could not be reproduced as written. Asserting
the real contract — install then uninstall is a no-op on every config file — surfaced four
genuine defects instead:

- **An unguarded `unlink()` aborted uninstall partway through.** One `OSError` in the
  hook-removal loop (or on the rules file) raised straight out of `uninstall()`, so the
  statusLine restore and Claude Desktop deregistration — both later in the function —
  silently never ran. This is the most likely root cause of #42.
- **An empty `hooks` scaffold was left behind** for users who had no `hooks` section.
- **`~/.claude.json` was left as a `{"mcpServers": {}}` husk.** It is now recorded as
  `created_file` and removed only when install is the sole reason the file exists.
- **`settings.json.bak` held POST-install state**, so restoring it reinstated the very
  hooks a user was trying to remove. The snapshot is now taken before the first mutation.

### Packaging

- **`_quarantined_tests/` no longer ships to PyPI.** The exclude list anchored `/tests/`;
  the quarantine lives at the repo root, so 13.0.2 shipped 16 files of dead test code.

### Fixed

- **`KIMI.md` gained a duplicate routing block on every install.** The idempotence guard
  tested for `llm_router`, a token the block it writes never contains.

## [13.0.2] — Document what `LLM_ROUTER_DIRECT_EXECUTION` actually grants (2026-08-19)

Documentation and a test. No behaviour change.

### Security documentation

- **SECURITY.md now covers `LLM_ROUTER_DIRECT_EXECUTION`** (#36). It is **default-on**,
  and with it enabled the routing hook runs a tool-calling agent loop that hands a local,
  uncurated model `write_file`, `edit_file` and `run_command` — the last as an arbitrary
  shell string via `subprocess.run(..., shell=True)` — unsupervised, no confirmation, up
  to 15 iterations, before Claude ever sees the prompt.

  The section states what is enforced (file operations are confined to the project root)
  and what is not, with the blocklist coverage **measured against 13.0.1**: of twelve
  representative commands, **three** are blocked. `rm -rf ./src`,
  `git push --force`, `cat ../../.ssh/id_rsa`, `curl -d @.env` and
  `echo $OPENAI_API_KEY` all pass. The filter stops catastrophic *system* damage; it does
  not stop project damage, credential disclosure, or exfiltration.

  It also records that `agent_loop.py`'s docstring — *"All file operations are sandboxed
  to the project directory"* — is true of the file tools and false in effect, because
  `run_command` runs a shell string and `cat ../../.ssh/id_rsa` is not a "file operation"
  the sandbox sees.

  The entry documents the current state rather than changing it. Whether this should
  default to on is a real question and the section says so, but a default change needs
  its own decision and its own release note.

### Added

- A test re-derives the whole coverage table from the live regex. A table of
  measurements in a document rots silently — nothing fails, the file still reads
  plausibly, and the figure becomes a claim nobody re-checked. It now fails if the
  blocklist widens (so the doc stops understating its protection) or narrows.

## [13.0.1] — 13.0.0 shipped without `llm_router.agents` (2026-08-19)

**13.0.0 cannot start.** `import llm_router.server` fails with
`No module named 'llm_router.agents'`, so the MCP server exits before registering a
single tool and the client reports `CONNECTION_CLOSED` — indistinguishable from a network
fault, which is the same diagnostic dead end as #37.

### Fixed

- **Two unanchored exclusion patterns, in two different files.** Without a leading slash,
  both `.gitignore` and hatch's sdist `exclude` match a directory of that name at ANY
  depth:

  - `.gitignore`'s `agents/` and `Library/` also matched `src/llm_router/agents/` and
    `src/llm_router/library/`, so 11 source files existed locally and were never
    committed. Caught by CI, fixed before 13.0.0 was tagged.
  - `pyproject.toml`'s sdist `exclude = ["agents/", …]` also matched
    `src/llm_router/agents/`. **This one shipped.** `uv build` builds the wheel FROM the
    sdist, so the published wheel was missing the package as well.

  Both are now anchored, and a comment in each says nothing in that block may exclude
  anything under `src/`.

### Why every check passed

A local `uv build --wheel` builds straight from source and included the files, so the
wheel on this machine was correct while the published one was not. The pre-release suite,
the linters, the identity gate and CI all ran against the source tree, where nothing was
missing.

The only step that distinguishes "the release workflow succeeded" from "the artifact
works" is installing the published artifact and importing it. That is now the last step
of the release, not an optional afterthought.

## [13.0.0] — Upstream core sync: the routing engine, its guards, and four security fixes (2026-08-19)

The package is now built from the upstream routing core, rebranded, rather than from a
port of selected capabilities. 358 source files and 477 test files, ~4,956 identifiers.
`scripts/sync_downstream.py` upstream is the reviewable artifact — the sync is
reproducible, not a one-off copy.

**6,695 tests passing, 0 failing.**

### ⚠️ Breaking

- **`requires-python` is now `>=3.11`** (was `>=3.10`). The synced source uses 3.11-only
  stdlib. Leaving the floor at 3.10 would let a 3.10 user install code that cannot run —
  a silent failure rather than a resolver error.
- **`llm_router.audit_routing` is a different module.** It is now the live per-turn
  compliance log. The post-hoc misroute **scorer** that used to live there —
  `run_audit`, `score_decision`, `sample_unaudited_decisions`, `AuditedDecision` — moved
  to **`llm_router.misroute_audit`**, unchanged. Both features exist; they simply stop
  sharing a name. Update imports.

  The two shared a path across the repositories, with disjoint APIs, and a file-level
  copy in either direction would have deleted one of them in silence: no merge conflict,
  no import error, no failing test. Renaming is what makes that impossible.
- **`llm_router.observability` is a package, not a module.** Its OpenTelemetry layer is
  at `llm_router.observability.core` and re-exported from the package, so
  `observability.is_enabled()` and friends keep working. Direct imports of
  `llm_router.summary` and `llm_router.surface_status` are now
  `llm_router.observability.summary` / `.surface_status`.

### Security

- **The persistence redactor never shipped.** `persist_redact` lived under `enterprise/`,
  which is excluded from public distributions, and five write paths — result cache,
  semantic cache, idempotency, context, session store — imported it inside a `try/except`
  that fell back to a scrubber carrying none of its patterns. Measured against the
  published upstream package: JWTs, Slack tokens, emails, SSNs, phone numbers,
  credit-card numbers and prose secrets all reached disk verbatim, 7 of 7.

  The upstream suite was green throughout, including tests asserting exactly that those
  secrets never reach disk — they passed because the development tree *has* `enterprise/`.
  The control was only ever exercised in its strongest configuration. Fixed upstream and
  carried here.
- **SEC-002 layer 2 — path confinement — has landed.** `llm_fs_*` tools now reject paths
  resolving outside `project_root`. 12.0.1 shipped only the opt-in gate.
- **An optional tool group was a load-bearing import of the MCP server.** A build
  excluding `agoragentic` could not import at all — `ModuleNotFoundError` before a single
  tool registered, surfacing as `CONNECTION_CLOSED`, indistinguishable from a network
  fault. Same shape as the mcp 2.0 breakage in #37.
- **Reordering profiles resolved to no chain.** `SUBSCRIPTION_LOCAL` produced a
  one-model chain containing only the paid seat — no fallback, and the exact inverse of a
  profile whose purpose is preferring the free local bucket. `QUOTA_BALANCED` produced an
  empty chain on the two paths that exist to guarantee a non-empty one.

### Added

Whole subsystems from the upstream core, including the agentic engine, control plane,
policy runtime, semantic classification, quota and budget envelopes, the execution
ledger's realized-savings accounting, and the `misroute_audit` scorer with
`llm-router audit misroute`.

`config/` (model registry, agents, signals) and `scripts/` (the CI guards) are now
synced, so the checks that keep these fixes from regressing ship with the code.

### Not included, by design

`enterprise/`, `admin_api`, `invoice_reconciliation`, `tenant_policy_sidecar` and the
agoragentic marketplace/wallet tools. Tests covering them are skipped with that reason
rather than failing, so a red suite still means something is wrong.

## [12.0.1] — Fix the install breakage introduced by `mcp` 2.0.0 (2026-08-19)

Patch release, shipped alone and ahead of the next feature work, because 12.0.0 cannot be
installed fresh.

### Fixed

- **`mcp` was pinned `>=1.0.0` with no upper bound.** `mcp` 2.0.0 removed
  `mcp.server.fastmcp`, which seven modules here imported, so every fresh
  `pip install` / `pipx install` resolved 2.0.0 and died during import — before a single tool
  was registered. The client surfaced this as `CONNECTION_CLOSED`, which is what a network or
  provider fault also looks like, so there was no way to tell the two apart from the outside.
  The imports are ported to the 2.x API (`MCPServer`, `mcp.server.mcpserver`) and the pin is
  now `mcp>=2.0.0,<3.0.0`. (#37)
- **SECURITY.md claimed "Hooks cannot block core tools."** They can, and have since v13
  enforcement landed; the document contradicted the shipped behaviour. Rewritten to describe
  what the hook actually does. (#35)
- **`agoragentic_*` tools registered unconditionally at server startup.** They are now behind
  an explicit opt-in, so a routing install no longer exposes them by default. (#34)
- **Filesystem tools were ungated.** `llm_fs_*` now requires the same explicit opt-in.

### Added

- Two tests that keep this from recurring, deliberately not one:
  `test_pin_has_an_upper_bound` reads `pyproject.toml`, so a loosened pin fails in CI on the
  commit that loosens it; `test_every_mcp_import_in_src_resolves` parses `src/` for real
  `from mcp…` imports and checks each resolves against the *installed* package, so a partial
  port to a future major fails too. Neither subsumes the other — the first passes against a
  broken environment, the second passes against a dangerously loose pin that happens to
  resolve today.

## [12.0.0] — Chuzom capability wave: execution ledger, quality/fallback split, capability-aware shadow routing, budget envelope, misroute audit + CLI (2026-08-02)

> **Version note:** released as a major bump (v11.0.0 → v12.0.0) to mark the scale of the Chuzom capability wave, even though every routing-affecting behavior ships default-off or shadow-only.

Ports eight capabilities adapted from an internal reference implementation ("Chuzom") into llm-router. Everything here is additive; every new routing-affecting behavior ships default-off or shadow-only, so existing installs behave identically until explicitly opted in.

### Added

- **Execution ledger + session store** (`execution_ledger.py`, `session_store.py`). Append-only SQLite ledger recording every route attempt (`execution_events`) via additive `ALTER TABLE` migrations onto the existing `usage.db`, with realized-savings gating (see below) and route/cost invariants. `session_store.py` adds a durable JSONL session-context log with cross-process advisory locking, TTL/size-triggered compaction, and privacy modes.
- **Quality/fallback split** (`routing_quality.py`, `bounded_operational.py`, `quality_feedback.py`). A schema-v2 route-quality ledger (`routing_quality.py`) with fail-open recording and a `summarize()` that never conflates verified/unverified or legacy-v1 rows into v2 metrics. `bounded_operational.py` adds a bounded-operational route predicate and pricing-derived budget, gated by `LLM_ROUTER_BOUNDED_OPERATIONAL` (default off). `quality_feedback.py` gains LoopHole ground-truth verdict ingestion (`record_loophole_verdict`/`ingest_loophole_jsonl`) feeding the existing heuristic quality store.
- **Realized-savings measurement + dashboard split** (`dashboard_data.py::query_realized_savings`, `dashboard/server.py`). Realization-gated savings accounting alongside the existing potential-savings columns: only attempts with `realization_status == "verified_used"` and an adoption method that counts as realized are counted, and the figure is never reconciled against or allowed to overwrite `usage.saved_usd`. Exposed additively as `/api/stats`'s `realized_savings` key, isolated in its own fail-open block.
- **Capability-aware routing (shadow mode)** (`capabilities.py`, wired into `router.py`/`cost.py`). An 8-bit capability detector records what capability-aware routing *would* choose into `routing_decisions.capabilities_json`, without changing any live routing decision. Gated by `LLM_ROUTER_CAPABILITY_ROUTING` (default off); live routing (`needs_claude_tools()`) stays byte-identical regardless of the flag.
- **Budget envelope** (`budget_envelope.py`). Standalone `BudgetEnvelopeManager` (register/reserve/release/commit/settle/tier-state, hierarchical ancestor accounting) gated by `LLM_ROUTER_BUDGET_ENVELOPE` (default off). Ships as an accounting primitive only — no router/cost wiring — so routing and spend behavior are unchanged with the flag off; `execution_ledger.py` remains the sole source of truth for realized spend.
- **Misroute audit** (`audit_routing.py`). A fully offline, post-hoc scorer over existing `routing_decisions` rows (heuristic over judge score / complexity downgrades / downshifts), writing back new `audit_verdict`/`audit_checked_at` columns idempotently. Gated by `LLM_ROUTER_AUDIT_DISABLED`; inert until explicitly invoked via `run_audit()` or the new `llm-router audit` CLI command (below).
- **Retrospective loop + team report enrichment**. Verified the existing retrospective debrief (`retrospective.py`, native since v6.x) reads the new `audit_verdict` directly rather than re-deriving misroutes, and fails open on the context fields it reads from the items above. `commands/team.py`'s report/push surfaces gain fleet-wide realized-savings and inferred misroute-rate columns, sourced from the realized-savings query and quality-ledger summary via a fail-open helper.

- **`llm-router audit` CLI command** (`commands/audit.py`). Wires `audit_routing.py::run_audit()` to a CLI entry point (mirrors the `team` command's structure): renders sampled/audited counts, verdict breakdown, and the inferred misroute-rate baseline, with a `--json` mode and a `--limit N` flag (default 100). Respects `LLM_ROUTER_AUDIT_DISABLED`. Strictly read-only/reporting — never mutates routing state.
- **Bounded-operational routing wired into the live path** (`router.py`), strictly behind `LLM_ROUTER_BOUNDED_OPERATIONAL` (default off). When the flag is unset/false, the routing decision path is byte-identical to before, proved by an invariance test comparing route decisions with the module absent vs. present-but-disabled.

### Config

New env vars (all optional, all default off / non-disabling): `LLM_ROUTER_BOUNDED_OPERATIONAL`, `LLM_ROUTER_LOOPHOLE_JSONL`, `LLM_ROUTER_CAPABILITY_ROUTING`, `LLM_ROUTER_BUDGET_ENVELOPE`, `LLM_ROUTER_AUDIT_DISABLED` (opt-*out* — unset means audits run when explicitly invoked).

### Notes

- There is no `LLM_ROUTER_QUALITY_FEEDBACK` flag, and none is planned. The heuristic quality scorer's `should_skip_model()` check in the router's fallback-chain path is unconditional — it pre-dates this release, is unrelated to the LoopHole-verdict additions above, and is already always-on in production. Gating it now would change existing behavior, so it intentionally stays ungated; this note exists only to correct an earlier reference to a flag that was never implemented.

## [11.0.0] — Adaptive routing wave: observability, importable classifier, subscription-local profile, PII→local (2026-07-09)

A wave of routing and observability capabilities, plus a docs restructure. Everything new is additive and off-by-default where it touches routing, so existing setups behave identically until opted in.

### Added

- **Cross-surface status indicator** (`llm_router.observability.surface_status`). A stdlib-only, fail-soft "router is working" signal for hosts without a native statusline: a compact status line (`⚡ llm-router · 🎯 hermes3:8b code/moderate · $0.03 · ✓`), an OSC terminal title, and a rate-limited OS notification, all derived from the shared savings log. Answers *is it active / what did it last route / is it healthy*.
- **Session-end summary** (`llm_router.observability.summary`). A content model over the existing `usage.db` with `render_markdown()` (CI / Claude Desktop / logs) and a rich `render()` (rich is optional; falls back to markdown): headline savings vs baseline, tier mix, per-provider cost, latency p50/p95/p99, outcomes, and top routes.
- **Importable deterministic classifier** (`llm_router.classify`). The hook's weighted intent×3 + topic×2 + format×1 scorer (`score_categories`, `classify_complexity`) is now an importable module with a `classify_signals() -> ClassifySignal` wrapper, so the router core, gateway, and MCP tools can classify at 0 cost/latency — previously only the UserPromptSubmit hook could. A drift-guard test keeps it byte-identical to the hook.
- **`SUBSCRIPTION_LOCAL` routing profile** (`llm_router.subscription_local_routing` + `RoutingProfile.SUBSCRIPTION_LOCAL`). Cost-inverted routing for the "one paid seat + free bucket" shape: free-first for simple/moderate, seat-first for complex, and the seat demoted to last when its quota is strained. Wired into `chain_builder.build_chain`; a complete no-op unless `LLM_ROUTER_SUBSCRIPTION_PROVIDER` is set. Quota-pressure source is a pluggable hook.
- **PII / secret signal with force-local routing** (`llm_router.signals`). `PiiSignal` detects API keys, tokens, JWTs, private keys, and `.env`-style secrets; `force_local_for_pii(chain, prompt)` filters a chain to local providers when a secret is present and is **fail-closed** (empty chain when no local model exists) so a secret is never dispatched to an external API. Evidence names the matched pattern, never the value.
- **`run_port_tests.sh`** — one-command runner for the new modules' tests.

### Changed

- **Docs restructure.** `docs/` is now gitignored (local working notes) except the CI-generated `docs/BENCHMARKS.md`. README media moved to `assets/readme/`, and public guide pages moved to `guide/` (Getting Started, Providers, Policies, Tools, Architecture, Troubleshooting, …). README and CHANGELOG links updated accordingly. **If you linked to `docs/*.md` externally, update to `guide/*.md`.**
- Version bumped to **11.0.0** to signal the new capability surface and the docs path change.

### Config

New env vars (all optional): `LLM_ROUTER_SUBSCRIPTION_PROVIDER`, `LLM_ROUTER_INTERNAL_PROVIDERS`, `LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD`, `LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES`, `LLM_ROUTER_STATE_DIR`, `LLM_ROUTER_INDICATOR`.

### Follow-ups (not yet wired)

Call `force_local_for_pii` in the dispatch path; wire `get_subscription_pressure` to a live quota source; repoint `hooks/auto-route.py` to import `classify.py` (removing its duplicate definitions).
