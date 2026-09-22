# 08 — Telemetry & Trust Audit (Phases 15, 16, 40)

Auditor: DATA/TELEMETRY specialist. Subject: `357a402e8f462f913cf9368244557eaaf7711beb`
on `fix/audit-2026-09-22`. Every probe below ran under a fresh
`export LLM_ROUTER_HOME=$(mktemp -d)` and `.venv/bin/python`, one fresh subprocess
per probe unless stated otherwise. Findings from `~/.llm-router` reads are marked
**OBSERVATIONAL**; everything else is **EXPERIMENTAL** (reproduced live in this
session, commands and output shown).

Per Hard Rule 7, nothing from `audit/2026-09-21/` or `audit/2026-09-22/` was taken
as true without independent re-derivation. Several of the findings below happen to
land on the same code the tree's own docstrings already describe (C-02, T-05, M-04,
M-06) — those docstrings were treated as *leads to re-check*, not evidence, and are
cited only after the claim was independently reproduced.

---

## 1. Headline finding: two parallel "savings" computations disagree on the same rows

**CONFIRMED. Severity: CRITICAL. This is the single most important finding in this
report.**

There are two independent code paths that turn the `usage` table into a dollar
figure, and only one of them applies the provenance/cutover fix the tree believes
it shipped.

| Path | Function | Provenance filter | Feeds |
|---|---|---|---|
| A | `cost.get_savings_by_period()` | `production_only()` → `is_simulated = 0` (fail-closed, excludes NULL) | `dashboard/server.py` (web dashboard), `tools/admin.py` (MCP admin savings tool) |
| B | `dashboard_data.query_window()` / `query_daily()` / `query_by_platform()` | **none** — no reference to `is_simulated` anywhere in `dashboard_data.py` | `ui/status_premium.py` (status bar), `tui/app.py`, `dashboard/tui.py`, `hooks/session-end.py` (end-of-session card), `hooks/codex-stop.py`, `commands/explain_dashboard.py` — path B's own comment says it "feeds ~26 reporting surfaces" |

### Reproduction

Seeded a raw `usage` table with the **pre-provenance schema** (no `is_simulated`
column — the shape every real installation had before this migration shipped) and
100 rows of realistic production activity (500 in / 300 out tokens, `$0.02` actual
cost each, `claude-sonnet-4-6`/`anthropic`). Opened it through the real `_get_db()`
(runs `MIGRATE_USAGE_ADD_SAVINGS`, `MIGRATE_SIBLING_TABLES_ADD_PROVENANCE`,
`_apply_provenance_cutover`) — this is the exact code path every upgrading
installation goes through on its next connection.

```
$ sqlite3 usage.db "SELECT COUNT(*), SUM(is_simulated IS NULL), SUM(is_simulated=0), SUM(is_simulated=1) FROM usage;"
100|100||        # all 100 rows now UNKNOWN provenance (NULL)

$ python -c 'get_savings_by_period()'
{'all_time': {'saved_usd': 0.0, 'actual_usd': 0.0, 'calls': 0, ...}, 'today': {...calls:0...}, ...}

$ python -c 'provenance_exclusion_summary()'
{'cutover_rows': 100, 'unknown_rows': 100, 'production_rows': 0, 'synthetic_rows': 0,
 'explanation': '100 usage row(s) predate provenance tracking and are excluded from
 savings totals...'}

$ python -c 'dashboard_data.query_window("lifetime")'   # SAME DB, SAME 100 ROWS
WindowTotals(calls=100, tokens=80000, cost_usd=2.0, saved_usd=-1.0, ...)
```

Path A says **0 calls, $0 spent, $0 saved**. Path B says **100 calls, $2.00 spent**,
on the identical database, in the same audit session, with the same 100 rows. An
operator sees whichever number their current screen happens to be wired to. The
status bar (path B) will show full historical activity; the web dashboard and the
`llm_router_admin` MCP savings tool (path A) will show zero, with **no explanation
surfaced on either of those two paths** — `provenance_exclusion_summary()` is wired
into exactly one place, `commands/doctor.py`; `dashboard/server.py`, `tools/admin.py`,
`dashboard_data.py`, `commands/savings_report.py` and `commands/invoice.py` never
call it.

