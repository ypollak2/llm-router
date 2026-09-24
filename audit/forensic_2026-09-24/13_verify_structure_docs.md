# Domain 13 — Adversarial Verification of Structure/Test/Recon/MCP/Docs Findings

Verifier baseline: worktree `llm-router-forensic` @ `3c96d23` (unchanged; nothing
edited, nothing deleted). All commands run with
`HOME=$(mktemp -d) PYTHONPATH=src <.venv 3.11.15>/python`. No paid APIs, no
ollama, no full-suite pytest run — targeted files/one-off scripts only. Every
verdict below is backed by a command I ran myself in this session, not by
re-reading the original auditor's prose.

---

## STR-001 — `hosts.HostAdapter` uncalled by `install.py`

**Verdict: CONFIRMED. Severity HIGH is right.**

`grep -rln "llm_router\.hosts\b\|from llm_router import hosts\|from \.hosts\b\|from \.\.hosts\b" src tests` returns
only `src/llm_router/hosts/__init__.py`, `src/llm_router/hosts/hook_io.py`
(both self-referential) and 9 test files. Zero hits in `src/llm_router/commands/install.py`
or anywhere else in `src/`. `install.py`'s own text search for `hosts.` only
matches an unrelated local variable name (`installed_hosts`). The abstraction
is real (`base.py` Protocol + `cursor.py` concrete adapter, 5 files) and is
genuinely never invoked by the shipping installer. No basis to refute.

## STR-003 — 63 files bypass `storage/`, hand-roll `sqlite3`/`aiosqlite`

**Verdict: CONFIRMED, count exact.**

`grep -rl "import sqlite3\|import aiosqlite" src/llm_router --include="*.py" | grep -v "^src/llm_router/storage/" | wc -l` → **63**, matching the finding's number exactly (double-checked with a stricter import-statement-anchored grep too, still 63 once `tools/fs.py` — a top-of-block import — is counted consistently). `storage/service.py` + 3 adapters are real, well-shaped, and adopted by essentially one caller (`budget_store.py`) per the finding's own citation. Not refuted.

## `cache/store.py::SemanticCache` — "proven dead" claim

**Verdict: CONFIRMED.**

`cache/store.py`'s `SemanticCache`/`SemanticCacheEntry` have exactly one
consumer anywhere in the tree: `cache/__init__.py`'s own re-export
(`from llm_router.cache.store import SemanticCache, SemanticCacheEntry`). No
other file references them. The real semantic cache lives in the unrelated
top-level `semantic_cache.py`, imported module-style (`from llm_router import
semantic_cache`) from `router.py` (two call sites: `router.py:2427-2428` store,
`router.py:4357-4363` check) plus schema/dashboard/cost-report wiring in
`cost.py`, `dashboard/server.py`, `env_registry.py`, `hooks/session-end.py`.
The naive `grep "semantic_cache import"` I first tried under-counted this
because the real code imports the *module*, not a name from it — worth noting
as a methodology trap, but it doesn't change the verdict: `cache/store.py`'s
class is dead, `semantic_cache.py`'s is live. Confirmed, not refuted.

## TST-01 — namespace-package false positive in `_no_module_state_leak`

**Verdict: CONFIRMED, fix verified to work, no regressions found. Severity: HIGH is defensible for triage cost, though true production/user impact is zero (CI runs the full suite) — I'd log it as MEDIUM-HIGH rather than pure HIGH, but would not fight the HIGH call.**

1. Reproduced the raw failure live: `pytest tests/test_first_forty_w2.py -q`
   alone → `ERROR at teardown of test_quota_absent_is_not_reported_as_zero`:
   *"this test left a fileless module stub on a package object: llm_router.ui"* —
   exact match to the finding.
2. Confirmed none of the six directories (`ui`, `hooks`, `policies`, `static`,
   `rules`, `commands`) has `__init__.py`.
3. **Actually applied the proposed fix** (not just reasoned about it): copied
   `src/` + `tests/` + `pyproject.toml` to an isolated scratch tree, added
   empty `__init__.py` to all six directories, reran
   `pytest tests/test_first_forty_w2.py -q` → **17/17 pass, zero errors**. This
   is the one finding in this batch I didn't just read code for — I ran the
   fix and watched it work.
