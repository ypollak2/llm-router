# Domain 12 — Documentation Claim Audit (llm-router, commit 3c96d23)

## Overview

README.md (605 lines) has clearly been through prior remediation rounds — most
of its risky claims already carry honest caveats (draft-vs-turn-replacement,
savings-as-counterfactual, direct-execution limits, RouterArena negative
results). The remaining problems are not marketing spin; they are **mechanical
drift**: numbers and host claims that were true when written and were never
re-checked against the code that moved past them. Every finding below is
reproducible from the commit in the worktree.

Five load-bearing numeric/behavioral claims were checked mechanically by
importing the actual registration code (not by reading docstrings):

| Claim (README/guide) | Location | Mechanically measured | Status |
|---|---|---|---|
| "60 tools" total MCP surface | README.md:359, guide/TOOLS.md:3, guide/QUICKSTART_2MIN.md:168 | **70** registered under `LLM_ROUTER_SLIM=off` (74 with `LLM_ROUTER_AGORAGENTIC=on`) | **STALE** |
| "11 front-door tools" under default `consolidated` tier | README.md:360, guide/TOOLS.md:5, guide/PROVIDERS.md:190 | **12** (`llm_local_task` is registered but never named in either list) | **MISLEADING** |
| "Codex CLI: Manual MCP tools · hooks 🔜" (no auto-routing yet) | README.md:213, guide/HOST_SUPPORT_MATRIX.md:9 | Installer (`src/llm_router/commands/install.py:639-645,695`) copies the **same** `auto-route.py` used for Claude Code to `~/.codex/hooks/codex-auto-route.py` and registers it on Codex's `UserPromptSubmit` — full automatic routing is shipped, matching the *same document's own* detailed table row 36 ("Codex CLI | UserPromptSubmit | yes | **shipped**") | **MISLEADING / internally contradictory** |
| "Run tests (1900+)" | README.md:581 | Baseline suite run (`audit/forensic_2026-09-24/00_baseline_pytest.log`): **9,640 tests**, 0 failed, 0 errors, 200 skipped | **STALE** (off by >5x) |
| "20+ providers" | README.md:332 | `config.py:611` `_PROVIDER_MAP` has 21 keyed providers + `ollama` (probed) + `openai_compat` (conditional) = **23** | **VERIFIED** (slightly conservative, if anything) |

The tool-count and Codex-hook errors are the two most consequential findings in
this domain: the tool count is repeated identically across three separate docs
(so it reads as corroborated, when all three copies share one stale origin),
and the Codex claim actively points a user at the wrong integration model for
the second-most-prominent host in the "Works With" table.

---

## §41 README Claim Ledger