### Consequence for "fresh install vs upgraded install"

Ran the mirror probe: 100 identical rows written through the real `log_usage()`
write path on a **brand-new** `LLM_ROUTER_HOME` (no pre-existing rows, so the
cutover has nothing to touch, and `_detect_synthetic()` stamps them `is_simulated=0`
at write time). Path A reports `calls: 100, actual_usd: 2.0` for this fresh install
— the *same shape of activity* that Path A zeroed out for the upgraded install two
paragraphs up. So: **for identical underlying activity, a fresh install and an
upgraded install report different numbers through Path A (by design — this is the
correction the cutover intends), and identical numbers through Path B (because
Path B was never updated to know the column exists).** The two paths therefore
disagree with each other by a different amount depending on install history, which
is the opposite of a coherent metric.

### Why this matters for Phase 40 (gaming)

Because Path B ignores `is_simulated` entirely, it is exactly as vulnerable to the
original C-02 defect ("1,813 test rows carried real model names, inflating savings
by $83.49") as before that fix shipped — for every one of its ~26 surfaces. The fix
protects two surfaces and leaves the rest of the estate, including the one most
users look at first (the status bar / session-end card), unprotected.

---

## 2. `is_real` filter on `routing_decisions` is a no-op in two of four call sites

**CONFIRMED (reproduced against current HEAD; the tree's own docstring at
`cost.py:719-738` asserts this and the source at HEAD matches it exactly).**

```
tools/dashboard.py:236        AND is_real = 1                                   -- real filter
commands/verify.py:251        WHERE is_real = 1 ...                             -- real filter
hooks/session-end.py:1124     WHERE timestamp >= ? AND (is_real = 1 OR is_real IS NULL)   -- ADMITS EVERYTHING
hooks/session-end.py:1137     AND (is_real = 1 OR is_real IS NULL)                          -- ADMITS EVERYTHING
```

`is_real` is `DEFAULT 1` on the column and is written to `0` by exactly one
one-time backfill migration (`MIGRATE_ROUTING_DECISIONS_MARK_CONTAMINATED`, 1,974
rows). Nothing else in the tree ever sets `is_real = 0` on a new row — the
column is not part of the live writer for `routing_decisions` (verified: no
`INSERT INTO routing_decisions` statement in `cost.py` populates `is_real`
explicitly; it silently takes the column default of `1`). The two session-end
queries that compute **burn rate, fallback %, escalation %, p95 latency, and
"routing effectiveness"** — the numbers shown at the end of every Claude Code
session — use the `OR is_real IS NULL` form, which is mathematically equivalent to
no filter at all for any row written after the one-time backfill. A demo script,
a manual `llm_route` test run from an ordinary (non-sandboxed, non-pytest) shell,
or a benchmark harness that forgets to set `LLM_ROUTER_SYNTHETIC=1` will silently
count toward this card with full apparent legitimacy.

---

## 3. `get_config()` singleton freezes `llm_router_db_path` at first call — the exact incident class, still open, one layer down

**CONFIRMED. Severity: HIGH.** This is new; it is not one of the two misses the
task brief already named (`agentic/telemetry._db_path`, `install_hooks._CLAUDE_DIR`
— both independently verified FIXED, see §5).

`config.py:373` declares
`llm_router_db_path: Path = Field(default_factory=lambda: state_path("usage.db"))`
with a comment claiming *"Resolved per instantiation... reads LLM_ROUTER_HOME at
call time."* True about the factory in isolation — false in effect, because
`get_config()` (`config.py:873`) is a **process-lifetime singleton**:

```python
global _config
if _config is None:
    with _config_lock:
        if _config is None:
            _config = RouterConfig()
return _config
```

Reproduced directly:

```
os.environ["LLM_ROUTER_HOME"] = "/tmp/audit-instance-A"
c1 = get_config()          # -> /tmp/audit-instance-A/usage.db
os.environ["LLM_ROUTER_HOME"] = "/tmp/audit-instance-B"
c2 = get_config()          # -> /tmp/audit-instance-A/usage.db   (STALE, same object: True)
paths.state_path("usage.db")  # -> /tmp/audit-instance-B/usage.db  (correct, for comparison)
```

`cost._get_db()` — the connection underneath `log_usage`, `get_savings_by_period`,
every money read in `cost.py` — opens `config.llm_router_db_path`, not
`paths.state_path()` directly. Any process that calls `get_config()` once before
`LLM_ROUTER_HOME` is set or changed (a long-lived dashboard/MCP server, a REPL, an
in-process test runner that monkeypatches `os.environ` after import, or an audit
harness that imports the package before exporting the isolation variable) will
write every subsequent `usage.db` row to the *first-seen* location for the rest of
that process's life — which is exactly the failure mode `paths.py`'s own docstring
describes as having destroyed live data in `evidence/AUDITOR_INCIDENT.md`, recurring
through the config-singleton wrapper instead of the class-attribute pattern that
was already fixed.

**Practical scope:** safe for any invocation where `LLM_ROUTER_HOME` is exported in
the shell *before* the Python process starts (this audit's own Hard-Rule-3 pattern,
one subprocess per probe, is safe). Unsafe for long-lived processes and for any
audit tooling that imports `llm_router` once and reuses the interpreter across
multiple simulated "installs" — a methodological trap for future audit rounds,
noted here explicitly.

---

## 4. `install_hooks._CLAUDE_JSON_PATH` — a third, still-open instance of the class the module claims to have fixed twice already

**CONFIRMED. Severity: HIGH (mutating, can delete a real file).**

`install_hooks.py` documents (T-15) that it fixed `_CLAUDE_DIR` and "its three
derivatives" (`_HOOKS_DST`, `_RULES_DST`, `_SETTINGS_PATH`) from plain
`Path.home()`-at-import constants to `_LazyHostPath` descriptors that re-resolve on
every access, honouring `LLM_ROUTER_CLAUDE_DIR` → `LLM_ROUTER_HOME` → `~/.claude`.
That fix is real and verified (§5). But at line 901, in the same file:

```python
_CLAUDE_JSON_PATH = Path.home() / ".claude.json"
```

a plain module-level constant, outside the `_LazyHostPath` mechanism, honouring
neither `LLM_ROUTER_CLAUDE_DIR` nor `LLM_ROUTER_HOME`. It is written to
(`.write_text`, lines 945/990) and **unlinked** (`.unlink()`, line 988) by
`_install_claude_code_cli` / its uninstall counterpart — the code path that
registers/deregisters llm_router as an MCP server in the *real* Claude Code CLI
user config.

Reproduced:

```
export LLM_ROUTER_HOME=$(mktemp -d)
python -c '
from llm_router import install_hooks as ih
print(ih._CLAUDE_JSON_PATH)                      # /Users/yaliandrona/.claude.json
os.environ["LLM_ROUTER_CLAUDE_DIR"] = "/tmp/audit-instance-claude"
os.environ["HOME"] = "/tmp/audit-instance-home"
print(ih._CLAUDE_JSON_PATH)                      # STILL /Users/yaliandrona/.claude.json
print(ih.claude_dir())                           # /tmp/audit-instance-claude (correct, for comparison)
'
```

Any install/uninstall test or probe that exercises `_install_claude_code_cli` /
its uninstall path under an isolated `LLM_ROUTER_HOME` or `LLM_ROUTER_CLAUDE_DIR`
will still write to, and can `unlink()`, the operator's real `~/.claude.json` —
which is the user's actual Claude Code CLI global config, not a test fixture.

---

## 5. Import-time path binding enumeration (Phase 16)

| Symbol | File:line | Pattern | Status at HEAD |
|---|---|---|---|
| `agentic/telemetry._db_path` | `agentic/telemetry.py:61` | function, reads `LLM_ROUTER_DB_PATH` then `paths.state_path()` | **FIXED** — reproduced: resolves live |
| `install_hooks._CLAUDE_DIR`/`_HOOKS_DST`/`_RULES_DST`/`_SETTINGS_PATH` | `install_hooks.py:188-191` | `_LazyHostPath` descriptor | **FIXED** — reproduced: `claude_dir()` follows `LLM_ROUTER_CLAUDE_DIR`/`HOME` set after import |
| `install_hooks._CLAUDE_JSON_PATH` | `install_hooks.py:901` | plain module constant, `Path.home()` at import | **NOT FIXED** — §4, reproduced, mutating |
| `claude_jsonl_usage._CC_DIR` | `claude_jsonl_usage.py:15` | plain module constant, `Path.home()/".claude"/"projects"` at import | **NOT ISOLATED** — read-only, feeds `dashboard/tui.py` "Models tab"; an isolated probe still reads the operator's real Claude Code conversation logs. Lower severity (no write), but a probe that believes itself sandboxed still ingests real data. |
| `config.RouterConfig.llm_router_db_path` | `config.py:373` + `get_config()` singleton at `config.py:873` | `default_factory`, but wrapped in a process-lifetime singleton | **NOT FIXED IN EFFECT** — §3, reproduced |
| `install_hooks._claw_code_dir` | `install_hooks.py:334-349` | function, but unconditionally reads real `Path.home()/".claw-code"` with no `LLM_ROUTER_HOME`/env override at all | **DESIGN RISK** — by-design (detects a *different* tool's real install), but means "isolated" probes still touch the real filesystem outside `LLM_ROUTER_HOME` |
| `env_registry.py` entry for `LLM_ROUTER_CP_AUDIT_PATH` → `control_plane/audit.py` | `env_registry.py:99`, `paths.py:59` | documented override variable | **DEAD/PHANTOM** — `control_plane/audit.py` does not exist in the tree; no code anywhere reads `LLM_ROUTER_CP_AUDIT_PATH`. `env_registry.py` (the tree's own "ask the code" inventory) is itself stale and cannot be trusted as ground truth without cross-checking. |
| `paths.py`'s own self-audit | `paths.py:55-60` (docstring) | — | Independently spot-checked, not re-litigated in full: confirmed 0 literal `Path.home()/".llm-router"` constructions remain at module scope in `src/llm_router/*.py` outside `paths.py`/`install_hooks.py`/`claude_jsonl_usage.py`; the ~92 files that still reference the `.llm-router` string almost all do so via `paths.state_path()`/`llm_router_home()` or a same-shaped function-local `os.environ.get("LLM_ROUTER_HOME", ...)` re-implementation (14 files: `vision_registry.py`, `trace.py`, `attempt_log.py`, `model_discovery.py`, `prompt_capture.py`, hooks `draft_usage.py`/`tool_intercept.py`(×2)/`agent_writes.py`/`direct_executor.py` — all function-scoped, all confirmed to read the env var at call time, not import time). These re-implementations bypass `paths.py`'s single resolver (a DRY violation, not an isolation bug) but were not each individually re-run through a live-probe; flagged as **HYPOTHESIS: consistent with `paths.py`, not independently reproduced for all 14.** |
| Four override env vars (`LLM_ROUTER_STATE_DIR`, `LLM_ROUTER_EXECUTION_LEDGER_DB`, `LLM_ROUTER_CP_AUDIT_PATH`, `LLM_ROUTER_DB_PATH`) | `observability/surface_status.py:35`, `execution_ledger.py:169`, (dead), `agentic/telemetry.py:73` | each falls back to `paths.state_path()`/`paths.llm_router_home()` when unset | **CORRECT fallback, confirmed by reading** — `paths.py`'s docstring phrase "none honours LLM_ROUTER_HOME" is about the *override variable name*, not the fallback behaviour; read literally it overstates the risk for `surface_status.py` and `execution_ledger.py`, which do fall through correctly. `LLM_ROUTER_CP_AUDIT_PATH` is moot (dead code). |

**Scope disclosure:** the task asks for an exhaustive enumeration of ~175+
previously-identified call sites. Given budget, this pass re-verified the specific
sites the task named as prior misses (both fixed), searched exhaustively for the
two highest-risk *shapes* (module-level `Path.home()` constants, module-level
`os.environ.get(...)`-derived path constants — zero and two hits respectively,
both reported above) across all of `src/llm_router/*.py`, and found two live ones
neither prior round had flagged (`config.py` singleton, `_CLAUDE_JSON_PATH`). It
did **not** individually execute a live-probe against each of the ~150+
function-scoped re-implementations `paths.py` mentions; those are lower risk by
construction (call-time env read) and are marked HYPOTHESIS rather than CONFIRMED
above.

---

## 6. Store inventory (condensed)

| Store | Path (relative to `LLM_ROUTER_HOME`) | Writer(s) | Reader(s) | Concurrency | Identifiers | Provenance column |
|---|---|---|---|---|---|---|
| `usage.db :: usage` | `usage.db` | `cost.log_usage` (single INSERT site) | `cost.get_savings_by_period`, `dashboard_data.query_*`, `tools/admin.py`, `commands/doctor.py` | WAL, `busy_timeout=5000` (set correctly, verified in source) | `session_id` (nullable), `correlation_id`, `user_id`/`project_id` (from git identity, best-effort) | `is_simulated` — NULL=unknown (23k pre-cutover rows + all upgraded installs), 0=production, 1=synthetic. **Read inconsistently — see Finding 1.** |
| `usage.db :: routing_decisions` | same file | multiple sites in `cost.py` | `tools/dashboard.py`, `commands/verify.py`, `hooks/session-end.py` | same connection/WAL | `route_id`/`attempt_id` (execution_ledger schema references these) | `is_real` (DEFAULT 1, one-time backfill only — **no-op in 2 of 4 readers, Finding 2**); `provenance` (`attribution.py` 3-state: runtime/unattributed/test/NULL) used by `routing_production_only()`, NOT the same column as `is_real` |
| `usage.db :: claude_usage/codex_usage/gemini_usage/savings_stats` | same file | `hooks/session-end.py`, `hooks/cc-usage-track.py` | `dashboard_data.py` (unfiltered, Finding 1), `cost.py` money functions (filtered) | same connection | host/session | `is_simulated`, added with **no default** (T-05 fix — confirmed present in migration list) |
| `usage.db :: provenance_meta` | same file | `cost._apply_provenance_cutover` (once) | `cost.provenance_exclusion_summary` → `commands/doctor.py` only | same connection | key=`usage_is_simulated_cutover` | n/a (meta table) |
| `~/.claude/projects/**/*.jsonl` | outside `LLM_ROUTER_HOME` entirely | Claude Code itself (external) | `claude_jsonl_usage.py` → `dashboard/tui.py` | file-per-conversation, append-only, external tool | Claude Code session id | none — always real host data, cannot be sandboxed by `LLM_ROUTER_HOME` (Finding, §5) |
| `~/.claude.json` | outside `LLM_ROUTER_HOME` | `install_hooks._install_claude_code_cli` | `claude mcp` CLI | file overwrite, no lock observed | n/a | none |
| `execution_ledger` DB | `usage.db` (shares file via `paths.state_path`, or `LLM_ROUTER_EXECUTION_LEDGER_DB`) | `execution_ledger.py` | not traced further this pass | — | `event_id`, `route_id`, `attempt_id`, `schema_version` | `schema_version` column exists — **not audited this pass for a version-filter denominator bug; flagged as unexamined, not cleared.** |

`execution_ledger`'s `schema_version` column is exactly the shape of denominator
bug the brief calls out by example ("a reader filtering `schema_version == 2` when
v3 rows exist"). This pass did not have budget to trace every reader of that
table; it is an open gap in this report, not a clean bill.

---

## 7. Denominator identity — `total == success + reject + dedup + error + exclusions`

Not proven to hold or break this pass. `execution_ledger.py` has the columns to
support this identity (`accepted`, `rejected`, `rejection_reason`,
`escalation_reason`, `fallback_reason`, `terminal_state`) but no query computing
routing-rate-style aggregates over this table was located and re-derived under a
live probe within budget. **Flagged as an open item, not claimed as either held or
broken — do not read the absence of a counterexample here as a clean pass.**

---

## 8. Phase 40 — metric-gaming table

| Metric | Gaming vector | Currently possible? | Evidence |
|---|---|---|---|
| Savings ($, all surfaces) | Run any script that writes to `usage`/`claude_usage`/etc. without `LLM_ROUTER_SYNTHETIC=1`, not under pytest, not in a `/tmp` or `/var/folders` cwd | **YES, for Path B (dashboard_data.py, ~26 surfaces) — unconditionally.** For Path A, only if `detect_synthetic()`'s three explicit signals are all absent (real for a manually-run demo script from a normal directory) | §1; `routing_quality.py:88-122` — signals are self-reported, not inferred, by design |
| Savings ($, status bar / session-end card) | Same as above; additionally `is_real IS NULL` admits any `routing_decisions` row not covered by the one-time 2026 backfill | **YES** | §2, reproduced against source |
| "Routing effectiveness %" / burn rate / fallback % (session-end card) | Same `is_real` no-op | **YES** | §2 |
| GT coverage / verifier coverage | Not traced this pass — no budget remaining to locate the computing function and its denominator | **UNKNOWN — not audited, do not treat as clear** | — |
| Cache hit rate (session-end card, `semantic_cache`) | `_query_cache_hit_stats()` (`hooks/session-end.py:1077`) applies **no provenance filter at all** — no `is_simulated`, no `is_real`-equivalent column referenced | **YES, if the write side does not already isolate test writes to `semantic_cache`** (write-side guard not traced this pass — flagged, not confirmed) | `hooks/session-end.py:1077-1093` read directly |
| Quota saved | Not traced this pass | **UNKNOWN — not audited** | — |
| "Fresh vs upgraded install shows the same number" (implicit trust claim) | N/A — this is the thing being tested, not a metric to game | **DISPROVEN as a claim of consistency: they show different numbers for the same activity, in both directions depending on which surface** | §1, reproduced |
| `env_registry.py` as ground truth for "which env var controls what" | Trusting the registry without grepping the target file | The registry itself contains a **dead entry** (`LLM_ROUTER_CP_AUDIT_PATH` → nonexistent `control_plane/audit.py`) | §5 |

---

## 9. Confidence summary

- **CONFIRMED, reproduced live this session:** Findings 1, 2, 3, 4; the `env_registry.py` dead entry; the two named prior misses being fixed.
- **STRONGLY SUPPORTED (source-verified, not independently re-run through a full live probe):** the 14 function-scoped `LLM_ROUTER_HOME` re-implementations; the four override-env-var fallbacks being correct.
- **DESIGN RISK:** `claude_jsonl_usage._CC_DIR`, `install_hooks._claw_code_dir` (both read real host data unconditionally; neither writes).
- **NOT AUDITED — explicit gaps, not clean bills:** the `total == success+reject+dedup+error+exclusions` identity; GT/verifier coverage; quota-saved; `execution_ledger.schema_version` reader-side filtering; `semantic_cache` write-side isolation guard.
