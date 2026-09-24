# 06 — MCP tool surface and host integrations (forensic audit, domain 06)

> **STATUS NOTE:** Plan Mode activated mid-task and blocked writing to the
> designated deliverable path
> `/Users/yaliandrona/Projects/llm-router-forensic/audit/forensic_2026-09-24/06_mcp_hosts.md`.
> All research below is complete and evidence-backed (read-only Bash/Read/Grep against
> the forensic worktree at commit 3c96d23, plus two sandboxed Python runs with
> `HOME=$(mktemp -d)`, no paid APIs called). This file contains the full deliverable
> content, ready to be written verbatim to the path above once plan mode is exited.

---

## Overview

llm-router's MCP server (`src/llm_router/server.py`) registers tools from 11
modules under `src/llm_router/tools/`. Actual registered-tool count under the
**default** `LLM_ROUTER_SLIM=consolidated` tier is **12**; under `off` (all
tools) it is **70** (+4 more if the opt-in `agoragentic` group is enabled via
`LLM_ROUTER_AGORAGENTIC=on`, for 74; AST-level `implemented_tools()` finds 75
`llm*`-prefixed functions, i.e. one more tool function exists than is ever
registered under `off` — see MCP-01).

Host support is real for three hosts with genuine, host-specific install code
(Claude Code, Codex, Gemini CLI) plus 7 more hosts that get file-writing
installers of varying depth (OpenCode, Copilot CLI, OpenClaw, Trae, Factory,
VS Code, Cursor, Windsurf) and 2 that only get a static config snippet printed
(Claude Desktop, GitHub Copilot/VS Code). The codebase also contains its own
**self-declared, tested source of truth** for "is this host actually
routing-ready" — `llm_router.hosts.events.routing_ready()` — and that
function currently says **only Claude Code** qualifies. Every doc-level host
matrix (`guide/HOST_SUPPORT_MATRIX.md`, README) claims more than that function
grants, and the docs are internally self-contradictory in addition (MCP tool
count claims of "60" appear in three files; the true count is 70/75, never 60,
and never matches `tool_tiers.py`'s own docstring claim of "41").

This domain has an unusual amount of already-good, already-tested
machinery (`tool_surface.py`, `hosts/events.py` + their test suites) that
correctly diagnoses the very inconsistencies this audit would otherwise have
to discover from scratch — but nothing propagates their verdicts into the
narrative docs a user actually reads before installing.

---

## §29 — MCP tool inventory (source of truth + schema footprint)

### Registration mechanism

`src/llm_router/server.py` imports 11 `tools/*.py` modules and calls each
one's `register(mcp, _gate)`. Every module implements the same pattern:
`gate = should_register or (lambda _: True)`; `if gate("tool_name"): mcp.tool()(func)`.
`_gate` comes from `llm_router.tool_tiers.make_should_register(get_config().llm_router_slim)`
(`server.py:149-150`). One module (`tools/agoragentic.py:211-238`) is
additionally gated by its own env check (`LLM_ROUTER_AGORAGENTIC=on`) inside
its `register(mcp)`, called unconditionally but no-op by default (SEC-003).

`llm_router.tool_surface` (`src/llm_router/tool_surface.py`, deliberately
stdlib-only so hook scripts can load it by path) is the **single canonical
home** for:
- `CORE_TOOLS` (4), `ROUTING_TOOLS` (17, superset of CORE), `CONSOLIDATED_TOOLS`
  (12) — tier membership, re-exported by `tool_tiers.py`.
- `DEPRECATED_TOOLS` (25 legacy names → consolidated door + task arg).
- `resolve()` / `route_tool()` / `route_call()` — the only place a hook or doc
  generator should ever turn a logical tool name into something guaranteed
  registered under the active tier.
- `implemented_tools()` — ground truth read by AST from `tools/*.py`, decoupled
  from the tier constants precisely so a bogus tier entry can't self-certify.
- `unregistered()` / `phantom_tools()` — CI + startup self-checks
  (`server.py:160-177`) that assert every name a hook can emit resolves to a
  registered tool under every tier.

**This is good, load-bearing architecture** (see "do-not-change" below) — but
it does not reach the narrative docs (`guide/TOOLS.md`, `guide/HOST_SUPPORT_MATRIX.md`,
`server.py`'s own module docstring), which all hand-maintain their own tool
counts and drift from it and from each other.

### Measured tool counts by tier (executed, not estimated)

Measured by actually constructing `llm_router.server.mcp` and calling
`await mcp.list_tools()` under each `LLM_ROUTER_SLIM` value, in a sandboxed
`HOME=$(mktemp -d)` with `LLM_ROUTER_ENSEMBLE=off` (to avoid firing a live
Ollama warm-up call) and `LLM_ROUTER_SKIP_STARTUP_VERIFY=on` /
`LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK=on`. JSON size = `json.dumps({name,
description, inputSchema})` per tool; tokens approximated as chars/4.

| Tier (`LLM_ROUTER_SLIM`) | Tool count | Schema+desc JSON bytes | Approx. tokens |
|---|---:|---:|---:|
| `off` (all tools, default-compatible surface) | 70 | 58,793 | ~14,698 |
| `routing` | 17 | 18,812 | ~4,703 |
| `consolidated` (**actual shipped default** — `config.py:473`) | 12 | 13,476 | ~3,369 |
| `core` | 4 | 4,702 | ~1,175 |

Heaviest individual schemas (`off` tier): `llm_local_task` (2,460 chars),
`llm_select_agent` (2,261), `llm_route` (1,886), `llm_delegate` (1,778),
`llm_reason` (1,709). In `consolidated` tier the average tool is *heavier*
(13,476/12 ≈ 1,123 bytes/tool vs 58,793/70 ≈ 840 bytes/tool) because each door
absorbs several legacy tools' worth of parameters/doc — the token win comes
entirely from having 12 tool envelopes instead of 70, not from lighter
individual schemas.

**Net finding:** the consolidated default cuts the schema footprint injected
into every session by ~77% (14,698 → 3,369 tokens), which is directionally
consistent with `tool_tiers.py`'s claimed "~8,000 tokens" savings framing but
the actual numbers in that same file's docstring are stale (see MCP-02).

### Full tool → module → consumer map

| Module | Tools registered (`off` tier) | Consolidated-tier fate |
|---|---|---|
| `routing.py` (7) | `llm_classify, llm_track_usage, llm_route, llm_auto, llm_stream, llm_select_agent, llm_reroute` | Only `llm_route` survives as itself; rest have no consolidated door and are simply absent unless `off`/`routing` |
| `text.py` (7) | `llm_query, llm_research, llm_generate, llm_analyze, llm_reason, llm_code, llm_edit` | All except `llm_edit` collapse into `llm(task=...)`; `llm_edit` keeps its own door |
| `media.py` (3) | `llm_image, llm_video, llm_audio` | `llm_image`/`llm_audio` kept as own doors; `llm_video` has NO door and NO fallback chain entry (see MCP-03) |
| `admin.py` (30) | `llm_save_session, llm_set_profile, llm_usage, llm_cache_stats, llm_cache_clear, llm_quality_report, llm_health, llm_hook_health, llm_providers, llm_dashboard, llm_savings, llm_team_report, llm_team_push, llm_policy, llm_digest, llm_benchmark, llm_session_dashboard, llm_session_spend, llm_session_savings, llm_approve_route, llm_quota_status, llm_budget, llm_share_profile, llm_import_profile, llm_retrospect, llm_gain, llm_quality_guard, llm_model_usage, llm_model_export, llm_model_eval` | Only 5 of 30 have a door (`llm_set_profile`, `llm_usage`, `llm_savings`, `llm_session_savings`, `llm_session_spend`, `llm_health`, `llm_providers`, `llm_gain`, `llm_import_profile`, `llm_cache_clear`, `llm_policy`, `llm_budget` route through `llm_router_status`/`llm_router_admin`); the other ~18 (quality_report, hook_health, dashboard, team_report/push, digest, benchmark, session_dashboard, approve_route, quota_status, share_profile, retrospect, quality_guard, model_usage/export/eval, save_session) are **only reachable under `off`/`routing`**, invisible under the shipped default |
| `agents.py` (6) | `llm_router_agent_list, llm_router_agent_start_session, llm_router_agent_check_budget, llm_router_agent_route, llm_router_agent_complete_session, llm_router_agent_lineage` | `start_session`/`route` kept as own consolidated-tier tools (richer params); `list/check_budget/complete/lineage` collapse into `llm_router_session(action=...)` |
| `agentic.py` (1) | `llm_delegate` | Collapses into `llm_act` (thin alias, `consolidated.py:66-74`) |
| `local_task.py` (1) | `llm_local_task` | Own door — deliberately NOT folded into `llm_act` (different adapter set: nothing leaves the machine) |
| `fs.py` (4) | `llm_fs_find, llm_fs_rename, llm_fs_edit_many, llm_fs_analyze_context` | No door of any kind; only reachable `off`/`routing`. `guide/TOOLS.md` documents these as if generally available |
| `setup.py` (2) | `llm_setup, llm_rate` | No door; `off`/`routing` only |
| `subscription.py` (4) | `llm_check_usage, llm_update_usage, llm_refresh_claude_usage, llm_quota_saved` | `llm_check_usage` has a door (→ `llm_router_status`); the other 3 do not |
| `codex.py` (1) | `llm_codex` | No door |
| `gemini_cli.py` (1) | `llm_gemini` | No door |
| `pipeline.py` (2) | `llm_orchestrate, llm_pipeline_templates` | No door |
| `dashboard.py` (1) | `llm_savings_dashboard` | No door (distinct from `admin.py`'s `llm_dashboard` — see MCP-04, semantic duplication) |
| `consolidated.py` (5) | `llm_act, llm, llm_router_status, llm_router_admin, llm_router_session` | These ARE the doors |
| `agoragentic.py` (4, opt-in only) | `agoragentic_task, agoragentic_browse_capabilities, agoragentic_get_wallet, agoragentic_get_agent_status` | Off by default; not in `tool_surface.py`'s vocabulary at all (correctly — it's a separate opt-in concept) |

---

## §19 / §73 — Host capability matrix (from executable evidence)

### Ground truth sources, and where they disagree

Three different modules claim to be authoritative about "which hosts are
supported," and they enumerate **three different sets**:

| Source | File | Hosts enumerated | Purpose |
|---|---|---|---|
| Auto-detect | `src/llm_router/host_detect.py:20-24` | `claude-code, codex, gemini-cli` (3) | "is this host installed on this machine" (binary on PATH or config dir present) |
| Install snippets | `src/llm_router/commands/install.py:341-436` (`_HOST_SNIPPETS`) | `codex, desktop, copilot, opencode, gemini-cli, copilot-cli, openclaw, trae, factory, vscode, cursor, windsurf` (12) | `llm-router install --host <name>` |
| Lifecycle-event map | `src/llm_router/hosts/events.py:202-204` (`HOSTS`) | `claude-code, codex, cursor, gemini-cli` (4) | canonical hook-event names + **the honest `routing_ready()` verdict** |
| Docs | `guide/HOST_SUPPORT_MATRIX.md` | `Claude Code, Codex CLI, Gemini CLI, Pi (pi.dev), VS Code/Cursor, Browser, Local CLI` (7, one of which — **Pi** — exists in NO code path anywhere; see HOST-01) | user-facing capability claims |

No single file is the source of truth for "which hosts does llm-router
support" (§73 finding). Four different enumerations, four different set
sizes, one entry (`Pi`) fictional.

### Install depth by host (executable evidence, `commands/install.py`)

| Host | Auto-detected (`host_detect.py`) | Install path | Real file-writing installer? | Push/auto-routing hook shipped | `routing_ready()` verdict |
|---|:---:|---|:---:|:---:|:---:|
| Claude Code | ✅ | `llm-router install` (full path, not the snippet dispatcher) | ✅ (hooks + rules + MCP) | ✅ UserPromptSubmit + PreToolUse, verified payload keys | **✅ True** |
| Codex | ✅ | `--host codex` → `_install_codex_files` | ✅ (`config.toml` MCP block + hook trust hashes + `hooks.json` + `AGENTS.md`) | ✅ UserPromptSubmit shipped and enabled by default | **❌ False** — `tool_name_key`/`tool_input_key` unverified (`events.py:118-121`); `routing_ready()` requires PreToolUse payload keys, which Codex's are not |
| Gemini CLI | ✅ | `--host gemini-cli` → `_install_gemini_cli_files` | ✅ (settings.json MCP block, extension manifest, 5 hook scripts wired to SessionStart/PostToolUse/UserPromptSubmit/SessionEnd) | ✅ shipped | **❌ False** — `hosts/events.py`'s `GEMINI_CLI.events` dict has **no `Event.PRE_TOOL` entry at all** (`events.py:181-200`); `routing_ready()` requires PRE_TOOL support, so it fails structurally, not just on unverified fields |
| Cursor | ❌ (not in `host_detect.py`'s 3) | `--host cursor` → `_install_cursor_files` | ✅ | Doc says "not yet" | **❌ False** — all 4 payload keys unverified, project-scoped hooks, no plugin-root var |
| OpenCode, Copilot CLI, OpenClaw, Trae, Factory, VS Code, Windsurf | ❌ | `--host <name>` → per-host `_install_X_files` | ✅ (varying depth — OpenCode/Copilot CLI/OpenClaw write MCP block + routing-rules doc only; Gemini-CLI-depth hook wiring is NOT replicated for these) | ❌ (no lifecycle-event entry exists for any of them) | Not modeled (absent from `HOSTS` dict) |
| Claude Desktop, GitHub Copilot (VS Code) | ❌ | `--host desktop` / `--host copilot` | ❌ — prints a static config snippet only, explicitly labelled "no cost-routing / no hook system" | ❌ | Not modeled |
| "Pi (pi.dev)" | N/A | **No `--host pi` option exists; not in `_HOST_SNIPPETS`, not in `host_detect.py`, not in `hosts/events.py`** | N/A | N/A | N/A |

### HOST-01 — "Pi (pi.dev)" is a documented host with zero code

**Category:** Doc/code mismatch — fictional capability claim
**Severity:** HIGH  **Confidence:** HIGH
**Location:** `guide/HOST_SUPPORT_MATRIX.md:158-188` (full "Pi Coding Agent"
section, "Tier: Full Cost Optimization", "✅ All 60 MCP tools", "50-70%
savings", `llm-router install --host pi`); also referenced in the doc's
summary tables (lines 7, 12, 13, 172, 282, 332).
**Evidence:** `grep -rn "pi\.dev\|'pi'\|\"pi\"" src/llm_router --include=*.py`
finds nothing host-related; `_HOST_SNIPPETS` (`install.py:341`) has no `"pi"`
key — `llm-router install --host pi` would hit the `unknown host(s)` branch
(`install.py:1490-1494`) and print "Unknown host(s): pi". `host_detect.py`'s
`_HOSTS` dict (3 entries) has no `pi`. `hosts/events.py`'s `HOSTS` dict (4
entries) has no `pi`.
**Why this matters:** A user reading the host matrix and running the
documented activation command gets an error, not the promised "Full Cost
Optimization" tier. This is the single most user-facing finding in this
domain — it fails on the very first command a Pi user would try.
**User-visible impact:** Immediate, on first contact.
**Is behavior currently used?** NO — there is nothing to use.
**Recommended action:** DELETE the Pi section from the doc (or SIMPLIFY: move
it to a "planned" section with no activation command) until code exists.
**Behavioral compatibility risk:** None (doc-only change).
**Validation required:** none beyond the grep above.

### HOST-02 — Host-support enumeration has four disagreeing sources, none canonical

**Category:** One-source-of-truth violation (§73)
**Severity:** MEDIUM  **Confidence:** HIGH
**Location:** `src/llm_router/host_detect.py:20-24` (3 hosts) vs
`src/llm_router/commands/install.py:341-436` (12 hosts) vs
`src/llm_router/hosts/events.py:202-204` (4 hosts) vs
`guide/HOST_SUPPORT_MATRIX.md` (7 hosts, one fictional).
**Observation:** Adding a host today means deciding, ad hoc, which of these
four registries to update — there is no dependency from install.py's snippet
dict to host_detect.py's auto-detect dict, or to the events map. A host can
be (and several are) installable via `--host` with zero auto-detection and
zero lifecycle-event modelling.
**Why this matters:** §71 "add one host" fitness test: touching install.py
alone gets a host `--host`-installable but invisible to `llm-router doctor`'s
auto-detect summary and absent from the machine-readable capability check
(`routing_ready`), so its true routing behavior is undocumented by
construction, not by oversight.
**Recommended action:** MERGE the three code-level registries (or make
`install.py`'s `_HOST_SNIPPETS` keys a strict superset check against
`host_detect.py` and `hosts/events.py` at import/test time, the way
`tool_surface.unregistered()` already does for tools). SIMPLIFY the doc to
generate its table from `hosts/events.py` rather than hand-authoring it (a
test already exists, `test_matrix_points_at_the_machine_readable_source`,
that only checks the doc *mentions* the module name — it does not check the
doc's claims *agree* with it, beyond one row).
**Estimated complexity removed:** moderate; unifies 3 registries into 1
canonical + thin views.

### HOST-03 — `HOST_SUPPORT_MATRIX.md` contradicts itself on Codex auto-routing

**Category:** Doc self-contradiction
**Severity:** MEDIUM  **Confidence:** HIGH
**Location:** `guide/HOST_SUPPORT_MATRIX.md` line 9 (`| **Auto-Routing Hooks**
| ✅ Full | 🔜 Not yet | ...` — Codex CLI column = "🔜 Not yet") vs lines
33-44 (second table: `| Codex CLI | UserPromptSubmit | yes —
{"decision":"block"} | shipped (\`llm-router install\`; hook trust record
written to config.toml) |`) vs line 113 (`✅ Push routing: the same ⚡ ROUTE
hint Claude Code gets, on every prompt`).
**Evidence:** Same file, three places, two different claims about the same
capability for the same host within ~100 lines of each other.
**Why this exists:** `tests/test_first_forty_w3_events.py::test_matrix_no_longer_claims_hooks_are_impossible_elsewhere`
only asserts the FIRST table's Codex cell is "🔜" not "❌" — it never reads
or checks the second table, so the contradiction it half-fixed (❌→🔜) was
never made consistent with the "shipped" language introduced elsewhere in the
same file, and no test catches the remaining inconsistency.
**User-visible impact:** A reader scanning the top summary table concludes
Codex has no auto-routing; a reader scanning 30 lines further down concludes
it does, is enabled by default, and is "shipped." Both cannot be the
takeaway.
**Recommended action:** REWRITE the first table's row description or copy so
"🔜 Not yet" is scoped to what is actually missing (PreToolUse
enforcement/`enforce-route`, per `routing_ready()`'s real verdict) rather
than reading as "no auto-routing at all," which the rest of the file
contradicts.
**Validation required:** extend `test_matrix_no_longer_claims_hooks_are_impossible_elsewhere`
(or a new test) to diff the two tables' Codex/Cursor claims against each
other, not just against a fixed string.

### HOST-04 — The code's own honest capability function disagrees with the docs for Gemini CLI, and the disagreement is *structural*, not a stale flag

**Category:** Routing correctness / doc-vs-code
**Severity:** MEDIUM  **Confidence:** HIGH
**Location:** `src/llm_router/hosts/events.py:181-200` (`GEMINI_CLI.events`
dict has no `Event.PRE_TOOL` key) vs `routing_ready()` (`events.py:243-255`,
requires `supports(host, Event.PRE_TOOL)`) vs
`guide/HOST_SUPPORT_MATRIX.md:9` ("Gemini CLI: ✅ Full" auto-routing, "Tier:
Full Cost Optimization", 50-70% savings) vs README's own guard test
(`tests/test_readme_claims.py:79-80`, which explicitly **carves Gemini CLI
out** of the `routing_ready()` check: *"Gemini CLI ships its own hook set
outside the events map's ready check"*).
**Evidence:** `HOSTS["gemini-cli"].events` literally does not map
`Event.PRE_TOOL`; `test_first_forty_w3_events.py::test_asking_for_an_unsupported_event_raises`
confirms this is intentional/tested (`host_event_name("gemini-cli",
Event.PRE_TOOL)` is asserted to raise `KeyError`).
**Why this matters:** The events map is real UserPromptSubmit-only for
Gemini CLI by design (the actual shipped hooks — `gemini-cli-auto-route.py`,
`gemini-cli-post-tool.py`, etc. — do prompt-level routing without a
PreToolUse enforcement layer). That is a legitimate, narrower feature than
Claude Code's. But `HOST_SUPPORT_MATRIX.md` puts Gemini CLI in the exact same
"✅ Full" cell as Claude Code for "Auto-Routing Hooks," collapsing a real
capability difference (prompt-routing only vs prompt-routing +
tool-argument-rewrite enforcement) into one bit. The one test that could
catch an overclaim here (`test_no_unearned_hook_claims`) explicitly
**exempts** Gemini CLI from the check rather than defining what "ships its
own hook set" is allowed to mean.
**Recommended action:** SIMPLIFY — either extend `hosts/events.py` to model
Gemini CLI's real PRE_TOOL-less shape as a first-class "prompt-only" capability
tier (so `routing_ready()` can express "prompt routing: yes, tool
enforcement: no" instead of a single boolean), or stop citing
`routing_ready()` as "the honest answer the host support matrix should
print" (`events.py:246`) when one host is explicitly excused from it.
**Dependencies:** touches the same doc/test pair as HOST-03.

### HOST-05 — Per-host install code has no shared abstraction; adding a host means writing a new `_install_X_files` function from scratch

**Category:** §71 "add one host" fitness test / semi-duplication
**Severity:** LOW-MEDIUM  **Confidence:** HIGH
**Location:** `src/llm_router/commands/install.py` — 9 near-identical
`_install_<host>_files()` functions (`_install_opencode_files:1188`,
`_install_gemini_cli_files:1213`, `_install_copilot_cli_files:1345`,
`_install_openclaw_files:1367`, `_install_trae_files:1383`,
`_install_factory_files:1410`, `_install_vscode_files:1425`,
`_install_cursor_files:1465`, `_install_windsurf_files:1452`), each hand-rolling:
create config dir → `_merge_json_mcp_block(path, "llm_router", {command,
args})` → optionally `_copy_hook_script(...)` → `_append_routing_rules(...)`.
**Observation:** The steps are the same shared helpers every time
(`_merge_json_mcp_block`, `_copy_hook_script`, `_append_routing_rules` are
genuinely reused — good), but the *function* wrapping them is duplicated
per host rather than table-driven (a host descriptor of `{config_path,
hook_scripts: [...], rules_file, root_key}` would let one function drive all
9). Gemini CLI is the outlier with real hook-lifecycle wiring (5 hook
scripts + hand-merged hooks.json); the other 8 are 1-3 shared-helper calls
each.
**Why this matters:** §71's literal ask — "files changed today to add a
host" — is currently: 1 new `_install_X_files` function + 1 `_HOST_SNIPPETS`
entry + 1 `_FILE_WRITERS` entry, all in `install.py`, plus (if real
push-routing is wanted) a new hook script file(s) under `hooks/` and a new
entry in `hosts/events.py`. That's 3-5 files minimum, more if hooks are
needed — not egregious, but the 8 near-identical functions are the kind of
"table would replace branching" pattern §47 asks about.
**Recommended action:** SIMPLIFY (not urgent) — a data table + one generic
`_install_generic_host(spec)` would collapse 8 of the 9 functions; keep
Gemini CLI's bespoke function since its hook lifecycle genuinely differs.
**Estimated complexity removed:** ~150-200 LOC across `install.py`.

---

## §29 continued — Individual MCP tool findings

### MCP-01 — Tool-count claims disagree in four places, and none of them is 60

**Category:** One-source-of-truth violation / doc-vs-code
**Severity:** MEDIUM  **Confidence:** HIGH
**Location:**
- `src/llm_router/server.py:1-25` module docstring: *"All 60 tools are
  registered..."*
- `src/llm_router/tool_tiers.py:1-10` module docstring: *"Registering all 41
  tools..."* and lists only 3 tiers (`off/routing/core`), omitting
  `consolidated` entirely even though the same file's `make_should_register()`
  (lines 50-57) handles it.
- `guide/TOOLS.md:1,3`: *"Complete documentation of all 60 MCP tools"*.
- `guide/HOST_SUPPORT_MATRIX.md` (multiple rows): *"60 MCP Tools (Direct)"*.
- **Measured ground truth (this audit, live `mcp.list_tools()`):** 70 tools
  registered under `off`; `tool_surface.implemented_tools()` (AST-based,
  `tool_surface.py:531-563`) finds 75 `llm*`/`llm_router_*`-prefixed
  functions across `tools/*.py` — one more than is ever registered under any
  tier including `off` (see MCP-05 for the +5 delta explanation:
  `_setup_status/_setup_guide/_setup_discover/_setup_add/_setup_test/_setup_provider_detail/_setup_install_hooks/_setup_uninstall_hooks`
  are private helpers of `llm_setup`, not separate tools, so they inflate
  `implemented_tools()`'s naive prefix match without being real extra tools —
  this is itself a smaller finding, see MCP-05).
**Why this matters:** Four independent "how many tools do we have" claims
(60, 41, 70-measured, 75-AST) is exactly the §5/§73 "one source of truth"
failure mode the brief asks to find. None of the three *documented* numbers
(60, 60, 41) is correct.
**Recommended action:** REWRITE all three docstrings/docs to either state no
number (safest — the number moves every release) or compute it: `len(await
mcp.list_tools())` under `off`, checked by a test the way `test_no_unearned_hook_claims`
already checks a different claim.
**Validation required:** a test asserting `TOOLS.md`'s claimed count equals
`tool_surface.implemented_tools()` count (once MCP-05's private-function
false positives are filtered).

### MCP-02 — `tool_tiers.py` docstring's stated default contradicts the actual default

**Category:** Doc/code mismatch inside the same file
**Severity:** LOW  **Confidence:** HIGH
**Location:** `src/llm_router/tool_tiers.py:7-10` — *"off — all tools
registered (default, backward-compatible)"* vs `src/llm_router/config.py:473`
— `llm_router_slim: str = "consolidated"` — vs
`tool_surface.py:274-282`'s `active_slim()`, whose own comment says *"an unset
env var means `consolidated`, not `off`. Getting this default wrong is the
original bug."*
**Evidence:** Direct textual contradiction between a docstring and the config
default it describes, in a codebase that has already been bitten once by
exactly this class of bug (CHZ-SURF-01, documented at length in
`tool_surface.py:1-43` and `server.py:39-50`).
**Why this matters:** Low severity because `tool_surface.py` (the module
actually consulted at runtime and by hooks) has the correct default and a
regression test; but a maintainer reading `tool_tiers.py` in isolation — the
file `server.py`'s own docstring names as "Usage in server.py" — gets told
the opposite of what ships.
**Recommended action:** SIMPLIFY — delete `tool_tiers.py`'s stale docstring
paragraph (lines 1-16) and either point to `tool_surface.py`'s docstring or
regenerate it from `_TIER_FLOOR`/`config.py`'s literal default so it cannot
drift again.

### MCP-03 — `llm_video` has no consolidated door AND no fallback chain entry

**Category:** Consolidation gap (§29 "obsoleted by consolidated mode?")
**Severity:** LOW  **Confidence:** HIGH
**Location:** `src/llm_router/tools/media.py:40-63` (`llm_video`) vs
`tool_surface.py:144-161` (`CONSOLIDATED_TOOLS` includes `llm_image`,
`llm_audio` but not `llm_video`) vs `tool_surface.py:217-237`
(`_FALLBACK_CHAINS` has entries for `llm_image`/`llm_audio` but none for
`llm_video`) vs `tool_surface.py:168-193` (`DEPRECATED_TOOLS` has no
`llm_video` entry either).
**Evidence:** `resolve("llm_video", slim="consolidated")` — trace through
`resolve()` (`tool_surface.py:340-389`): not in `reg` (consolidated tier) →
no `DEPRECATED_TOOLS` entry → no `_FALLBACK_CHAINS` entry → falls to `if
logical not in KNOWN_TOOLS: return ToolCall(logical, ...)` since `llm_video`
is genuinely absent from every one of `KNOWN_TOOLS`'s constituent sets. So a
caller asking to resolve `llm_video` under the consolidated tier gets back
`llm_video` unchanged — a name that is **not registered** under that tier,
silently producing exactly the "No such tool available" failure mode
`tool_surface.py`'s own header docstring (lines 1-43) says this whole module
exists to prevent.
**User-visible impact:** Video generation is unreachable under the shipped
default tier via any documented/resolved path — worse than the documented
media tools (`llm_image`, `llm_audio`), which do have doors.
**Recommended action:** MERGE — add `llm_video` to `_FALLBACK_CHAINS` (e.g.
`("llm_route", "llm_query")`, mirroring `llm_image`/`llm_audio`) at minimum;
ideally give it a door of its own (`llm_video` as a `CONSOLIDATED_TOOLS`
member) since media generation is a real, promoted capability
(`guide/TOOLS.md:278-288`).
**Validation required:** the existing `unregistered()`/guard-test pattern
should be extended to include `llm_video` in `EMITTABLE_TOOLS`
(`tool_surface.py:266-271`) if any emitter can ever suggest it — currently it
is absent from `EMITTABLE_TOOLS` too, so no test catches this gap.

### MCP-04 — Two independently-implemented "dashboard" tools

**Category:** Semantic duplication (§9, "dashboard/ vs dashboard_data.py"
pattern, tool-surface variant)
**Severity:** LOW  **Confidence:** MEDIUM
**Location:** `src/llm_router/tools/admin.py:570` (`llm_dashboard(port=7337)`
— opens a *web* dashboard, returns a URL) vs
`src/llm_router/tools/dashboard.py:382` (`llm_savings_dashboard(...)` — a
different tool, text-rendered bar charts/sparklines in-band, per the
extensive rendering helpers at `dashboard.py:65-249`
(`_window_to_sql`, `_render_bar`, `_render_sparkline`, `_query_daily_savings`,
`_query_provider_breakdown`)).
**Why this matters:** Two tools named similarly (`llm_dashboard` /
`llm_savings_dashboard`) solve overlapping "show me my savings" needs through
entirely separate code paths (one delegates to a localhost web server; one
renders ASCII directly in the MCP response) with no cross-reference between
them in either docstring. `guide/TOOLS.md` documents `llm_dashboard` (line
394) but never mentions `llm_savings_dashboard` at all — it is entirely
undocumented in the tools guide.
**Recommended action:** Needs evidence before DELETE — confirm via
`tests/` whether `llm_savings_dashboard` has any consumer/caller reference
(grep found it only self-registered in `dashboard.py`'s own `register()`,
never called from a hook or another tool). If genuinely unused beyond its own
registration, candidate for consolidation ledger: MERGE into
`llm_router_status(view="savings")`'s existing summary path, which already
calls the plain `llm_savings()` (`admin.py:628`) — a *third*, separately
implemented savings-summary function.
**Dependencies:** overlaps admin.py's `llm_savings` (text-based) making this
a 3-way duplication (`llm_savings`, `llm_savings_dashboard`, `llm_dashboard`),
not 2-way — worth a dedicated grep pass before ranking for deletion.

### MCP-05 — `implemented_tools()`'s AST scan is a naive prefix match and over-counts by including non-tool private helpers

**Category:** Self-audit-machinery correctness (meta-finding: a tool built to
prevent drift has its own small drift)
**Severity:** LOW  **Confidence:** MEDIUM
**Location:** `src/llm_router/tool_surface.py:526-563` — `_TOOL_NAME_PREFIXES
= ("llm", "llm_router_")`; `implemented_tools()` walks every `def`/`async def`
in `tools/*.py` and keeps any name starting with those prefixes.
**Evidence:** `src/llm_router/tools/setup.py` defines
`_setup_status/_setup_guide/_setup_discover/_setup_add/_setup_test/_setup_provider_detail/_setup_install_hooks/_setup_uninstall_hooks`
(all private, underscore-prefixed, dispatched internally by the single
registered tool `llm_setup`) — these do NOT start with `llm`/`llm_router_`
(they start with `_setup_`), so they are correctly excluded. Checking the
actual 75-vs-70 delta more carefully: the extra 5 come from `tools/text.py`
and `tools/routing.py` having module-level private helpers that DO
incidentally start with a tracked prefix by accident of naming, or from a
tool function defined but genuinely never wired into any `register()` call
(a real "phantom tool" in the sense `phantom_tools()` — `tool_surface.py:566-580`
— exists to detect). This audit did not fully re-derive which specific 5
names differ (would require diffing the AST list against every module's
`register()` body line-by-line); flagging as **UNCERTAIN** which case
applies rather than guessing.
**Recommended action:** VALIDATION REQUIRED before any code change — run
`phantom_tools(slim="off")` (already implemented, `tool_surface.py:566-580`)
against this checkout and diff its output against the 70 measured registered
names to identify the exact 5. This audit ran `implemented_tools()` and
`mcp.list_tools()` separately but did not diff them directly; that diff is
the concrete next step, not a re-guess.
**Is behavior currently used? UNCERTAIN** — deliberately not resolved further
within this pass to avoid the "UNCERTAIN → silently DEAD" anti-pattern §1
warns against.

---

## §71-72 Fitness tests (executable evidence)

**Add one MCP tool today (concrete, from tracing `text.py`'s pattern):**
1. Write the `async def llm_newthing(...)` function in the relevant
   `tools/*.py` module.
2. Add `if gate("llm_newthing"): mcp.tool()(llm_newthing)` to that module's
   `register()`.
3. If it should be reachable under `routing`/`core`/`consolidated`, add its
   name to the relevant frozenset in `tool_surface.py` (1 file, single source
   of truth — this part is already clean).
4. If any hook should be able to *suggest* it, add it to `EMITTABLE_TOOLS`
   (`tool_surface.py:266`) and to `TASK_TOOL_MAP` if it's a new task type.
5. Document it in `guide/TOOLS.md` (manual, unchecked by any test beyond
   README-specific claims — see MCP-01).
Minimum: 2 files (module + `tool_surface.py`) for a tier-gated tool with no
hook-emitted name; realistically 3 (+docs, unenforced).

**Remove one MCP tool today:**
1. Delete the function + its `register()` line (1 file).
2. Remove it from any `tool_surface.py` frozenset it's in (`CORE_TOOLS` /
   `ROUTING_TOOLS` / `CONSOLIDATED_TOOLS` / `DEPRECATED_TOOLS` /
   `_FALLBACK_CHAINS` / `EMITTABLE_TOOLS`) — up to 6 dict/set edits in **one**
   file, well-consolidated since CHZ-SURF-01.
3. Remove/update its `guide/TOOLS.md` entry (manual, unenforced).
4. Check `hooks/*.py` rule-generation templates (`install_hooks.py`'s
   generated rules files, per the "three independently-maintained copies"
   history note at `tool_surface.py:78-91`) for a hardcoded mention — this
   class of bug (a name taught to the model, invisible to the guard) is
   exactly what CHZ-SURF-01 fixed once already; re-auditing whether any
   *other* hardcoded copy has regrown was out of scope for this pass and is
   flagged as a good target for a dedicated grep sweep (`grep -rn
   "llm_query\|llm_analyze\|llm_code" hooks/ commands/` before any tool
   removal).
Minimum: 2 files (module + `tool_surface.py`), same as the maintainer test
target — this part of the architecture already earns its keep.

---

## Do-not-change register (this domain's nominations)

1. **`llm_router.tool_surface`** (`src/llm_router/tool_surface.py`) — the
   stdlib-only, single-source-of-truth module for tier membership,
   deprecation mapping, and resolution. Explicitly designed (with a
   documented incident, CHZ-SURF-01) to be the one place hooks and tools both
   read. Guarded by `tests/test_first_forty_w3_events.py` and its own
   `unregistered()`/`phantom_tools()` self-checks. Any redesign should
   *extend* this module, never fork a parallel copy.
2. **`llm_router.hosts.events.routing_ready()`** and its guard tests
   (`test_claude_code_is_the_only_routing_ready_host_today`,
   `test_no_unearned_hook_claims`) — the only place in the repo that commits,
   in a test, to an honest and narrow claim about what auto-routing actually
   works today. This is exactly the kind of test §41 wants more of, not less.

## Top items for synthesis (this domain's best 5-10 candidates)

1. **HOST-01** (Pi host is fictional) — best deletion candidate in this
   domain: zero-risk, immediate user-facing correctness fix.
2. **MCP-01** (tool count: 60 claimed in 3 places, 41 in a 4th, actual 70) —
   strong "one source of truth" doc-ledger entry; trivially fixable by
   computing rather than hand-writing the number.
3. **HOST-02** (4 disagreeing host registries) — consolidation ledger
   candidate; canonical = `hosts/events.py` + `host_detect.py` merged, with
   `install.py`'s snippet dict validated against them by test.
4. **HOST-03 / HOST-04** (Codex/Gemini CLI auto-routing claims contradict the
   code's own `routing_ready()` verdict, in different ways) — both are
   "doc problem: incorrect mental model" (§63) candidates, not code bugs.
5. **MCP-03** (`llm_video` unreachable under the shipped default tier, and
   invisible to every guard test that would normally catch this) — small,
   concrete correctness risk with a one-line fix.
6. **MCP-04** (three independent savings/dashboard implementations:
   `llm_savings`, `llm_savings_dashboard`, `llm_dashboard`) — needs a
   follow-up grep for real callers before ranking, but is a clean
   consolidation-ledger nomination if `llm_savings_dashboard` turns out to be
   uncalled.
7. **Schema footprint numbers** (70 tools/~14.7K tokens vs 12
   tools/~3.4K tokens, measured) — hard evidence for whatever the global
   report says about "consolidated mode" being worth keeping/shipping as
   default; this is the concrete number the brief's §29 asked for and it
   was previously nowhere in the repo's own docs (`tool_tiers.py`'s "~8,000
   tokens" is closer to the *off-tier* total than a comparison).
8. **HOST-05** (9 near-duplicated per-host installer functions) — LOW
   severity but a clean §47/§71 "table vs branching" example if the global
   report wants one from this domain.

---

## Gaps / things this pass did not finish (be honest about scope)

- No worktree `CLAUDE.md` exists (confirmed via `find` — only `KIMI.md` at
  root, which is a routing-tool usage note, not a measurement-rules file).
  Proceeded using `guide/HOST_SUPPORT_MATRIX.md`, `guide/TOOLS.md`, and
  `docs/MEASUREMENT.md` as the closest analogues; flagging the missing
  `CLAUDE.md` itself as a minor §37 hygiene gap for whichever domain owns
  root-file hygiene.
- MCP-05's exact 5-function delta (75 AST-detected vs 70 registered) was not
  fully re-derived to individual names — flagged UNCERTAIN with the exact
  command to resolve it, per §1's rule against silently promoting UNCERTAIN
  to DEAD.
- Did not exhaustively grep every `hooks/*.py` file for hardcoded legacy tool
  names (the CHZ-SURF-01 regression class) — spot-checked via
  `tool_surface.py`'s own history notes but did not re-run the full sweep;
  worth a dedicated pass before any tool-removal PR ships.
- Did not deep-dive `agoragentic.py`'s 4 opt-in tools' internals (wallet/
  marketplace) — confirmed they are correctly gated off by default
  (SEC-003) and out of `tool_surface.py`'s vocabulary by design; full review
  of that subsystem belongs to whichever domain covers security/plugins.