| Claim | README location | Code evidence | Test evidence | Measured evidence | Status |
|---|---|---|---|---|---|
| "No API keys" / "works with zero API keys" on Claude subscription | L14-16, L131-132, L184 | `available_providers()` (config.py:649) gates external providers on key presence; MCP/hook path routes via `llm_router` tools + Ollama with none set | Install/doctor tests exist (not exhaustively checked here) | Consistent with `LLM_ROUTER_DIRECT_EXECUTION`/hook design | VERIFIED (mechanism exists); caveat below |
| "the draft is injected... as an unverified hint... Claude still takes the turn" (default mode disclaimer) | L20-25 | Matches `LLM_ROUTER_DIRECT_EXECUTION` section further down (L432-474): default write mode is `propose`, not applied | n/a | Points to `docs/MEASUREMENT.md` for n/window — that file contains measurement *methodology*, not the actual draft-rate/acceptance numbers (see DOC-07) | PARTIALLY VERIFIED — the disclaimer is honest, but its own citation doesn't carry the promised numbers |
| "60 tools ... exposed to any MCP host" | L359 | Actual: 70 (off tier), 74 with agoragentic flag. See table above | n/a | Mechanically counted via `mcp.tool()(` registrations across `src/llm_router/tools/*.py`, cross-checked by importing `register()` with a fake MCP object | **STALE** |
| "default consolidated surface shows 11 front-door tools" | L360-361, guide/TOOLS.md:5 | `tool_surface.CONSOLIDATED_TOOLS` = 12 members (`llm, llm_act, llm_audio, llm_edit, llm_image, llm_local_task, llm_route, llm_router_admin, llm_router_agent_route, llm_router_agent_start_session, llm_router_session, llm_router_status`) | n/a | `llm_local_task` confirmed registered by default via `local_task.py:298-300` (`should_register("llm_local_task")` → True under consolidated gate) and is the live MCP tool in this very session's tool list | **MISLEADING** — undercounts by 1, and the omitted tool (`llm_local_task`, whole-task local execution with write/edit/run_command) is exactly the kind of capability a user would want to know is on by default |
| "set `LLM_ROUTER_SLIM=full` for all 60" | README.md:361 | `"full"` is not a recognized tier (`_TIERS` keys are `core/routing/consolidated/off` only, `tool_surface.py:255-260`); `registered_tools()` does `_TIERS.get(tier, None)` so an unrecognized string **happens** to fall through to "register everything" — functionally works, but not because "full" is documented anywhere in code | n/a | Confirmed by reading `tool_surface.py:279-293` and `tool_tiers.py:38-57` | **UNSUPPORTED value that works by accident** — the documented/valid value is `off`, not `full`; a future tightening of the fallback (e.g. raising on unknown tier) would silently break this documented instruction |
| "Works With" table: Codex CLI "Manual MCP tools · hooks 🔜" | L213 | `install.py:639-645,695` wires `UserPromptSubmit` → `codex-auto-route.py` (a copy of the shared `auto-route.py`) automatically on `llm-router install --host codex` | n/a | guide/HOST_SUPPORT_MATRIX.md's own detailed table (line 36) says Codex is "shipped" — contradicts its own summary table (line 9: "🔜 Not yet") and README's copy of that summary | **MISLEADING / internally contradictory** |
| "Works With" table: Cursor/VS Code "Manual MCP tools · hooks 🔜" | L215 | `install.py:1465` `_install_cursor_files()` only writes `~/.cursor/mcp.json` and a rules markdown file — no `beforeSubmitPrompt` hook wiring found | n/a | Confirmed genuinely MCP-only | VERIFIED |
| "Works With" savings column: 60–80% / 30–50% / 50–70% per host | L212-216, repeated in guide/HOST_SUPPORT_MATRIX.md (8+ places) | No per-host measurement artifact found; `docs/MEASUREMENT.md` (the file the README's own savings section leans on for rigor) never ties these ranges to an n/window/host | n/a | Contrast with the README's *own* nearby disclaimer for the "35–80%"/"87%" figures ("single-user observations... no stated denominator... read as anecdotes") — that disclaimer is NOT attached to this table, even though the same standard should apply | **UNSUPPORTED / inconsistently caveated** — the identical 30–50% figure appears for both Codex and VS Code/Cursor, two structurally different integrations, suggesting an estimate rather than a per-host measurement |
| Savings 35–80% / 87% figures | L391-396 | N/A (explicitly framed as anecdote) | n/a | Self-disclosed as "single-user observations... with no stated denominator" | VERIFIED AS OBSERVATIONAL (README already does this correctly — model example of how the "Works With" table above should read) |
| "343 OpenRouter models" | L181 | No occurrence of `343` anywhere else in the repo (code, config, docs) | n/a | Third-party catalog size, not reproducible from this repo, no refresh mechanism found | **STALE-RISK / UNVERIFIABLE** — a hardcoded point-in-time snapshot of someone else's catalog with nothing keeping it current |
| "20+ providers, free-first" | L332 | `config.py:611-632` `_PROVIDER_MAP` (21 entries) + `ollama` + `openai_compat` = 23 | n/a | Mechanically counted | VERIFIED |
| guide/PROVIDERS.md "Every provider... " (implies completeness) | README L337 | `_PROVIDER_MAP` includes `moonshot`, `minimax`, `zhipu`, `arcee` — none of these four appear anywhere in guide/PROVIDERS.md, README, or docs/ | n/a | `grep -in "moonshot\|minimax\|zhipu\|arcee"` across README/guide/docs returns nothing | **UNDOCUMENTED** — 4 of 23 implemented providers (17%) have zero user-facing documentation |
| "Automatic fallback with circuit breakers" | L297-298 | `health.py` (`HealthTracker`, circuit breaker pattern), `server.py:106-115` resets stale breakers on startup, `classifier.py:195` avoids providers with open breaker | n/a | Implementation confirmed present and wired at three call sites | VERIFIED (implementation exists; did not independently verify trip/reset thresholds match documented behavior — out of scope for this domain, flagged for §13 routing-engine domain) |
| "Secrets never leave your machine... fail-closed" | L292-293 | Consistent with SECURITY.md's stated scrubbing (not independently re-verified here; §31/§32 domains own this) | n/a | n/a | NOT INDEPENDENTLY VERIFIED IN THIS PASS — cross-ref to Security/Privacy domain auditors |
| RouterArena "4.25 points" proxy-split miss | L158 | Matches `docs/ROUTERARENA.md:101-102` verbatim claim | n/a | Consistent across both documents | VERIFIED (internally consistent, evidence lives in cited doc) |
| "22,356 routing records produced zero evaluable examples" (Ground Truth section) | L508-510 | Not independently re-derived in this pass (would require DB inspection — out of scope/time for this pass; flagged) | n/a | n/a | NOT INDEPENDENTLY VERIFIED — plausible given the surrounding honest framing, but the exact number needs a query against `~/.llm-router/usage.db` (read-only) to confirm, which a data/telemetry-domain auditor should run |
| CLI command list (`llm-router install/status/gain/doctor/okf index/okf status/sessions status`) | L311-319 | Not exhaustively checked against CLI dispatch table in this pass | n/a | n/a | NOT INDEPENDENTLY VERIFIED — recommend CLI-domain auditor cross-check `--help` output against this list |
| "60 tools across routing, analysis, code, media, budget and diagnostics" (MCP Tools section header) | L359 | Same as tool-count finding above | n/a | Same | STALE (duplicate of the finding above, different location) |

---

## §44 Docs topic map (tracked vs local, contradictions, canonical owner)

`docs/` is git-ignored by policy (`.gitignore:111` `/docs/*`) with an explicit
allow-list of exceptions. Verified via `git ls-files docs/`:

**Tracked (canonical, public) under `docs/`:**
`BACKEND-QUALITY.md`, `BACKLOG.md`, `BACKLOG_NEXT.md`, `BENCHMARKS.md`,
`GROUNDING_API.md`, `MEASUREMENT.md`, `PLAN_CLOSE_THE_GAP.md`,
`PROPOSAL_LOCAL_EXECUTION.md`, `RESEARCH_LANDSCAPE.md`,
`RESEARCH_SEMANTIC_LAYER.md`, `ROUTERARENA.md`, `security_command_matrix.txt`,
`decisions/0001-package-name.md`, `decisions/0002-semantic-layer.md`,
`measurements/*.md,*.json` (9 files), `releases/v11.1.0-chuzom-migration-evidence.md`,
`archive/*.md` (9 files, including `PLAN_LOCAL_EXECUTION_ROADMAP.md`,
`PLAN_ROUTING_OBSERVABILITY.md`, `PLAN_SAVINGS_ATTRIBUTION.md`).

**Minor hygiene finding:** `.gitignore` lines 146-148 whitelist
`!/docs/PLAN_SAVINGS_ATTRIBUTION.md`, `!/docs/PLAN_ROUTING_OBSERVABILITY.md`,
`!/docs/PLAN_LOCAL_EXECUTION_ROADMAP.md` at the **root** of `docs/`, but the
actual tracked files now live under `docs/archive/`. The root-level exceptions
are dead rules (no file exists at that path to un-ignore) — harmless but stale,
left over from when those files moved into `archive/`. Low severity; belongs on
the hygiene ledger, not a doc-claim problem.

**Topic ownership map (who is the canonical doc per topic):**

| Topic | Canonical owner | Secondary mentions | Contradiction found? |
|---|---|---|---|
| Tool inventory / MCP surface | guide/TOOLS.md | README §MCP Tools, guide/QUICKSTART_2MIN.md | Yes — all three share the same stale "60"/"11" numbers (one origin, three copies) |
| Host support | guide/HOST_SUPPORT_MATRIX.md | README §Works With | Yes — Codex row contradicts itself within HOST_SUPPORT_MATRIX.md (summary table vs detailed table), and README inherits the wrong summary |
| Providers | guide/PROVIDERS.md | README §Providers | Undercounts (17 documented vs 23 implemented); no direct contradiction, just a gap |
| RouterArena / benchmark methodology | docs/ROUTERARENA.md | README §On the RouterArena leaderboard | Consistent |
| Savings methodology | README §Savings: How It Works | docs/MEASUREMENT.md | Partial gap — MEASUREMENT.md documents *pitfalls in measuring*, not the actual current draft-rate/acceptance/per-host numbers the README's top banner promises are "in docs/MEASUREMENT.md with their n, window and conditions" (L24-25) |
| Direct execution / agent safety | README §Direct Execution section | SECURITY.md | Not independently cross-checked in this pass (Security domain owns verification of the technical claims; the doc-consistency check found no contradiction in framing) |
| Ground Truth | README §Ground Truth accumulation | guide/GROUND_TRUTH.md | Not independently verified (see ledger row above) |
| Architecture | guide/ARCHITECTURE.md | architecture/CURRENT_ARCHITECTURE.md, architecture/TARGET_ARCHITECTURE.md | Two documents with overlapping scope (current vs target) — this is a §44 candidate for consolidation but is really the Architecture domain's call; flagged here only because README points to guide/ARCHITECTURE.md as *the* architecture doc while a much larger architecture/ directory exists and isn't linked from README at all |
| CLI reference | README §CLI (partial) + guide/GETTING_STARTED.md ("full command reference") | — | Not independently verified against actual CLI dispatch table in this pass |

`architecture/` (16 files: ARCHITECTURE_CHALLENGE, CURRENT_ARCHITECTURE,
EVALUATION_PLAN, GAP_ANALYSIS, IMPLEMENTATION_PLAN, KNOWLEDGE_MODEL,
LEARNING_SYSTEM, OBSERVABILITY, README, ROUTING_MODEL, TARGET_ARCHITECTURE,
TASK_GRAPH_COMPILER, TEST_PLAN, TOKEN_EFFICIENCY) is never linked from
README.md at all — it is invisible to a normal reader who only follows the
README's "Documentation" table. Whether that's intentional (internal
design-process docs, not user docs) or an oversight is a judgment call for
synthesis, but as written a user cannot discover it exists.

`audit/` (this very forensic tree plus ~25 prior audit rounds) is entirely
internal-process documentation and correctly not referenced from README.

---

## §45 Feature matrix (sample — full matrix belongs to synthesis, not restated per-line here)

| Capability | Claimed (README) | Implemented | Tested (this pass, spot-check only) | Documented | Recommended status |
|---|---|---|---|---|---|
| Zero-API-key routing on Claude subscription | Yes | Yes (MCP + hooks + Ollama) | Not independently run | README + guide/GETTING_STARTED.md | Keep claim, already accurate |
| Automatic routing on Codex CLI | **No** (README says manual-only) | **Yes** (installer wires `UserPromptSubmit`) | Not independently run | Contradicted between README/HOST_SUPPORT_MATRIX summary and HOST_SUPPORT_MATRIX detail | **Fix claim** — this understates a shipped capability |
| 60-tool MCP surface | Yes, exactly "60" | 70 (74 with agoragentic) | N/A (mechanical count) | guide/TOOLS.md (stale), README (stale) | **Fix number**, and add `llm_local_task` to the documented default-tier list |
| `LLM_ROUTER_SLIM=full` | Documented as the way to unlock everything | Works, but "full" isn't a real tier name (falls through the `.get(..., None)` default) | N/A | README only | **Fix to the real value `off`**, or make "full" a first-class alias in code so the doc and code agree either way |
| 23 providers | "20+" | 23 | N/A | 17 of 23 documented in guide/PROVIDERS.md | Fix — either document `moonshot/minimax/zhipu/arcee`, or if they're experimental/undermaintained, say so and consider the Deletion Ledger |
| Circuit breaker fallback | Yes | Yes (`health.py`) | Not independently run | README (brief), no dedicated guide doc found | Sufficient as-is |
| RouterArena methodology | Detailed, self-critical | N/A (external benchmark) | N/A | docs/ROUTERARENA.md | Model example — no change needed |
| Ground Truth accumulation numbers (22,356 records) | Specific, falsifiable number | Not independently re-derived | Not run | guide/GROUND_TRUTH.md | Needs one query against `~/.llm-router/usage.db` (read-only) by a telemetry-domain auditor before synthesis treats it as fact |

---

## §39 Comments/docstrings promising unsupported or contradicted behavior (sample)

1. **`src/llm_router/tool_tiers.py:1-16` docstring vs. actual default.**
   Docstring states: `off — all tools registered (default, backward-compatible)`
   and `make_should_register`'s own fallback (`slim = (slim or "off")`, line 48)
   treats an absent/`None` value as `"off"`. But the *actual* system default,
   set in `config.py:473` (`llm_router_slim: str = "consolidated"`) and
   `tool_surface.py:282` (`os.environ.get("LLM_ROUTER_SLIM") or "consolidated"`),
   is `consolidated`, not `off`. As long as every call site passes
   `get_config().llm_router_slim` (which it does, per `server.py:181-197`) this
   is harmless in practice, but the comment actively documents the wrong
   default, and any future caller that does `make_should_register(None)`
   directly (bypassing config) would silently register the full 70-tool
   surface instead of the intended 12. `tool_surface.py:279` even has its own
   comment warning about exactly this class of mistake ("Getting this default
   wrong is...") for a sibling function — the warning wasn't applied
   consistently to `tool_tiers.py`.

2. **`tool_tiers.py:9-10` ("Three tiers... routing — 12 core routing + admin
   tools", "core — 4 essential tools").** Mechanically, `ROUTING_TOOLS` has 17
   members (not 12) and `CORE_TOOLS` has 4 (matches). The "12" figure for the
   routing tier is stale relative to `tool_surface.py`'s own frozensets it
   re-exports.

3. **`server.py:1-3` docstring ("All 60 tools are registered by modules
   in...")** — same stale count as the README, but this is the primary-source
   comment the README claim likely traces back to. If synthesis fixes only the
   README, the code comment will keep reintroducing the same wrong number the
   next time someone copies from it.

No instances were found in this pass of a comment promising a security
guarantee the code doesn't provide (that class of finding belongs to the
Security domain, which should independently verify the `LLM_ROUTER_DIRECT_EXECUTION`
comments in README against `agent_writes.py`/`direct_executor.py`).

---

## §68-69 Skeptical-engineer test on headline sentences

For each headline claim, would a careful engineer's actual first-run experience match the sentence?

| Headline sentence | Would experience match? | Why / why not |
|---|---|---|
| "No API keys. No change to how you work." | Mostly yes | True for Claude Code; **not fully true for Codex**, where the README implies a manual-tool workflow but the installer actually changes the workflow automatically (silently, in the good sense) — the surprise here is pleasant but still a mismatch between promise and delivery |
| "60 tools across routing, analysis, code, media, budget and diagnostics" | **No** | Running `LLM_ROUTER_SLIM=off` and counting registered tools yields 70. A user who counts (or who reads `guide/TOOLS.md`'s own per-section entries) will find the header number doesn't add up, and will reasonably wonder what else in the doc is uncounted |
| "the default consolidated surface shows 11 front-door tools" | **No** | It's 12; the 12th (`llm_local_task`) is also the single most powerful one (write/edit/run_command access), which makes the omission worse than an off-by-one clerical slip |
| "Codex CLI: Manual MCP tools · hooks 🔜" | **No** | A Codex user running `llm-router install --host codex` gets full automatic UserPromptSubmit routing today, not "coming soon" |
| "Run tests (1900+)" | **No** | 9,640 tests exist; a contributor who reads this before running `pytest -q` will be confused by a 5x-longer run than promised |
| "20+ providers" | Yes | 23, so "20+" reads conservative-but-true |
| "It works with zero API keys" | Yes | Verified mechanism |
| "Savings...should be read as anecdotes rather than a range you can expect" (Savings section) | Yes | Exactly the standard of honesty this whole README should hold to — and the "Works With" table two screens earlier does not meet that same standard for its per-host percentages |

---

## §64 Top 10 documentation problems (this domain's candidates for the global list)

1. **Tool-count claim ("60") is stale by 10-14 tools**, repeated identically
   in 3 files (README.md:359, guide/TOOLS.md:3, guide/QUICKSTART_2MIN.md:168) —
   one wrong number, three places it looks corroborated. Root cause traces to
   `server.py`'s own docstring, which should be fixed first so the wrong number
   doesn't keep propagating.
2. **Codex auto-routing claim is flatly wrong** and self-contradicts within
   guide/HOST_SUPPORT_MATRIX.md (summary table says "🔜 Not yet", detail table
   says "shipped" three lines of markdown later). This is the single highest-impact
   fix: it misdirects Codex users toward a manual workflow they don't need.
3. **Default consolidated tool count is 12, documented everywhere as 11**,
   and the omitted tool is the write/edit/run_command-capable one — worth
   flagging because a security-conscious reader specifically counting "what's
   on by default" gets the wrong list.
4. **`LLM_ROUTER_SLIM=full` is not a real tier name.** It happens to work via
   a permissive fallback, which is fragile: the moment someone tightens
   `registered_tools()`'s unknown-tier handling (a very natural refactor), this
   documented instruction silently breaks with no test currently pinning it.
5. **"Run tests (1900+)" undercounts the real suite by >5x** (actual: 9,640
   per the audit's own clean baseline run) — a small thing, but exactly the
   kind of unverified number the project's own `docs/MEASUREMENT.md` culture
   says not to publish.
6. **"Works With" per-host savings percentages (60-80/30-50/50-70%) have no
   n/window/methodology**, unlike the README's own savings-section numbers a
   few screens down, which do carry that caveat. Identical inconsistency
   repeated 10+ times across guide/HOST_SUPPORT_MATRIX.md.
7. **4 of 23 implemented providers (moonshot, minimax, zhipu, arcee) are
   completely undocumented** — not in README, guide/PROVIDERS.md, or docs/.
   A user with a Moonshot key has no way to discover it's supported except by
   reading `config.py`.
8. **`architecture/` (16 files) is entirely unlinked from README**, making a
   substantial design-history document set undiscoverable to a normal user
   (may be intentional — internal-only — but nothing states that).
9. **A hardcoded third-party number ("343 OpenRouter models") with zero
   provenance or refresh path** — will silently go stale as OpenRouter's
   catalog changes, and nothing in the repo would catch it.
10. **`.gitignore`'s docs/ allow-list references 3 filenames at a path
    (`docs/PLAN_*`) that no longer exists** (files moved to `docs/archive/`) —
    low severity, but it's exactly the kind of drift this repo's own culture
    (see `docs/MEASUREMENT.md`) says to catch by checking the artifact, not by
    trusting the rule that was written about it once.

---

## §42 What must stay in README vs move (informing, not writing, the rewrite)

**Keep in README as-is (already at the right altitude, evidence-backed):**
- The "why a proxy cannot do this" framing and the pays-per-token vs subscription table.
- The default-mode disclaimer banner (L19-26) — this is the standard every other numeric claim in the doc should be held to.
- The Savings section's counterfactual/anecdote framing (L391-396).
- The `LLM_ROUTER_DIRECT_EXECUTION` section's specific, measured claims about what the blocklist does and does not stop (L432-458) — this is genuinely excellent, specific, falsifiable documentation and should be a template for fixing the "Works With" table.

**Move out of README (belongs in a linked doc, not the landing page):**
- The per-host savings percentages in the "Works With" table should either
  gain the same anecdote caveat inline, or be removed from the table entirely
  and left to guide/HOST_SUPPORT_MATRIX.md with the caveat attached there
  (once that document's own internal contradiction on Codex is fixed).
- The exact tool count/front-door count numbers should not be hardcoded in
  three files; they belong in exactly one place (`guide/TOOLS.md`, since it's
  the most detailed), generated or checked by a test that fails when
  `tool_surface.py`'s frozensets change size (there is currently no such test
  found in this pass — recommend one to the Testing domain).

**Fix before anything else moves:** the Codex row, because it currently sends
users to the wrong workflow, and the tool-count trio, because they compound
(three files that look independently corroborated but share one stale source).

---

## Findings register (§66 format)

### DOC-01
- **Category:** README claim accuracy — tool count
- **Severity:** MEDIUM
- **Confidence:** HIGH (mechanically reproduced)
- **Location:** Files: `README.md:359`, `guide/TOOLS.md:3`, `guide/QUICKSTART_2MIN.md:168`, `src/llm_router/server.py:1-3` (source of the number). Symbols: n/a. Lines: as listed.
- **Observation:** All four locations state the MCP tool surface is "60 tools". Importing every `tools/*.register()` function with a fake MCP object and `LLM_ROUTER_SLIM=off` registers 70 distinct tool names (74 if `LLM_ROUTER_AGORAGENTIC=on` is also set).
- **Evidence:** Reproduced via: `HOME=$(mktemp -d) PYTHONPATH=.../src python -c "..."` importing `routing, text, media, pipeline, admin, subscription, codex, gemini_cli, setup, dashboard, fs, agents, agentic, local_task, consolidated` and calling `.register(fake_mcp, None)` on each; counted 70 unique names, no duplicates. `tool_surface.KNOWN_TOOLS` (a curated subset used for name-resolution, not a total) is 41 — a third, different number that also doesn't match, showing the drift has compounded over at least two rewrites.
- **Why this exists, if discoverable:** The tool surface clearly grew (new tool files: `agoragentic.py`, `local_task.py`, `agents.py`'s dynamic loop, several admin/dashboard additions) after the "60" figure was written into `server.py`'s docstring, and every doc that cites the number copied from that one docstring rather than counting.
- **Why this matters:** Three files repeating the same wrong number look like independent corroboration to a reader; fixing only one leaves the other two as re-infection vectors.
- **User-visible impact:** Low-moderate — a user auditing the tool surface (e.g. for a security review of what's exposed) undercounts by ~15%.
- **Engineering impact:** None functionally; purely a documentation-drift indicator with no test pinning the number.
- **Is behavior currently used? YES** (the tools are registered and used) — only the *documented count* is wrong, not the behavior.
- **Recommended action:** SIMPLIFY/REWRITE — state the count once (ideally computed, e.g. `len(KNOWN_TOOLS_active_under_off)` printed by a doc-generation step or CI check) rather than hardcoded in 3 places.
- **Proposed target:** Single source of truth in `guide/TOOLS.md`, README links to it without repeating the number, or both drop the exact count in favor of "70+ tools, 12 by default" language that degrades gracefully as the surface grows.
- **Behavioral compatibility risk:** None (docs-only).
- **Security risk:** None directly, though an undercount could lull a security reviewer into checking less surface than actually exists.
- **Performance impact:** None.
- **Estimated complexity removed:** N/A (docs).
- **Validation required:** A test asserting `len(tool_surface._TIERS["off"] or <computed full set>) == <documented number>` would catch future drift; none found in this pass.
- **Dependencies on other findings:** DOC-02 (front-door count), DOC-08 (comment source of the number).

### DOC-02
- **Category:** README claim accuracy — default tool surface undercount
- **Severity:** MEDIUM
- **Confidence:** HIGH
- **Location:** Files: `README.md:360-361`, `guide/TOOLS.md:5-9`, `guide/PROVIDERS.md:190`. Symbols: `tool_surface.CONSOLIDATED_TOOLS`, `local_task.register`. Lines: `tool_surface.py` (CONSOLIDATED_TOOLS definition), `local_task.py:298-300`.
- **Observation:** All three docs enumerate the default (`consolidated`) tier as exactly 11 named tools. `tool_surface.CONSOLIDATED_TOOLS` has 12 members; the extra one, `llm_local_task`, is confirmed registered by default (its own `register()` checks `should_register("llm_local_task")`, which is `True` under the consolidated gate) and is visible as a live tool in an active MCP session against this exact codebase.
- **Evidence:** `python -c "from llm_router import tool_surface as ts; print(sorted(ts.CONSOLIDATED_TOOLS))"` → 12 names including `llm_local_task`; `guide/TOOLS.md:5-8`'s enumerated list of "only 11 front-door tools" omits it.
- **Why this exists, if discoverable:** `llm_local_task` (whole-task local execution with write/edit/run_command tools) was likely added to the consolidated tier after the "11" enumeration was written and not backfilled into any of the three lists.
- **Why this matters:** This is the tool with the most agentic capability (file writes, command execution) among the default set — omitting it from "what's on by default" documentation is the kind of gap a security-conscious user specifically checks for.
- **User-visible impact:** Moderate — undercounts exactly the highest-risk default-on capability.
- **Engineering impact:** None functionally.
- **Is behavior currently used? YES**
- **Recommended action:** SIMPLIFY — add `llm_local_task` to all three enumerations, or restate as "12 front-door tools" (fix the number, not just the list).
- **Proposed target:** Single canonical enumeration in `guide/TOOLS.md`, cross-checked by a test against `tool_surface.CONSOLIDATED_TOOLS`.
- **Behavioral compatibility risk:** None (docs-only).
- **Security risk:** Indirect — undocumented default-on write/exec capability is a "hiding limitations" pattern the brief explicitly flags (§74).
- **Performance impact:** None.
- **Estimated complexity removed:** N/A.
- **Validation required:** Test asserting the documented list matches `sorted(CONSOLIDATED_TOOLS)`.
- **Dependencies on other findings:** DOC-01.

### DOC-03
- **Category:** README/guide claim accuracy — host capability, internally contradictory
- **Severity:** HIGH
- **Confidence:** HIGH
- **Location:** Files: `README.md:213`, `guide/HOST_SUPPORT_MATRIX.md:9` (summary table) vs `guide/HOST_SUPPORT_MATRIX.md:36` (detail table); code: `src/llm_router/commands/install.py:563,639-645,695`.
- **Observation:** README's "Works With" table and HOST_SUPPORT_MATRIX.md's own summary table both list Codex CLI as "Manual MCP tools · hooks 🔜" (not yet automatic). HOST_SUPPORT_MATRIX.md's own *detailed* table, 27 lines below its summary table, says for Codex: `UserPromptSubmit | yes — {"decision":"block"} | shipped`. Reading `install.py` confirms: `llm-router install --host codex` copies the shared `auto-route.py` (the exact file used for Claude Code's automatic routing) to `~/.codex/hooks/codex-auto-route.py` and registers it against Codex's `UserPromptSubmit` hook via `_ensure("UserPromptSubmit", None, _codex_hook_command(route_dst))`.
- **Evidence:** `install.py:639` `route_dst = hooks_dir / "codex-auto-route.py"`; `install.py:640` `route_src = pkg_hooks / "auto-route.py"`; `install.py:695` `_ensure("UserPromptSubmit", None, _codex_hook_command(route_dst))`. `guide/HOST_SUPPORT_MATRIX.md:36`: `| Codex CLI | \`UserPromptSubmit\` | yes — \`{"decision":"block"}\` | shipped (\`llm-router install\`; hook trust record written to config.toml) |`.
- **Why this exists, if discoverable:** The "🔜" emoji/summary table was likely accurate at some earlier point and the automatic-hook installer for Codex was shipped later; the detailed table was updated, the summary table (and README, which mirrors the summary) were not.
- **Why this matters:** This is the single clearest case in this domain of a user forming an incorrect mental model from the README: Codex is the second host listed, and a Codex user reading only the README (the documented entry point) would believe they need to call MCP tools manually, when installing already gives them the same automatic experience as Claude Code.
- **User-visible impact:** HIGH — directly misdirects a specific, common user path (Codex CLI users) toward unnecessary manual work.
- **Engineering impact:** None (no code fix needed — the code is correct; only the two summary tables are wrong).
- **Is behavior currently used? YES** — the Codex auto-route hook is fully wired by the installer.
- **Recommended action:** REWRITE (docs only) — change both summary-table rows (README + HOST_SUPPORT_MATRIX.md) from "🔜" to "Full auto-routing via hooks", matching the detailed table already present in the same file.
- **Proposed target:** One row, one truth — the summary table should be generated from (or at minimum unit-tested against) the detail table in the same document, since this file has already proven it can drift from itself.
- **Behavioral compatibility risk:** None (docs-only fix).
- **Security risk:** None.
- **Performance impact:** None.
- **Estimated complexity removed:** N/A.
- **Validation required:** None beyond the doc edit; recommend a lightweight doc-consistency check (grep for "🔜"/"shipped" pairs referring to the same host) added to the Documentation domain's do-not-regress list.
- **Dependencies on other findings:** None.

### DOC-04
- **Category:** README claim accuracy — test count
- **Severity:** LOW
- **Confidence:** HIGH
- **Location:** Files: `README.md:581`. Evidence file: `audit/forensic_2026-09-24/00_baseline_pytest.log`.
- **Observation:** README's Contributing quickstart says `uv run pytest tests/ -q # Run tests (1900+)`. The audit's own clean baseline run (HOME isolated, full suite) reports 9,640 tests collected, 0 failed, 0 errors, 200 skipped.
- **Evidence:** `audit/forensic_2026-09-24/00_baseline_pytest.log` (per the brief, already run and not to be re-run).
- **Why this exists, if discoverable:** Classic "stated once, never updated as the suite grew" drift; test suites grow continuously and comments citing an exact count age out quickly.
- **Why this matters:** Sets contributor expectations (run time, scope) 5x too low; not dangerous, just sloppy in a codebase whose own `docs/MEASUREMENT.md` explicitly warns against unverified numbers.
- **User-visible impact:** LOW — mildly surprises a first-time contributor.
- **Engineering impact:** None.
- **Is behavior currently used? N/A**
- **Recommended action:** SIMPLIFY — drop the exact number ("Run tests") or replace with a range that's cheap to keep roughly true ("thousands of tests").
- **Proposed target:** `uv run pytest tests/ -q  # full suite`
- **Behavioral compatibility risk:** None.
- **Security risk:** None.
- **Performance impact:** None.
- **Estimated complexity removed:** N/A.
- **Validation required:** None.
- **Dependencies on other findings:** None.

### DOC-05
- **Category:** Doc/code comment contradiction — default tier
- **Severity:** LOW
- **Confidence:** HIGH
- **Location:** Files: `src/llm_router/tool_tiers.py:1-16,38-57`; `src/llm_router/config.py:472-473`; `src/llm_router/tool_surface.py:279-282`.
- **Observation:** `tool_tiers.py`'s module docstring and `make_should_register`'s own fallback both document/implement "off" (register everything) as the default when no tier is given. The actual system default, sourced from `config.py:473` and independently re-affirmed in `tool_surface.py:282`, is `"consolidated"`. Every real call site passes the config value explicitly, so this is not currently a live bug, but the comment documents a default that contradicts the rest of the codebase's stated default.
- **Evidence:** `tool_tiers.py:48`: `slim = (slim or "off").strip().lower()`. `config.py:473`: `llm_router_slim: str = "consolidated"`. `tool_surface.py:282`: `return (os.environ.get("LLM_ROUTER_SLIM") or "consolidated").strip().lower()` with an adjacent comment: "the env var means `consolidated`, not `off`. Getting this default wrong is..." (i.e., this exact class of mistake is called out as dangerous one function away, but not fixed in the sibling module).
- **Why this exists, if discoverable:** `tool_tiers.py` predates the `tool_surface.py` refactor (per its own comment: "the tier membership sets moved to `llm_router.tool_surface`... This module remains the documented home for the *gate*"); the fallback default wasn't updated when the canonical default moved.
- **Why this matters:** A future caller (test, script, new hook) that constructs `make_should_register(None)` directly, bypassing `get_config()`, would silently get the full 70-tool surface instead of the intended 12 — the exact "wrong default" failure mode `tool_surface.py`'s own comment warns about, just in the file next door.
- **User-visible impact:** None today (no such caller found in this pass).
- **Engineering impact:** LOW-MEDIUM — a plausible future footgun given the pattern already caused one shipped incident, per `tool_surface.py`'s own history comment (the CHZ-SURF-01 incident description at the top of that file, about `auto-route.py` emitting unregistered tool names).
- **Is behavior currently used? UNCERTAIN** — no direct caller of `make_should_register(None)` found; all production call sites pass `get_config().llm_router_slim`.
- **Recommended action:** SIMPLIFY — change `tool_tiers.py`'s fallback default to `"consolidated"` to match the rest of the system, and fix its docstring.
- **Proposed target:** `slim = (slim or "consolidated").strip().lower()` plus corrected docstring.
- **Behavioral compatibility risk:** LOW — only changes behavior for a currently-unused call pattern (`None`/falsy input).
- **Security risk:** None currently exploited; closes a latent footgun.
- **Performance impact:** None.
- **Estimated complexity removed:** N/A.
- **Validation required:** Add a test asserting `make_should_register(None)` matches `make_should_register("consolidated")` (or explicitly documents why it shouldn't).
- **Dependencies on other findings:** None.

### DOC-06
- **Category:** README claim accuracy — provider documentation gap
- **Severity:** LOW
- **Confidence:** HIGH
- **Location:** Files: `guide/PROVIDERS.md` (entire document); `src/llm_router/config.py:611-632` (`_PROVIDER_MAP`).
- **Observation:** `_PROVIDER_MAP` defines 21 keyed providers (plus `ollama`, `openai_compat` handled separately) = 23 total. `guide/PROVIDERS.md`, which README links as "Every provider, its models, cost tier and env var," documents roughly 17-18 (Ollama, Gemini, OpenAI, Perplexity, Anthropic, OpenRouter, Groq, Deepseek, Mistral, Together AI, xAI, Cohere, fal.ai, Stability AI, Runway, Replicate, ElevenLabs, OpenAI TTS). `moonshot`, `minimax`, `zhipu`, `arcee` do not appear anywhere in README, guide/, or docs/.
- **Evidence:** `grep -in "moonshot\|minimax\|zhipu\|arcee" README.md guide/*.md docs/*.md` → no matches. `config.py:611-632` lists all four with their own config field and env var (`MOONSHOT_API_KEY`, `MINIMAX_API_KEY`, `ZHIPU_API_KEY`, `ARCEE_API_KEY`).
- **Why this exists, if discoverable:** These four providers were likely added to the config/key-management layer without a corresponding doc pass; `huggingface` also appears in `_PROVIDER_MAP`/`text_providers` and is similarly absent from the provider guide (a 5th gap, lower confidence since HF appears in the `_DISK_KEY_FILES` dict inconsistently — worth a second look by whoever owns this file next).
- **Why this matters:** README explicitly claims the linked doc covers "every provider" — a reader with a Moonshot/Minimax/Zhipu/Arcee key has no way to discover support exists except reading source.
- **User-visible impact:** LOW-MEDIUM — affects users of these specific (less mainstream, mostly China-market) providers.
- **Engineering impact:** None.
- **Is behavior currently used? YES** (the config plumbing is live) — UNCERTAIN whether these providers are exercised in the routing/model-selection path with the same completeness as the documented ones (out of scope for this domain; flag to Providers domain, §18).
- **Recommended action:** DEPRECATE-OR-DOCUMENT — either add these 4 providers to `guide/PROVIDERS.md`, or if they're experimental/unmaintained, say so explicitly (and consider whether they belong on the Deletion Ledger if genuinely unsupported).
- **Proposed target:** `guide/PROVIDERS.md` new "Additional / experimental providers" section, or full parity section per existing format.
- **Behavioral compatibility risk:** None (docs-only, unless paired with a code decision from the Providers domain).
- **Security risk:** None.
- **Performance impact:** None.
- **Estimated complexity removed:** N/A.
- **Validation required:** A test enumerating `_PROVIDER_MAP` keys and asserting each has a `guide/PROVIDERS.md` heading would catch this class of drift going forward.
- **Dependencies on other findings:** Providers domain (§18) should confirm these 4 are fully functional before deciding "document" vs "delete."

### DOC-07
- **Category:** README claim accuracy — unsupported/inconsistently-caveated numeric claims
- **Severity:** MEDIUM
- **Confidence:** MEDIUM
- **Location:** Files: `README.md:208-224` ("Works With" table and its savings column), `guide/HOST_SUPPORT_MATRIX.md` (10+ repetitions of the same ranges).
- **Observation:** The per-host savings ranges (60-80% Claude Code, 30-50% Codex/VS Code/Cursor, 50-70% Gemini CLI) are presented as a plain data table with no caveat. Three sections later, the README's own Savings methodology section explicitly disclaims comparable-looking numbers ("35-80%" and "87%") as "single-user observations... with no stated denominator... read as anecdotes rather than as a range you can expect." No equivalent caveat is attached to the "Works With" table, and no measurement artifact (docs/MEASUREMENT.md, docs/BENCHMARKS.md) was found tying these specific per-host ranges to an n/window/methodology.
- **Evidence:** `grep` for the percentage strings across README.md and guide/HOST_SUPPORT_MATRIX.md shows the same ranges repeated verbatim in 10+ places with no source citation in any of them; `docs/MEASUREMENT.md` (224 lines) is entirely about measurement pitfalls, not a record of these specific figures.
- **Why this exists, if discoverable:** Likely originated as rough per-integration-tier estimates (e.g. "manual tools probably save less than full hooks") formalized into a specific-looking numeric range without ever being measured per host.
- **Why this matters:** Per the brief's own standard (§17: "every % claim needs numerator, denominator, n, window, baseline, workload, mode, host, methodology; else OBSERVATIONAL"), these fail that bar, and — unlike the nearby caveated numbers — read as authoritative because of the clean table format.
- **User-visible impact:** MEDIUM — sets a specific expectation (e.g. "Codex saves 30-50%") a user might use to justify not adopting the Codex integration, with no evidence behind the number.
- **Engineering impact:** None.
- **Is behavior currently used? N/A** (claim, not behavior)
- **Recommended action:** REWRITE — either attach the same "read as anecdote" caveat used elsewhere in the same README, replace with qualitative language ("Full auto-routing saves more than manual tool calls in practice"), or (better, if the data exists) cite an actual measured split per host.
- **Proposed target:** README "Works With" table savings column either removed or footnoted with the existing anecdote disclaimer.
- **Behavioral compatibility risk:** None.
- **Security risk:** None.
- **Performance impact:** None.
- **Estimated complexity removed:** N/A.
- **Validation required:** None beyond editorial consistency check.
- **Dependencies on other findings:** None.

### DOC-08
- **Category:** Stale source-of-truth comment (root cause of DOC-01)
- **Severity:** LOW
- **Confidence:** HIGH
- **Location:** Files: `src/llm_router/server.py:1-20` (module docstring).
- **Observation:** The module docstring that documents which files register which tools opens with "All 60 tools are registered by modules in llm_router/tools/" — this is almost certainly the origin of the "60" figure copied into README.md and two guide docs (DOC-01). It is itself now stale relative to the modules it lists (which include `agoragentic.py`, `local_task.py`, `agents.py` — all present in its own bullet list, whose combined tool count already exceeds 60).
- **Evidence:** Same mechanical count as DOC-01 (70 actual vs. 60 claimed), sourced from the same file this docstring lives in.
- **Why this exists, if discoverable:** Docstring not updated as new tool modules were added to the same file's own registration block (`server.py:181-197`).
- **Why this matters:** Fixing README/guide without fixing this comment leaves the wrong number ready to be copied into the next doc that references "how many tools does this expose."
- **User-visible impact:** None directly (internal comment) but indirect via propagation into user docs.
- **Engineering impact:** None.
- **Is behavior currently used? N/A**
- **Recommended action:** SIMPLIFY — fix the docstring count alongside DOC-01, or better, remove the hardcoded number from the docstring entirely and describe the *modules*, not a count that will drift again.
- **Proposed target:** Docstring keeps the per-module tool list (useful, accurate) and drops the leading total-count sentence.
- **Behavioral compatibility risk:** None.
- **Security risk:** None.
- **Performance impact:** None.
- **Estimated complexity removed:** N/A.
- **Validation required:** None.
- **Dependencies on other findings:** DOC-01.

### DOC-09
- **Category:** Docs hygiene — dead `.gitignore` exceptions
- **Severity:** LOW
- **Confidence:** MEDIUM
- **Location:** Files: `.gitignore:146-148`.
- **Observation:** `.gitignore` whitelists `!/docs/PLAN_SAVINGS_ATTRIBUTION.md`, `!/docs/PLAN_ROUTING_OBSERVABILITY.md`, `!/docs/PLAN_LOCAL_EXECUTION_ROADMAP.md` at the root of `docs/`, but `git ls-files docs/` shows these three files now live under `docs/archive/` instead. The root-level negation patterns currently match nothing.
- **Evidence:** `git ls-files docs/` output vs `.gitignore` lines 146-148 content.
- **Why this exists, if discoverable:** Files were moved into `docs/archive/` at some point after the `.gitignore` exceptions were written for their original root-level location; the ignore rules were never updated to follow the move.
- **Why this matters:** Not currently harmful (git ignore rules that match nothing are silent no-ops), but it's exactly the class of small, self-inconsistent drift the brief's §37 hygiene pass is meant to catch, and it sits in the same file/area as the docs/ tracked-vs-ignored policy this domain had to reconstruct by hand.
- **User-visible impact:** None.
- **Engineering impact:** Negligible — mild confusion for the next person editing `.gitignore`'s docs/ section, who might reasonably (and wrongly) conclude these three files are meant to be at the root.
- **Is behavior currently used? NO** (the specific ignore-negation lines are currently no-ops)
- **Recommended action:** SIMPLIFY — update the three lines to `!/docs/archive/PLAN_SAVINGS_ATTRIBUTION.md` etc., or fold them into a single `!/docs/archive/` allow rule if the whole archive directory should be tracked (it already appears to be, per `git ls-files`).
- **Proposed target:** `.gitignore` docs/ section updated to match actual tracked paths.
- **Behavioral compatibility risk:** None.
- **Security risk:** None.
- **Performance impact:** None.
- **Estimated complexity removed:** N/A.
- **Validation required:** `git ls-files docs/` after the edit should show the same tracked set.
- **Dependencies on other findings:** None.

### DOC-10
- **Category:** Doc discoverability
- **Severity:** LOW
- **Confidence:** MEDIUM
- **Location:** Files: `README.md` §Documentation table (L549-561); `architecture/` (16 files, entirely unlinked).
- **Observation:** README's documentation index links `guide/*` and `docs/BENCHMARKS.md`/`CHANGELOG.md` but never links anything under `architecture/`, a 16-file directory covering current/target architecture, routing model, learning system, observability, and a test plan.
- **Evidence:** `grep -n "architecture/" README.md` → no matches (only `guide/ARCHITECTURE.md` is linked, a different, smaller document).
- **Why this exists, if discoverable:** UNCERTAIN — could be intentional (internal design-process documents not meant for end users) or an oversight. Both `guide/ARCHITECTURE.md` and `architecture/CURRENT_ARCHITECTURE.md`/`architecture/TARGET_ARCHITECTURE.md` cover overlapping ground under different names, which reads as unintentional duplication rather than a deliberate internal/external split.
- **Why this matters:** A user or contributor looking for "the" architecture doc finds one (guide/ARCHITECTURE.md) and has no way to know a more detailed 16-document set exists one directory over.
- **User-visible impact:** LOW — mainly affects contributors, not end users.
- **Engineering impact:** LOW — risk of the two architecture doc sets diverging further with no cross-reference.
- **Is behavior currently used? N/A**
- **Recommended action:** MERGE or at minimum cross-link — synthesis should decide whether `architecture/` is (a) superseded by `guide/ARCHITECTURE.md` and should be archived, (b) the deeper reference `guide/ARCHITECTURE.md` should point to, or (c) genuinely internal-only and should say so explicitly at its own top-level README.
- **Proposed target:** One canonical architecture entry point linked from README; the other either archived or explicitly scoped as "internal design history."
- **Behavioral compatibility risk:** None.
- **Security risk:** None.
- **Performance impact:** None.
- **Estimated complexity removed:** Potentially significant if `architecture/` is superseded and can be archived (16 files) — but that determination belongs to whichever domain covers module/doc consolidation generally; flagged here as a discoverability problem, not a redundancy verdict.
- **Validation required:** Read `architecture/README.md` (16-file directory's own index) to determine current status before deciding archive vs. link — not done in this pass due to scope/time; recommend to synthesis.
- **Dependencies on other findings:** None directly, but relevant to the global Consolidation Ledger.

---

## Top items for synthesis (5-10 candidates)

1. **DOC-03 (Codex auto-routing mislabeled "not yet")** — highest-severity, highest-confidence, purest docs-only fix with real user impact. Strong Top-10 global candidate.
2. **DOC-01 + DOC-08 (tool count "60" vs actual 70, propagated from one stale comment into 3 docs)** — good example of "one wrong number, three places it looks corroborated"; recommend fixing the source comment (DOC-08) as the actual root-cause action item, with DOC-01 as the visible symptom.
3. **DOC-02 (12 vs 11 front-door tools, omitting the write/exec-capable `llm_local_task`)** — pairs well with the Security domain's review of default-on capabilities; worth a joint mention if Security also flags `llm_local_task`.
4. **DOC-07 (uncaveated per-host savings percentages)** — a good example for the "skeptical engineer" test (§68-69) and a template case: the same README already shows, three screens later, exactly how to caveat a number like this correctly.
5. **DOC-06 (4 providers with zero documentation)** — candidate for either the Deletion Ledger (if these providers are actually unmaintained) or a documentation backlog item; needs a one-line check from the Providers domain (§18) on whether these integrations are even exercised by tests.
6. **DOC-05 (tool_tiers.py's stale default-fallback comment/logic)** — low current impact but a real latent footgun matching a failure mode (`CHZ-SURF-01`) the codebase has already been burned by once, per its own comments; good "do-not-change-blindly, but do fix" register candidate.
7. **DOC-10 (architecture/ entirely unlinked, likely duplicate of guide/ARCHITECTURE.md)** — feed into the global Consolidation Ledger; needs architecture/README.md read by whoever owns that ledger to determine current-vs-dead status.
8. **General pattern for synthesis:** every numeric claim in this README that is NOT already caveated the way the Savings section caveats its own figures should get the same treatment — the doc already contains its own quality bar (§17-style rigor) in one section and violates it in others. That's a cheap, systemic, one-pass fix (add "measured, not estimated" / "n=..." / explicit anecdote framing) rather than 10 separate edits.