4. Checked whether "namespace packages might be intentional because hooks run
   as scripts" (the concern I was asked to weigh): `hooks/` contains a mix of
   hyphenated files (`auto-route.py`, `codex-post-tool.py`, `agent-route.py`,
   …) that are **never importable via dotted path regardless of `__init__.py`**
   (hyphens aren't valid Python identifiers) — these are shipped by
   `shutil.copy2` into host config dirs and invoked as standalone scripts, so
   they have nothing to do with this question either way. The underscore-named
   files in the same six directories (`hooks/agent_loop.py`,
   `ui/session_summary.py`, `commands/doctor.py`, etc.) *are* imported via
   normal dotted paths today, both in production code and 9+ test files, and
   continued to import fine after adding `__init__.py`
   (`import llm_router.hooks.agent_loop`, `import llm_router.commands.doctor`
   both succeeded in the scratch tree).
5. Checked for any dependency on PEP 420 namespace-package *merge* semantics
   (multiple directories contributing to one namespace across `sys.path`):
   `grep -rn "pkgutil.extend_path\|namespace_packages\|find_namespace\|__path__"`
   → zero hits. Packaging (`pyproject.toml`'s `[tool.hatch.build.targets.wheel]`)
   uses `packages = ["src/llm_router"]` — hatchling bundles the whole tree as
   one unit regardless of `__init__.py` presence, so there is no packaging
   dependency on these being namespace packages either.
6. Re-ran the two-file order-dependence A/B in the scratch tree
   (`test_session_summary_xaxis.py` then `test_first_forty_w2.py`) and the
   already-patched `test_s7_rendered_output_is_rendered.py` (7/7 pass) — no
   regression from the fix.

Conclusion: the namespace-package status of these six directories is
**accidental, not intentional** — nothing in the codebase relies on it, the
fix is a pure win, and I verified that empirically rather than by inspection
alone. Confirmed, not refuted; if anything the finding under-claims how safe
the fix is (its own "Validation required" hedge can be closed out — I closed it).

## REC-004 — three scripts fail to compile on Python 3.11

**Verdict: CONFIRMED, byte-for-byte.**

`.venv/bin/python --version` → 3.11.15. `python -m py_compile` on all three
files reproduces the exact reported `SyntaxError: f-string expression part
cannot include a backslash` at the exact reported lines
(`scripts/gen_cast.py:31`, `scripts/dev/gen_cast.py:31`,
`scripts/bench_session_replay.py:138`). Confirmed CI blindness too:
`.github/workflows/ci.yml:28` and `publish.yml:29` both run
`ruff check src/ tests/` — `scripts/` is out of scope, so this can pass CI
green forever. Also confirmed the "known, never fixed" claim: `git log` shows
HEAD is `v15.1.0-23-g3c96d23` (23 commits past the last tag) and
`audit/2026-09-22/DEAD_AND_SUSPICIOUS_CODE.md:107-108` already names these
exact three files. Nothing to refute; severity HIGH is justified (broken at
the interpreter level, silently, for two release cuts).

## REC-001 — `service.py` hardcodes `version="5.3.0"`

**Verdict: CONFIRMED.**

`src/llm_router/service.py:42` (`FastAPI(title=..., version="5.3.0")`) and
`:329` (`/health` response `"version": "5.3.0"`) both confirmed live. Real
version resolved dynamically: `llm_router.__version__` → `15.1.0` (matches
`pyproject.toml`'s `version = "15.1.0"`). The 10-major-version drift claim
checks out exactly. Severity MEDIUM is fair — no security/correctness impact,
pure health-check truthfulness.

## HOST-01 — "Pi (pi.dev)" documented host with no working install path

**Verdict: CONFIRMED by live execution, with one nuance the original finding missed.**

Ran the actual documented activation command against this checkout:
`llm-router install --host pi` (via `python -m llm_router.cli install --host pi`)
→ **`Unknown host(s): pi` / `Valid options: codex, desktop, copilot, opencode, gemini-cli, copilot-cli, openclaw, trae, factory, vscode, cursor, windsurf or 'all'`** — no `pi` anywhere in the list. This is the real `llm-router install` entry point (`pyproject.toml`'s `llm-router = "llm_router.cli:main"` → `main()` dispatches `install` to `llm_router.commands.install.cmd_install`), so a Pi user following the docs hits exactly the failure HOST-01 describes.

**Nuance not in the original finding**: `src/llm_router/cli.py` (a separate,
older `_install_host()`/`_install_pi_files()` implementation, distinct from
`commands/install.py`'s function of the same name) *does* have real Pi
support — `~/.pi/agent/mcp.json` merge logic exists and is exercised by
`quickstart.py` (`llm-router-quickstart` console script) and by tests
(`test_cost_host.py`). But `host_detect.py`'s auto-detect table (3 hosts,
no `pi`) means quickstart's auto-detected host is never `"pi"` in practice, so
this code path is reachable only via the legacy `_install_host("all")` loop or
a direct test call — not through anything a Pi user would discover. This
doesn't refute HOST-01 (the documented, primary `--host pi` command genuinely
fails), but it means "zero code anywhere" is slightly overstated — the more
precise finding is "the shipping `llm-router install --host pi` command fails;
a separate, effectively unreachable legacy code path has partial support."
Severity HIGH stands — the user-facing failure is real and reproduces exactly
as described.

## MCP-01 — tool count claims (60 documented, actual higher)

**Verdict: CONFIRMED, count reproduced independently.**

Live-registered `mcp.list_tools()`-equivalent under `LLM_ROUTER_SLIM=off`:
imported `routing, text, media, pipeline, admin, subscription, codex,
gemini_cli, setup, dashboard, fs, agents, agentic, local_task, consolidated`
and called `.register()` against a fake MCP object → **70 unique tool names,
zero duplicates**. `server.py`'s docstring, `README.md:359,556`,
`guide/TOOLS.md:3`, `guide/QUICKSTART_2MIN.md:168` all say "60". None of the
three documented numbers (60, 60, 41-for-`tool_tiers.py`) is correct. DOC-01/
DOC-08 (same root cause, stale `server.py` docstring) confirmed on the same
evidence. Severity MEDIUM fair for DOC-01/MCP-01 — no behavior is wrong, only
the documented count.

## DOC-02 — 11 vs 12 front-door (consolidated) tools, missing `llm_local_task`

**Verdict: CONFIRMED.**

`tool_surface.CONSOLIDATED_TOOLS` (live import) has 12 members including
`llm_local_task`. `README.md:360` and `guide/TOOLS.md:5` both say "11
front-door tools" and `guide/TOOLS.md`'s enumerated list omits
`llm_local_task`. This is the highest-capability default-on tool (file
writes/command execution per the finding) and it is the one omitted — the
security-relevance framing in the original finding holds up on inspection of
`tools/local_task.py`'s own capability set. Confirmed.

## HOST-03 / HOST-04 vs DOC-03 — resolving the apparent Codex contradiction

**Verdict: both HOST-03 and DOC-03 are CONFIRMED and are NOT actually in
conflict — they are each right about a different, narrower question, and the
doc itself conflates the two. HOST-04 also CONFIRMED. Net: the doc is worse
than either single finding suggests (at least 4 mutually inconsistent claims
about Codex in one file, not 2).**

Traced the actual code:

- `src/llm_router/hosts/events.py`'s `CODEX` entry: `events` dict **does**
  map `Event.PRE_TOOL: "PreToolUse"` (the event name is known) but
  `tool_name_key=None` / `tool_input_key=None`, with an explicit code comment:
  *"Tool-event keys are still unverified: no PreToolUse payload has been
  captured, so a port must not read them yet."* `prompt_key="prompt"` /
  `session_key="session_id"` **are** verified (captured from a real `codex
  exec` payload, per the adjacent comment).
- `routing_ready(host)` (`events.py:243-255`) requires **all three**:
  prompt-block support, PRE_TOOL event support, AND zero unverified fields.
  For Codex, the third condition fails (`tool_name_key`/`tool_input_key`
  unverified) → `routing_ready("codex")` is **False**. HOST-03/HOST-04's
  reading of this function is accurate.
- Separately, `commands/install.py:639-695` really does copy the shared
  `auto-route.py` to `~/.codex/hooks/codex-auto-route.py` and wires it to
  Codex's `UserPromptSubmit` event by default. This is a real, shipped,
  default-on **prompt-level push-routing hook** — DOC-03's claim that "Codex
  hooks are shipped, not coming" is also accurate, for *this* capability.
- **These are two different capabilities being described by one doc cell.**
  `routing_ready()` measures whether Codex can do full enforcement
  (prompt-routing hint **plus** verified PreToolUse tool-argument rewrite).
  The installer's shipped hook only does the first half (prompt-level ⚡ ROUTE
  hint via UserPromptSubmit) — real, live, default-on, but not the same thing
  `routing_ready()` is asking about.
- Reading the actual doc file top to bottom surfaced **more contradiction
  than either domain report flagged individually**: `guide/HOST_SUPPORT_MATRIX.md`
  line 9 says Codex "🔜 Not yet"; line 21 defines 🔜 as "the host *supports*
  this; llm-router has not shipped it" (false — it has shipped it); line 36
  says Codex's UserPromptSubmit hook is "shipped"; line 86 has a section
  header "🟢 Codex CLI (Full)"; but lines 297-301/314/330/363/369 elsewhere in
  the *same file* say "Codex CLI ❌ (explicit MCP calls only)" / "⚠️ (routed
  MCP calls only)" for "Auto-Routing Hooks." That is at least **four**
  mutually inconsistent characterizations of the same capability in one
  document, not the two-way contradiction either domain report cited.

**Implication for DOC-03's recommended fix**: DOC-03 recommends changing the
summary-table cell to "Full auto-routing via hooks." Given `routing_ready()`'s
honest **False** verdict for Codex (PreToolUse enforcement unverified), that
exact replacement wording would introduce a new overclaim of its own. The
correct fix is narrower than DOC-03 proposes: describe Codex as shipping
default-on **prompt-level** auto-routing (true) while being explicit that
tool-argument-rewrite enforcement is unverified/not ready (also true) —
matching HOST-04's recommendation to give `routing_ready()` a
non-boolean/tiered output rather than picking one of the two existing
one-bit claims. DOC-03's factual observation (the doc self-contradicts, and
"not yet" understates real shipped behavior) is CONFIRMED and HIGH severity
is justified by real user misdirection; its specific proposed replacement
text is not quite right and should be corrected before landing.

