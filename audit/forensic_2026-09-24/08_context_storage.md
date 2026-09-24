# BLOCKED BY PLAN MODE — audit deliverable could not be written to its designated path

This session is running under a subagent task ("auditor 08 — Context/knowledge/memory and
persisted state") for a forensic llm-router audit. The task required writing the deliverable to:

    /Users/yaliandrona/Projects/llm-router-forensic/audit/forensic_2026-09-24/08_context_storage.md

Partway through the investigation, a system-reminder announced Plan Mode is active for this
session, restricting me to read-only tools plus writes to this plan file only. Per the standing
rule that a system/harness-level restriction overrides task instructions, I stopped short of
writing the actual deliverable file and completed the investigation read-only instead. Everything
below is the real, evidence-backed audit content — it only needs to be transcribed to the
designated path (or I can be un-blocked to write it directly).

Methodology note: `sqlite3 -readonly` fails to open any `~/.llm-router/*.db` file in this sandboxed
bash environment (confirmed even against a plain `/tmp` copy — likely a sandbox syscall
restriction, not a repo defect). Workaround used throughout: copy each db file into the scratchpad
(`/private/tmp/claude-501/.../scratchpad/dbcopies/`) and query the copy without `-readonly`. No
write ever targeted the real `~/.llm-router` files. No CLAUDE.md exists in the forensic worktree
root (checked; only a Trae-IDE `.rules` file exists there, unrelated) — proceeded without it.

---

## Overview

Domain 08 covers context/knowledge/memory and all persisted state. The single dominant fact: the
"routing decision" / "usage record" concept is persisted through **at least four independent
mechanisms** (two of them fully dead in real-world use), schema evolution happens through **two
uncoordinated systems** (one formal, one ad-hoc, the ad-hoc one carrying 25+ changes), and at least
three other concepts ("session," "budget," "lineage") each have their own multi-implementation
sprawl. Separately, the on-disk knowledge store has grown to 300 MB / ~12.7k directories, of which
essentially all of the growth is test-fixture debris, not real user data. None of this needs new
abstraction to fix — it needs deletion of the dead paths and consolidation onto the one each
concept's live evidence already picked.

---

## §16 deliverable: every persisted store, inventoried

### SQLite databases in `~/.llm-router/` (8 files)

| File | Tables | Live rows (see below) | Owner module(s) |
|---|---|---|---|
| `usage.db` | 16 (incl. `sqlite_sequence`) | Yes — primary store | `cost.py` (4,296 lines), `quota_savings.py`, many readers |
| `routing.db` | 13 | **0 in every table** except `provenance_meta`=1 | none in current `src/` — orphaned |
| `routing_lineage.db` | `routing_decisions`, `lineage` | **0 / 0** | `lineage/lineage_store.py`, `hooks/lineage_integration.py` |
| `sessions.db` | `sessions` | **0** | `agents/session.py` |
| `budgets.db` | `envelopes`, `budget_spend_events` | **0 / 0** | `budget_backend.py` family |
| `receipts.db` | `receipts` | 14,813 | (not traced further — outside primary evidence chain, flagged for synthesis) |
| `audit.db` | `audit_events` (hash-chained, tamper-evident) | **0** | `storage/adapters/sqlite_adapter.py`, `storage/service.py` |
| `result_cache.db` | `results` + FTS5 shadow tables | 78 (FTS in sync: `results_fts_docsize`=78) | semantic/result cache path |

Row counts obtained by copying each db to the scratchpad and running `select count(*)` per table
(exact commands and outputs captured in this session's tool transcript). `usage.db` table-by-table:
`usage`=312, `claude_usage`=37,878, `routing_decisions`=1,605, `savings_stats`=8,977,
`semantic_cache`=0, `corrections`=0, `model_quality_trends`=462, `benchmark_results`=0,
`codex_usage`=0, `gemini_usage`=0, `quota_snapshots`=3,058, `session_summaries`=0,
`execution_events`=2,349, `compression_stats`=1,188, `provenance_meta`=1.

### JSON / JSONL / lock files directly under `~/.llm-router/` (partial enumeration, root has 1,113 entries)

- `attempts.jsonl` (60 KB) + `attempts.jsonl.lock` (0 bytes, present) — lock-file pattern confirmed.
- `attempts.test.jsonl` (122 KB) — a *test* artifact living permanently in the *real* state dir.
- `model_tracking.jsonl` — 9,075 lines, last written today. See ST-03 below: this is the file the
  code's own comments call "legacy," yet it is the only one of the four routing-decision stores with
  real, current, actively-read data.
- `routing_lineage.jsonl` — **does not exist on disk** despite `LineageStore.__init__` being the kind
  of thing that would create it (see ST-02).
- `ground_truth_candidates.jsonl` (+ `.funnel.jsonl`, `.lock`), `gt_accumulation.jsonl`,
  `coverage.jsonl`, `intercepts.jsonl`, `fail_open.jsonl`, `direct_samples.jsonl`,
  `community_export.jsonl` (0 bytes) — each a distinct JSONL fact-stream with its own writer.
- `discovery.json` plus two `discovery.json.bak-<timestamp>` files, and `vision_models.json` plus a
  matching `.bak-<timestamp>` — confirms a save-with-timestamped-backup pattern used for at least
  two files; not confirmed as atomic (rename-based) vs. copy-then-write from this evidence alone.
- Per-session/per-agent shard families (see ST-09 for the growth finding):
  `transcript_<id>.jsonl` × **172**, `last_route_<id>.json` × **813**, `turn_blocks_<id>.json` × 15,
  `agent_depth_<id>.json` × 12, `last_classification_<id>.json` × 1, `violations_<id>.json`.
- `current_session.json` — single active-session pointer (`{"session_id": ..., "ts": ...}`).
- `session_snapshots/<hour>.json` — 144 files (hourly quota/usage snapshots going back to 19 Aug).
- `knowledge/` — see §15 below.
- `.env`, `broker.secret` (both `0600`) — credential-shaped files, correctly permissioned.

### Concurrency evidence (from source, not inference)

`lineage/lineage_store.py:18-31` documents a measured concurrency fix: rollback-journal mode left
only 1.1x headroom against Python's 5s default busy-timeout under 60 threads × 40 writes (slowest
writer 4.41s), so WAL + a 30s explicit busy-timeout was adopted (20x headroom, 1.50s slowest
writer). The comment states the same pattern is duplicated in `budget_backend.py` and
`result_cache.py` — i.e. each SQLite-backed store independently re-derived and re-documented the
same WAL/busy-timeout fix rather than sharing one connection helper.

---

## §35 deliverable: migrations

- Formal framework: `src/llm_router/migrations/__init__.py` (366 lines) +
  `migrations/versions/001_create_llm_router_health.py` (55 lines) — **one migration, one table**
  (`llm_router_health`).
- Everywhere else, schema evolution is ad-hoc and duplicated: 8 files independently implement their
  own "PRAGMA table_info → ALTER TABLE ADD COLUMN if missing" idempotency check:
  `cost.py`, `session_spend.py`, `agents/session.py`, `lineage/lineage_store.py`,
  `hooks/session-end.py`, `semantic_cache.py`, `execution_ledger.py`. `cost.py` alone contains
  **25+** hand-written `ALTER TABLE ... ADD COLUMN` statements evolving `usage`, `claude_usage`,
  `routing_decisions`, `savings_stats`, `codex_usage`, `gemini_usage` over what is visibly a long
  history (columns like `is_simulated`, `correlation_id`, `cache_hit` were added well after initial
  `CREATE TABLE`, confirmed by the literal on-disk schema dump showing columns appended after a
  `CREATE TABLE (...)\n, extra_col ...)` — the telltale shape SQLite leaves when `ALTER TABLE ADD
  COLUMN` runs after the original DDL).
- `lineage_store.py:198-215` shows the same pattern for the `lineage` table's v0.0.2 columns, with an
  explicit comment explaining *why* order matters ("SQLite refuses to index a column that doesn't
  exist on the table yet").
- No downgrade path found anywhere in either mechanism (consistent with the brief's expectation —
  not flagged as a defect on its own, just noted as absent).

**Finding**: the formal `migrations/` package is not the source of truth for schema evolution; it
governs a single, apparently low-traffic table while the core `usage.db` schema (the one with real,
heavily-used data) evolves through scattered, independently-reimplemented idempotency checks.

---

## §15 deliverable: context/knowledge/memory concept map

| Concept | Storage | Live? | Notes |
|---|---|---|---|
| OKF (repo knowledge) | `~/.llm-router/knowledge/{projects,models}/` | Yes, but 99.5% test debris (ST-08) | `okf.py` (1,376+ lines) |
| Injection choke point | in-process only (no store) | Yes, enforced by test | `context_injection.py` — see CTX-01/02 |
| Repo state (git head/branch/uncommitted) | in-process, re-derived every call | Yes | `repo_facts.py`, injected fresh each call, never persisted (explicitly "cannot compound like a remembered fabrication" per code comment) |
| Session conversation context | `session_store.build_session_context` | Yes | reads session transcript, privacy-gated (see CTX-03) |
| Semantic layer | attaches at the same choke point | Off by default | `semantic/modes.py` — "shadow mode is byte-identical to off" per its own comment |
| Routing decisions / lineage | 4 separate stores (see ST-01/02/03) | 1 of 4 live | — |
| Historical/session memory | 6+ separate mechanisms (see ST-05) | 2 of 6 empty | — |

### CTX-01 — Cross-project contamination: root cause confirmed in source, mitigation confirmed live

**Category**: Context/Privacy · **Severity**: LOW (mitigated) · **Confidence**: HIGH
**Location**: `src/llm_router/context_injection.py:44-46`
**Observation**: The module's own docstring states the exact failure mode the brief asked about:
"the MCP server's cwd is $HOME, which has no .git, so unscoped retrieval has previously pulled one
project's documents into another project's prompt." The fix is that `inject()` requires a `root`
parameter to scope `okf.find_relevant`/`project_root`, and `router.py` (the MCP call site) now
passes it.
**Why this matters**: confirms the memory note ("llm-router context not delivered ROOT CAUSE
2026-09-10... server cwd=$HOME") is real and understood by the current maintainers, and that the
fix is the `root` parameter, not a broader architectural change.
**Recommended action**: KEEP. This is a do-not-change candidate — the fail-open, single-choke-point
design (`context_injection.inject`) plus `tests/test_okf_choke_point.py` enforcing every execution
path imports it is a genuinely good pattern.
**Is behavior currently used?** YES — confirmed 6 real call sites (`tools/local_task.py:227`,
`gemini_cli_agent.py:182`, `hooks/agent_loop.py:640`, `hooks/direct_executor.py:521`,
`codex_agent.py:294`, `claude_agent.py:152`) plus `router.py` (exempted from the test's list because
it "predated the choke point" but the code comment says it "now crosses inject() like every other
path").

### CTX-02 — Stale docstring contradicts the code's own enforced state

**Category**: Documentation/Comments · **Severity**: MEDIUM · **Confidence**: HIGH
**Location**: `src/llm_router/context_injection.py:1-16` (module docstring); contradicted by
`tests/test_okf_choke_point.py:1-13` (its own module docstring) and the `EXECUTION_PATHS` list
(6 entries, all present, all passing per the file's own logic) plus `router.py`'s exemption comment.
**Observation**: Both `context_injection.py` and `test_okf_choke_point.py` open with the identical
claim "OKF reached 2 of 7 execution paths... The tool loop, the Codex agent, the Claude agent, the
Gemini agent and `llm_local_task` all ran blind" — written in the present tense, describing a
historical bug that the same files' own logic shows is now fixed (all 6 listed paths + router.py
import/call `context_injection`).
**Why this exists, if discoverable**: The docstring was written to justify *why* the choke point
exists (a real historical incident: "101 drafts, zero used, a third asserting statuses they had no
way to observe") and was never updated after the fix landed.
**Why this matters**: A maintainer reading either file today is told the bug still exists. This is
exactly the "comment contradicting code" class from §39 — low user impact, but it actively misinforms
whoever next has to reason about this subsystem, including a future audit.
**User-visible impact**: none. **Engineering impact**: wastes a future maintainer's time re-verifying
something already fixed and tested.
**Recommended action**: SIMPLIFY — update the docstring to past tense / point at the test as the
ongoing guarantee, rather than restating a fixed bug as current. Trivial edit, zero behavioral risk.
**Behavioral compatibility risk**: none (doc-only). **Validation required**: none beyond re-reading.

### CTX-03 — Measured: tokens injected before a routed model sees the task (clean HOME)

**Category**: Context/Performance · **Severity**: INFORMATIONAL · **Confidence**: HIGH (for the
number produced), MEDIUM for real-world generalization (see gap below)
**Method**: `HOME=$(mktemp -d) PYTHONPATH=.../src python3 -c "context_injection.inject(prompt,
root='/Users/yaliandrona/Projects/llm-router-forensic')"` with prompt = "How do I add a new
provider to the router and wire up its pricing?" (66 chars).
**Result**: output grew from 66 → 349 chars (≈70 tokens added at 4 chars/token), consisting entirely
of a `<repo_state>` block (git head/last_commit/uncommitted/changed files) rendered by
`repo_facts.render()`. **Zero** OKF concepts were injected, because a clean `HOME` has no
`~/.llm-router/knowledge/` index to retrieve from.
**Gap, stated honestly**: this measures the *floor* (repo-state overhead only), not what a real user
with the live 300 MB knowledge store sees per prompt — I did not run `inject()` against the real
`~/.llm-router` because `okf.find_relevant`/`inject_context`/session dedup (`_seen_this_session`)
have side-effect-shaped names and I could not confirm zero-write behavior without reading substantially
more of `okf.py` than the time budget allowed, and the task rules treat `~/.llm-router` as read-only.
**Recommended action**: a follow-up measurement (point `LLM_ROUTER_HOME`/equivalent at a *copy* of
the real knowledge dir, not the live one) would give the real per-prompt token cost; flagging as
OBSERVATIONAL/incomplete rather than asserting a number I did not measure.

---

## Findings register

### CTX- (context/knowledge/memory)

**ID: CTX-01** — see above. **Recommended action: KEEP.**

**ID: CTX-02** — see above. **Recommended action: SIMPLIFY (doc fix).**

**ID: CTX-03** — see above. **Recommended action: KEEP measurement gap open / follow-up.**

**ID: CTX-04**
Category: Context/Storage growth · Severity: HIGH · Confidence: HIGH
Location: `~/.llm-router/knowledge/projects/` (data, not code — but code has no visible retention
policy for it; `okf.py` has `gc_store()` at line 1376 but see below)
Observation: `du -sh ~/.llm-router/knowledge/` = 300 MB (matches the brief's reported ~298 MB
independently). `find ... -type d` under `knowledge/` = 12,694 directories total; 5,404 directly
under `knowledge/projects/`. Of those 5,404: **2,451** match test-fixture naming
(`test_*`, `tmp*`, `fixture*`), and a further **~2,926** are opaque `repo-<8-hex>` directories
(sampled listing shows them interleaved with the test-named ones, consistent with anonymized test
fixtures rather than real user checkouts). Only **27** directories are neither test-named nor
`repo-<hash>`-named — i.e., roughly 0.5% of the project-knowledge store looks like genuine user data.
Why this matters: the real user's knowledge store — the thing OKF injection reads from on every
routed call — is >99% test-run debris. This directly degrades the thing §15 asks about
(injection quality/relevance) and inflates disk/scan cost for no benefit.
Is behavior currently used? YES — `okf.py:1326 scan_store()` / `1376 gc_store()` exist, meaning a
GC mechanism is present in the code, but its presence doesn't answer whether it's ever invoked
automatically; I did not find a scheduled/automatic caller in the time available (UNCERTAIN, not
asserted dead).
Recommended action: SIMPLIFY / DEPRECATE stale entries — wire `gc_store()` (or an equivalent
test-fixture-path filter) to run automatically, or have the test suite write fixtures to a
`LLM_ROUTER_HOME` override instead of the real `~/.llm-router` (root-cause fix; this is very likely
a test-isolation bug, since production code has no reason to create thousands of `test_*`-named
project directories).
Validation required: confirm whether `pytest` fixtures/conftest already set `LLM_ROUTER_HOME` for
most tests (root `conftest.py` is 10,697 bytes — worth a targeted read by whoever owns test
infrastructure) and why ~5,377 of 5,404 project dirs escaped that isolation.
Dependencies on other findings: none directly, but overlaps whichever auditor covers `conftest.py`
and test isolation.

### ST- (state/storage)

**ID: ST-01**
Category: Storage/Duplication · Severity: HIGH · Confidence: HIGH
Location: `~/.llm-router/routing.db` (data); referenced in `guide/TROUBLESHOOTING.md:284,287,293,548`
and `guide/HOST_SUPPORT_MATRIX.md:343-344`; absent from `src/llm_router` (`grep -rln 'routing\.db'
src/` → zero hits; `grep -rln '"routing\.db"' ` also zero).
Observation: Two user-facing docs instruct the reader to inspect/delete `~/.llm-router/routing.db`
as *the* shared SQLite store ("there's no separate env var to configure this... they all read/write
the same `~/.llm-router/routing.db`" — HOST_SUPPORT_MATRIX.md:344) and as the fix for a stuck
circuit breaker ("delete `~/.llm-router/routing.db` and restart" — TROUBLESHOOTING.md:548). On this
real, many-months-old install, `routing.db` exists (created 19 Aug, last touched 22 Sep) but **every
one of its 13 tables has 0 rows** except `provenance_meta` (1 row, a schema marker written at file
creation). No current source file references the literal string `"routing.db"`; all ~35 current
call sites (config.py:402, paths.py, every hook, every command) target `usage.db` instead.
Why this exists, if discoverable: `routing.db`'s schema is a strict subset of `usage.db`'s (missing
`session_summaries`, `execution_events`, and several columns added later — e.g. `usage.db`'s
`routing_decisions` has `session_id`, `prompt_sequence`, `subject`, `provenance` that `routing.db`
lacks). This is consistent with a rename from `routing.db` → `usage.db` at some point in the
project's history that updated the code but not these two docs.
Why this matters: a user following TROUBLESHOOTING.md's documented fix for "stuck circuit breaker"
deletes a file that was never being written to, gets no error, and reasonably concludes the problem
is fixed — when the actual state (in `usage.db`) is untouched. This is a **misleading, no-op
troubleshooting step given to users today**.
User-visible impact: HIGH (false confidence a documented fix worked). Engineering impact: low to fix
(doc-only + optionally delete the orphaned file).
Is behavior currently used? NO (the file itself, proven by 0 rows across all 13 tables + no code
reference).
Recommended action: DELETE the two doc references (correct them to `usage.db`); DELETE the orphaned
`routing.db` file itself as a one-time cleanup (safe — it is empty). Proposed target: single
mention of `usage.db` in both docs.
Behavioral compatibility risk: none. Security risk: none. Validation required: grep confirmed above;
recommend a second pass over `guide/` for any other `routing.db` mentions before shipping the doc fix.

**ID: ST-02**
Category: Storage/Duplication · Severity: HIGH · Confidence: HIGH
Location: `src/llm_router/lineage/lineage_store.py` (586 lines), `src/llm_router/lineage/decision_logger.py`
(124 lines), `src/llm_router/hooks/lineage_integration.py`; data: `~/.llm-router/routing_lineage.db`
Observation: `LineageStore` is documented (module docstring, and inline at lines 50-55, 118-129,
296-310) as a "dual-write" system — every call to `.append()` is supposed to write both
`routing_lineage.jsonl` (real-time) and the `routing_decisions` table in `routing_lineage.db`
(analytics), with a second, "planned-API" table `lineage` populated by a separate `.record()`
method. On the real, live install: `routing_lineage.jsonl` **does not exist on disk at all**, and
both `routing_decisions` and `lineage` tables inside `routing_lineage.db` have **0 rows**, despite
the `.db` file itself existing (49 KB, last touched 24 Sep 09:37 — i.e., something calls
`LineageStore()`'s constructor, which runs `_init_db()` and creates the empty schema, but nothing
ever calls `.append()` or `.record()`).
Why this exists, if discoverable: the code's own comment at line 296-310 explains the actual
history: "the v0.1.x rewrite added the new store but never migrated the production write path" — the
production auto-route hook writes to `model_tracking.jsonl` instead (see ST-03). This is the
project's own documented admission that this store never got wired up.
Why this matters: ~700+ lines of code (store + decision_logger + hook integration) implement a
dual-write, WAL-tuned (30s busy-timeout, explicitly measured concurrency headroom, see §16
concurrency note), migration-aware (v0.0.2 column backfill) storage system that has recorded zero
production events in a long-lived real install.
User-visible impact: none currently (nothing reads it that matters, since `.recent()` falls back to
the legacy JSONL — see ST-03). Engineering impact: real — a maintainer touching "routing decisions"
storage has to understand and maintain this path even though it does nothing.
Is behavior currently used? NO (proven by 0 rows in both tables + absent JSONL file, in a real
install with 9,075 legacy-store entries over the same period).
Recommended action: DELETE the entire `LineageStore` dual-write mechanism, OR finish the migration
the code comment says was never done (wire the production hook to call `LineageStore.append()`
instead of/in addition to `model_tracking.py:log_routing_decision`) and then delete
`model_tracking.py`. Either resolves the duplication; leaving both is the worst option. Given
DELETE > MERGE preference and that `model_tracking.jsonl` is the one with real data and real
readers, deleting the unused `LineageStore` path (or formally re-scoping it as pure `lineage`-table
analytics for the agentic subsystem, which is a separate concept from routing-decision logging) is
the lower-risk direction.
Behavioral compatibility risk: LOW if deleted (zero real readers/writers found; `recent()`'s JSONL
merge already treats the new store as empty and falls through). Validation required: grep for any
other caller of `LineageStore` beyond `hooks/lineage_integration.py` and tests before deleting, to
rule out a caller I didn't find in the time budget.
Dependencies on other findings: ST-03 (the legacy store this duplicates).

**ID: ST-03**
Category: Storage/Terminology · Severity: MEDIUM-HIGH · Confidence: HIGH
Location: `src/llm_router/model_tracking.py` (502 lines), `src/llm_router/hooks/savings_logger.py:385`,
`src/llm_router/commands/explain_dashboard.py`, `src/llm_router/lineage/lineage_store.py:299-301`
Observation: `model_tracking.jsonl` (9,075 lines, last written today) is called "legacy" and
"the code path the production auto-route hook actually uses today" in the same sentence by
`lineage_store.py:299-301`. It is the sole real, current data source for `explain_dashboard.py`'s
"classifier log" panel and for `LineageStore.recent()`'s fallback path. `hooks/session-end.py:1312`
has an explicit code comment warning that this is "a different store from the `routing_decisions`
table" to prevent the two counts being conflated in the dashboard — i.e., the project is already
aware two counts exist and is actively guarding against conflating them, but has not consolidated
the stores themselves.
Why this matters: "legacy" is the wrong word for the only actively-written, actively-read store for
this concept; the actually-unused one (ST-02) is the one labeled "new canonical." A maintainer
trusting the naming would look in the wrong place.
Recommended action: RENAME/MERGE at minimum (call it what it is — the primary store — in comments
and docs); ideally MERGE into whichever store survives ST-02's resolution.
Is behavior currently used? YES.
Dependencies on other findings: ST-02.

**ID: ST-04**
Category: Storage/Migrations · Severity: MEDIUM-HIGH · Confidence: HIGH
Location: `src/llm_router/migrations/__init__.py` + `migrations/versions/001_create_llm_router_health.py`
(421 lines total, 1 table) vs. `cost.py` (25+ inline `ALTER TABLE` statements), plus 6 more files
independently reimplementing the same "check `PRAGMA table_info`, `ALTER TABLE ADD COLUMN` if
missing" pattern: `session_spend.py`, `agents/session.py`, `lineage/lineage_store.py`,
`hooks/session-end.py`, `semantic_cache.py`, `execution_ledger.py`.
Observation: see §35 above for full detail.
Why this matters: §70/§73's "one source of truth for... persisted field" test fails outright —
adding one column to `usage.db`'s `routing_decisions` table today means editing `cost.py`'s ad-hoc
migration list, NOT the `migrations/` package, which a new contributor would reasonably assume is
the place to do it.
Recommended action: MERGE — either fold the formal `migrations/` package's job into whichever
pattern `cost.py` already uses successfully at scale (it's the one that's actually battle-tested
across 25+ real additions), or migrate `cost.py`'s ALTER list into the formal framework. Either
direction removes one of the two systems; leaving both is the anti-pattern.
Is behavior currently used? Both are used; that is the problem (two live systems for one job).
Validation required: confirm no test asserts the formal `migrations/` package handles `usage.db`
tables (would indicate intent to migrate cost.py's logic there eventually) before choosing a
direction.

**ID: ST-05**
Category: Storage/Semantic duplication ("session") · Severity: MEDIUM-HIGH · Confidence: HIGH
Location: `sessions.db` (`agents/session.py`, table `sessions`, 0 rows), `usage.db.session_summaries`
(0 rows), `~/.llm-router/current_session.json` (1 file, live), `~/.llm-router/session_snapshots/*.json`
(144 files, live, hourly), `usage.db.quota_snapshots` (3,058 rows, has `session_id` column),
`session_spend.py`, `session_store.py`, `session_broker.py`, `ui/session_summary.py`.
Observation: at least 6 independent mechanisms persist something under the "session" concept. Two
of them (`sessions.db.sessions`, `usage.db.session_summaries`) are defined with real schemas
(the former has 17 columns including `budget_cap_usd`, `parent_session_id`, `max_recursion_depth` —
clearly built for the agentic multi-agent subsystem) but contain **zero rows** in a live,
many-months-old install, while `current_session.json` / `session_snapshots/` / `quota_snapshots` are
all actively written.
Why this matters: two of six "session" stores appear to be for a feature (agentic multi-session
budgeting/tracking) that is either not yet wired to any real invocation path, or whose invocation
path never fires in ordinary (non-agentic) usage — worth a definite answer before further building
on it.
Is behavior currently used? `sessions.db` / `session_summaries`: UNCERTAIN-leaning-NO (0 rows is
strong but not proof of zero code path — could be gated behind an agentic feature this particular
install never exercised). The other four: YES.
Recommended action: KEEP the 4 live ones; get a definite answer on `sessions.db`/`session_summaries`
(is the agentic multi-session feature shipped/used by anyone, or is this schema aspirational?) before
deciding DELETE vs KEEP. Flag as UNCERTAIN, not dead — per audit rules, absence of rows in one
install doesn't prove code-path deadness for a feature gated behind agentic/multi-agent usage this
install may simply never have triggered.

**ID: ST-06**
Category: Storage/Semantic duplication ("budget") + false abstraction · Severity: HIGH ·
Confidence: HIGH
Location: `storage/service.py` (`StorageService`, 293 lines), `budget_store.py`, `budget_backend.py`,
`budget_backend_postgres.py`, `budget_envelope.py`, `quota_envelope_routing.py`, `quota_tracker.py`;
data: `budgets.db` (`envelopes`=0 rows, `budget_spend_events`=0 rows).
Observation: `storage/service.py`'s docstring claims `StorageService` is "Single entry point for all
llm_router file I/O," managing "Budgets (JSON)... Audit events (SQLite)... Configuration (YAML)."
`grep -rln StorageService src/llm_router` (excluding tests) returns exactly two files: the service
itself and `budget_store.py`. Every other persisted concept in this audit (usage, sessions, lineage,
semantic cache, result cache, receipts) is written by its own independent module, not through
`StorageService`. Separately, the "budget" concept alone has at least 6 more independent
implementations outside `StorageService` (`budget_backend.py`, `budget_backend_postgres.py`,
`budget_envelope.py`, `quota_envelope_routing.py`, `quota_tracker.py`), and the one SQLite-backed
budget store (`budgets.db`) has zero rows in both its tables in a live, many-months-old install.
Why this matters: `StorageService` is a false "single source of truth" abstraction — its own
docstring overclaims scope relative to its one real caller, which itself appears to sit atop a
budget subsystem with no observed real-world data. This is exactly the §10/§75 "single-impl,
duplicates responsibility, anticipatory abstraction" pattern the brief asks to flag.
Is behavior currently used? `StorageService`/`budget_store.py`/`budgets.db`: UNCERTAIN-leaning-NO
(0 rows, but budget enforcement could be gated behind a feature/env var this install never enabled —
flagging as UNCERTAIN per audit rules rather than asserting dead). The other 5 budget-concept files:
not row-verified in this pass (no dedicated on-disk store found for them beyond `budgets.db`; some
may be pure in-memory/request-scoped logic) — needs a dedicated pass, likely by whichever auditor
owns the routing/policy domain.
Recommended action: (1) fix `StorageService`'s docstring to state its actual scope (budget JSON only,
one caller) — MEDIUM priority doc fix regardless of the bigger question; (2) get a definite answer on
whether ANY budget-enforcement code path is live before consolidating the 6 budget implementations
down to one. Top candidate for synthesis's consolidation ledger.
Dependencies on other findings: overlaps whichever auditor covers policy/budget enforcement logic
(their domain, not mine) — I'm reporting the persistence-layer half only.

**ID: ST-07**
Category: Storage/Security-adjacent · Severity: MEDIUM · Confidence: HIGH (row count),
MEDIUM (interpretation)
Location: `audit.db.audit_events` (hash-chained: `prev_hash`, `hash_hex UNIQUE` columns —
explicitly tamper-evident design), written via `storage/adapters/sqlite_adapter.py` /
`storage/service.py`.
Observation: 0 rows in a live install spanning at least from 19 Aug (file creation) to today.
Why this matters: a tamper-evident audit log that has never recorded an event is either (a) gated
behind a trigger condition (e.g., budget breach, per `StorageService`'s docstring pairing
"Audit events" with the budget subsystem — and `budgets.db` is also 0 rows, which would be
consistent) that this install never hit, or (b) not wired into any real call path at all. Given
ST-06's finding that `StorageService` has exactly one caller (`budget_store.py`), and `budgets.db`
is also empty, hypothesis (a) — audit events fire only on budget-related actions that never
occurred here — is the more likely explanation, but I did not trace `budget_store.py`'s call graph
deeply enough in the time budget to confirm which specific action is supposed to trigger an audit
write.
Recommended action: KEEP the mechanism (tamper-evident hash chain is good practice if it fires), but
DEPRECATE or re-scope depends entirely on the answer to ST-06's "is the budget subsystem live"
question — flagged as UNCERTAIN, dependent on ST-06.
Dependencies on other findings: ST-06.

**ID: ST-08**
Category: Storage/Unbounded growth · Severity: HIGH · Confidence: HIGH
Location: `~/.llm-router/` root (1,113 top-level entries)
Observation: beyond the CTX-04 knowledge-store finding, the root of `~/.llm-router/` itself has
massive per-session/per-agent file proliferation: `last_route_<id>.json` × **813** files,
`transcript_<id>.jsonl` × **172**, `turn_blocks_<id>.json` × 15, `agent_depth_<id>.json` × 12 —
together ~93% of the 1,113 top-level entries. A manual cleanup path exists
(`src/llm_router/commands/gc.py`, which explicitly names `last_route_` as one of the "small
per-session shard files" it collects via a TTL, `collect_stale(root, ttl_days)`), but I found no
evidence of it running automatically (no cron/scheduled-hook registration found for it in the time
available — this is UNCERTAIN, not proven absent).
Why this matters: whether or not `gc.py` is scheduled, this real install has accumulated 813
`last_route_*.json` files without them being collected, meaning either the ttl_days default is very
long, gc.py has never been run, or it's not being invoked automatically — practically, unbounded
growth is happening today on this machine.
Recommended action: confirm whether `llm-router gc`/`commands/gc.py` is registered as an automatic
hook (e.g., on session-end) anywhere; if not, that is the single highest-leverage fix for this
finding — wire it in rather than build a new mechanism.
Validation required: check `hooks/session-end.py` and the install manifest
(`~/.llm-router/install-manifest.json`) for a `gc` invocation.

**ID: ST-09** (methodology note, not a product finding)
`sqlite3 -readonly` cannot open any `~/.llm-router/*.db` file in this sandboxed bash environment,
even against a plain copy under `/tmp` (confirmed: `cp` succeeds, `sqlite3 -readonly <copy>` still
errors "unable to open database file"; the same copy opens fine with `sqlite3 <copy>` — no
`-readonly` flag). Future auditors on this domain should copy DB files to the scratchpad and query
without `-readonly` rather than losing time on the flag. Recorded here since no CLAUDE.md exists in
this worktree to carry the lesson forward as the brief expected.

---

## Top items for synthesis (best 5-10 candidates)

1. **ST-01** (routing.db doc/code mismatch) — best "misleading user-facing troubleshooting step"
   candidate for the global Top-10 correctness/doc-problems list. Concrete, cheap fix, real user
   impact.
2. **ST-02** (dead LineageStore dual-write system, ~700 LOC, 0 rows in production) — best "delete a
   whole concept" candidate for the deletion ledger. High complexity-removed, near-zero risk.
3. **ST-06** (StorageService false single-entry-point + 6-way budget duplication) — best
   consolidation-ledger candidate; also a do-not-trust-this-abstraction flag for anyone building on
   `StorageService` believing its docstring.
4. **ST-04** (two uncoordinated schema-migration systems) — direct hit on the brief's own §71/§73
   "add one persisted field" maintainer test; formal `migrations/` package covers 1 table while the
   real, live schema evolves through `cost.py`'s 25+ ad-hoc `ALTER TABLE`s.
5. **CTX-04** (knowledge store 99.5% test debris, 300 MB / 12,694 dirs) — best "unbounded growth"
   candidate; likely root cause is test fixtures not honoring an isolated `LLM_ROUTER_HOME`, which
   if fixed once eliminates the majority of this domain's storage bloat.
6. **ST-08** (813 uncollected per-session shard files despite an existing `gc.py` TTL command) —
   good "the mechanism exists but isn't wired in" pattern, cheap to fix (register the existing
   command as a hook) rather than build anything new.
7. **ST-03 / ST-02 pair** — best "one source of truth" naming-inversion example: the store called
   "legacy" is the one with all the real data; the store called "canonical" has none.
8. **CTX-01** — best do-not-change-register candidate: the `context_injection` choke point +
   enforcing test (`test_okf_choke_point.py`) is a well-designed, test-locked pattern that correctly
   fixed a real historical incident (cross-project contamination via `$HOME` cwd); do not refactor
   this away in the name of simplification.
9. **ST-07** (zero-row tamper-evident audit log) — smaller, but worth a security-domain auditor
   cross-check: is this a dormant feature or a broken trigger path for something meant to be
   security-relevant?
10. **CTX-02** — trivial doc-fix but a good illustration for the "comments contradicting code"
    category (§39) — cheap, safe, immediate cleanup item for any phase-0 pass.

---

## What I could not complete (be honest about gaps)

- Did not measure real-world (non-clean-HOME) context-injection token cost against the actual 300 MB
  knowledge store, due to uncertainty about `okf.py`'s side effects and the read-only constraint on
  `~/.llm-router` (see CTX-03 gap note).
- Did not fully trace `receipts.db` (14,813 rows, clearly live) to a specific owner module beyond
  confirming its existence — ran low on budget before tracing its writer/reader; flagging for
  synthesis in case another auditor's domain already covers it (likely cost/accounting, §17).
- ST-05/ST-06/ST-07's "is this feature live at all" questions are UNCERTAIN by design (row count in
  one install is evidence, not proof, per §1's rules) — did not chase them to PROVEN DEAD given time
  budget; flagged explicitly rather than guessed.
- Did not verify whether `discovery.json.bak-*` / `vision_models.json.bak-*` backups are written via
  atomic rename or copy-then-overwrite (relevant to §16's "atomic writes" ask) — found the backup
  files but did not locate/read the writer function in the time available.