---

## Verdict summary

| Finding | Verdict | Severity |
|---|---|---|
| STR-001 (hosts/ uncalled) | CONFIRMED | HIGH |
| STR-003 (63-file storage bypass) | CONFIRMED (count exact) | HIGH |
| cache/store.py SemanticCache "proven dead" | CONFIRMED | (component of STR-item, LOW risk to delete) |
| TST-01 (namespace-package false positive) | CONFIRMED, fix verified by actually running it | MEDIUM-HIGH (not pure HIGH — zero production impact, real triage-cost impact) |
| REC-004 (3 scripts fail py_compile on 3.11) | CONFIRMED, byte-for-byte | HIGH |
| REC-001 (service.py hardcoded 5.3.0) | CONFIRMED | MEDIUM |
| HOST-01 (Pi host has no working install path) | CONFIRMED live; "zero code anywhere" slightly overstated (dead legacy cli.py path exists) | HIGH |
| MCP-01 (tool count: 70 actual vs 60/60/41 documented) | CONFIRMED, reproduced 70 independently | MEDIUM |
| DOC-01/DOC-08 (60 vs 70, one stale docstring root cause) | CONFIRMED | MEDIUM/LOW |
| DOC-02 (11 vs 12 front-door tools, missing llm_local_task) | CONFIRMED | MEDIUM |
| HOST-03 (Codex doc self-contradicts) | CONFIRMED, doc is worse than described (4-way, not 2-way, contradiction) | HIGH |
| HOST-04 (Gemini CLI has no PRE_TOOL event, doc says "Full") | CONFIRMED | MEDIUM |
| DOC-03 (Codex hooks shipped, doc says "coming") | CONFIRMED on the facts; its proposed replacement text ("Full auto-routing via hooks") would itself overclaim against routing_ready()==False — needs narrower wording | HIGH (impact), fix text needs correction |
