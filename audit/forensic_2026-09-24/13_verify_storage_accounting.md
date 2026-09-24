# Domain 13 — Adversarial Verification of Storage/Accounting/Perf/Error Findings

Verifier: independent pass, no prior context from the original auditors. Baseline: worktree
`llm-router-forensic` @ `3c96d23` (confirmed via `git log -1`). All Python run via
`HOME=$(mktemp -d) PYTHONPATH=.../src /Users/yaliandrona/Projects/llm-router/.venv/bin/python`.
All `~/.llm-router/*.db` reads were done against copies in a scratch dir; no write ever touched
the real files. No paid APIs, no ollama, no full pytest run.

Method for every finding below: re-derive the number independently (own grep/AST scan/live DB
query), not by re-reading the original auditor's command. Where my number differs from the
claim, both are reported.

---

## ST-01 — `routing.db` orphaned, docs still tell users to delete it

**Verdict: CONFIRMED. Severity: HIGH (unchanged).**

- `grep -rln 'routing\.db' src/` → **zero** hits. `usage.db` → **55** hits, including
  `config.py:402: return state_path("usage.db")` — the actual configured path.
- `guide/TROUBLESHOOTING.md` lines 284, 287, 293, 548 and `guide/HOST_SUPPORT_MATRIX.md:344`
  reference `~/.llm-router/routing.db` (5 lines across 2 files) — including an explicit
  documented "fix": `rm -f ~/.llm-router/routing.db`.
- Live `routing.db` copy: all 13 tables 0 rows except `provenance_meta`=1 — exact match to the
  claim. Table set is identical to `usage.db`'s 13 tables, consistent with a rename that updated
  code but not docs.

No basis to weaken this; it is a real, cheap-to-fix, user-facing documentation defect.

---

## ST-02 — Dead `LineageStore` dual-write system

**Verdict: CONFIRMED, on firmer evidence than the original finding used. Severity: HIGH (unchanged).**

- `routing_lineage.db` copy: `routing_decisions`=0, `lineage`=0 rows. `routing_lineage.jsonl`
  does not exist on disk. Matches the claim exactly.
- **New evidence the original finding missed**: `hooks/session-start.py:462-463` calls
  `lineage_integration.init_session_lineage()` on every SessionStart, which **unconditionally
  deletes** `routing_lineage.db` and `routing_lineage.jsonl` before recreating an empty schema.
  This means the 0-row observation in one install is not fully dispositive by itself (a session
  boundary would wipe real data too) — however:
- Direct call-graph check closes that gap: the only function that calls `LineageStore.append()`
  is `lineage/decision_logger.py:log_routing_decision()` (line 122). Grepping every call site of
  a function literally named `log_routing_decision` across `src/` turns up **five distinct
  functions with that name** in different modules (`cost.py`, `model_tracking.py`,
  `routing_hints.py`, `hooks/response-router.py`, `lineage/decision_logger.py`) — a naming
  collision, not five callers of one function. The production call site
  (`router.py:2292: await cost.log_routing_decision(...)`) calls **cost.py's** function, not the
  lineage one. `lineage/decision_logger.log_routing_decision` has **zero** production callers.
  So the write path is provably unused regardless of the session-wipe behavior.
- Additional consequence not in the original finding: `hooks/lineage_integration.py`'s
  `get_session_routing_report()` / `get_waste_alerts()` / `format_routing_section()` (used by
  `hooks/session-end.py:2483-2485` for the session-end "ROUTING EFFICIENCY REPORT") all early-
  return empty because `LineageQuery.get_recent(limit=1)` is always empty — an entire session-end
  UI feature is silently dead as a direct consequence of ST-02, not just ~700 LOC of inert storage
  code.

---

## ST-04 — Two uncoordinated schema-migration systems

**Verdict: CONFIRMED. Severity: MEDIUM-HIGH (unchanged). One mechanism detail corrected.**

- `migrations/__init__.py` + `versions/001_create_llm_router_health.py` = 421 lines, 1 table —
  exact match.
- `cost.py`: **45** literal `ALTER TABLE` statements (claim: "25+" — conservative, still true,
  actual count is higher).
- Correction: the claim describes cost.py's idempotency check as "PRAGMA table_info → ALTER
  TABLE ADD COLUMN if missing." That is not what cost.py does — it wraps each `ALTER TABLE` in a
  handler that catches `sqlite3.OperationalError` and checks for `"duplicate column name"` in the
  message (cost.py:797, 837, 860), a different but equally ad hoc idempotency mechanism.
  `session_spend.py`, `hooks/session-end.py`, and `execution_ledger.py` do independently
  duplicate `ALTER TABLE ... ADD COLUMN` migrations, confirming the multi-file duplication claim;
  the literal string `"PRAGMA table_info"` appears in only 6 files (`dashboard_data.py,
  test_delta.py, attribution.py, semantic_cache.py, agents/session.py, lineage/lineage_store.py`),
  not the exact 8 named. The substantive finding (schema evolution happens outside the formal
  `migrations/` package, in several independently-reimplemented ways) stands; the specific
  mechanism-per-file list is imprecise.

---

## ST-06 — `StorageService` false single-entry-point abstraction

**Verdict: CONFIRMED exactly. Severity: HIGH (unchanged).**

- `grep -rln StorageService src/llm_router` (excluding tests) → exactly 2 files:
  `storage/service.py`, `budget_store.py`. Matches the claim precisely.
- `budgets.db`: `envelopes`=0, `budget_spend_events`=0. `audit.db.audit_events`=0.
  `sessions.db.sessions`=0. All confirmed exactly.

---

## CTX-04 — Knowledge store is ~99.5% test-fixture debris

**Verdict: CONFIRMED, near-exact recount (small drift = continued growth since the audit ran).
Severity: HIGH (unchanged, arguably understated).**

| Metric | Original claim | My recount |
|---|---:|---:|
| `du -sh knowledge/` | ~298-300 MB | 300 MB |
| Total dirs under `knowledge/` | 12,694 | 12,709 |
| Dirs directly under `knowledge/projects/` | 5,404 | 5,406 |
| Test-named (`test_*`, `tmp*`, `fixture*`) | 2,451 | 2,451 (exact) |
| `repo-<8hex>` named | 2,926 | 2,926 (exact) |
| Neither pattern ("genuine") | 27 | 29 |

The +15/+2 drift is consistent with ongoing test runs still writing into the real store between
the original audit and this verification pass — itself corroborating the "unbounded growth,
still happening" framing. I additionally sampled the 29 "genuine" directories: several (e.g.
`llm_router-agentic-probe-*` × 10+) are themselves agentic-probe test-fixture artifacts, not real
user checkouts — meaning the true fraction of genuine user data is *lower* than even the "27/29"
figure implies, not higher.
`okf.gc_store()` has exactly one caller repo-wide (`commands/okf.py:122`, the `llm-router okf gc`
CLI command) — confirmed no automatic/scheduled invocation exists.

---

## ACC-01 — Unfiltered headline "saved" figure

**Verdict: PARTIALLY CONFIRMED. Core defect real; the headline arithmetic in the writeup is wrong.
Severity: HIGH (core defect) — but correct the numbers before this ships anywhere.**

Core defect confirmed: `grep -n "is_simulated\|production_only" dashboard_data.py` → zero hits.
`claude_usage.cost_saved_usd` sums to $366.90 live, of which 37,871/37,878 rows (99.98%) have
`is_simulated IS NULL` (provenance never established) and only 7 rows ($0.105) are confirmed
production (`is_simulated=0`). This part reproduces exactly.

**What does not reproduce**: I ran `dashboard_data.query_window("lifetime")` against a live copy
of `usage.db` (via `LLM_ROUTER_HOME` override, no write to the real file). It returns:

```
WindowTotals(saved_usd=372.656735, unverified_saved_usd=110.910668, unverified_calls=7958, ...)
```

`saved_usd` and `unverified_saved_usd` are **two separate fields**, not one summed scalar. The
"$495.04 unfiltered lifetime total" and "4,500×" gap in the writeup are the *auditor's own manual
addition* of the table's rows (usage + claude_usage + unverified + verified), not a number the
code ever actually produces or a user ever actually sees. `ui/status_premium.py:135-142`
(`cli_status_premium`, the actual `llm-router status` code path) prints `totals.saved_usd` as the
headline **and separately** prints `savings.unverified_note(totals.unverified_saved_usd,
totals.unverified_calls)` as a distinct dim line ("+ $110.91 unverified, n=7958") — i.e. the
commit's verified/unverified split **is** visibly surfaced to the user, contradicting the
writeup's claim that "no indication a materially different number exists."

Separately, the "usage table = $17.23 (Opus-rate recompute)" figure is **wrong**. Live run of the
same code gives `usage: saved_usd=5.75952` for the identical rows. Reason: `dashboard_data.py`'s
`_BASELINE_MODEL = _pricing.savings_baseline_model()` currently resolves to `claude-opus-5` at
$5/M input, $25/M output — not the "$15/$75" Opus-3 rate that a *stale code comment two lines
above* (`dashboard_data.py:313-314`) still describes. Manually computing $17.23 from that stale
comment (462,719 in-tok × $15/M + 137,837 out-tok × $75/M = $17.28) reproduces the writeup's
number almost exactly — strong evidence the original figure was computed by hand from the
comment's literal rates rather than by running the code. `cost.py`'s own
`CHECKPOINT_2026-09-24.md:159-166` independently states the real number: "`llm-router status`
all-time went $483.49 → $372.58 + $110.91 unverified. The remaining $372.58 comes from... Opus
rates" — $372.58 matches my $372.66 (small drift = 7 more rows since), confirming $372.58/66,
not $495.04, is the actual single figure in play.

**Corrected comparison**: real headline `saved_usd` ≈ **$372.66** (still ~98% built from
`claude_usage`'s unfiltered $366.90) vs. the codebase's own canonical
`get_realized_savings("all","all")` = **$0.10546** (confirmed live, matches the "~$0.11" claim
exactly) — a ≈3,534× gap, not 4,500×, and the unverified $110.91 is not folded into either side.
Still a severe, real, HIGH-severity defect (the provenance filter genuinely is missing from 3 of
4 tables) — but the specific $495.04/4,500× headline numbers should not be published as-is.

---

## ACC-02 — 96 rows with a sentinel value pair

**Verdict: CONFIRMED exactly.**

`SELECT model, count(*), sum(cost_saved_usd) FROM claude_usage WHERE tokens_used=200000 AND
cost_saved_usd=3.0 GROUP BY model` → `claude/claude-fable-5`: 66 rows, $198.00;
`openai/gpt-5.4`: 30 rows, $90.00. Total 96 rows, $288.00, all dated 2026-09-04, all
`is_simulated IS NULL`. Matches every number in the claim exactly.

---

## ACC-03 — Checkpoint mis-names the dominant contributor

**Verdict: CONFIRMED exactly.** Direct read of `audit/CHECKPOINT_2026-09-24.md:159-166`: "The
remaining $372.58 comes from the legacy `usage` table recomputed at Opus rates." Measured live,
the `usage` table itself contributes only $5.76-$17 (see ACC-01 correction) while `claude_usage`
(unnamed by the checkpoint) contributes $366.90 — 98%+ of the total. The checkpoint's gloss is
confirmed imprecise.

---

## ACC-06 — `mcp_session_dashboard` mislabeled `canonical=True`

**Verdict: CONFIRMED exactly.** `tools/admin.py:948-988` (`llm_session_dashboard`) calls
`cost.get_realized_savings()` directly and never references `canonical_savings`,
`CanonicalSavings.headline()`, or `_under_subscription()`. `savings.py:334`:
`Surface("mcp_session_dashboard", "tools/admin.py:963", True)` — exact line-number match.
A subscription user calling this MCP tool gets an unqualified `$X.XXXX` figure with no
subscription caveat, as claimed.

---

## PERF-01 — `session_spend.py`'s short-timeout, swallowed-exception write

**Verdict: PARTIALLY CONFIRMED. Downgrade severity HIGH → MEDIUM. The "15x shorter than the
documented standard, via sqlite_wal.enable_wal" evidence chain is factually wrong; the underlying
architectural risk is real but smaller than described.**

Confirmed as described: `session_spend.py:327` opens
`sqlite3.connect(str(db_path), timeout=2.0)` to write into the shared `usage.db`
`claude_usage` table, and the call is wrapped in `except Exception: pass`
(`session_spend.py:307-310`, no log, no `failopen.record`). `record_reclaimed()` (the caller) is
genuinely hot — called from `router.py:2080` and `hooks/savings_logger.py:288`, i.e. on real
routed-call paths, not a rare code path.

**What is wrong**: the claim states `cost._get_db()` "sets a 30,000ms busy_timeout through
`sqlite_wal.enable_wal`." Direct read of `cost.py:891-935` shows `_get_db()` does **not** call
`sqlite_wal.enable_wal()` at all — it sets its own inline `PRAGMA busy_timeout = 5000` (5 seconds,
not 30). `sqlite_wal.enable_wal()`'s `DEFAULT_BUSY_TIMEOUT_MS = 30_000` is real, but it is used by
`lineage_store.py`, `execution_ledger.py`, `storage/adapters/sqlite_adapter.py`,
`semantic/store.py`, `semantic/traces.py` — none of which write `usage.db`. `dashboard/tui.py`'s
own code comment states this plainly: "`enable_wal` ... was adopted by only 3 of 9 sites." So the
actual comparable timeout on `usage.db` (via `cost._get_db()`, the connection most other writers
use) is 5,000ms, making `session_spend.py`'s 2,000ms **2.5× shorter**, not 15×. The "already got
bitten by 66/2400 events lost" evidence cited from `sqlite_wal.py`'s docstring documents a
different connection path's cold-start race, not a measured loss on this specific write.

The directional concern (a separate, ad hoc, short-timeout, silently-swallowing raw connection
into a heavily-contended shared file, with zero observability) is still real and worth fixing —
but it is one of several raw `sqlite3.connect()` sites at even shorter timeouts
(`hooks/auto-route.py:463` and `hooks/agent-route.py:584` both use `timeout=1`), so it is not
uniquely dangerous, and the specific "15x" framing and its cited mechanism do not hold up.
Recommend MEDIUM, not HIGH, until an actual concurrent-load reproduction is run (which neither
the original finding nor this verification did).

---

## ERR-01 — Broad-except silent-swallow population

**Verdict: CONFIRMED on the headline numbers; two supporting sub-statistics don't reproduce, and
the correction makes the finding slightly worse, not better. Severity: HIGH (unchanged).**

Independent AST walk (`ast.ExceptHandler`, same definition of "broad": bare/`Exception`/tuple
containing `Exception`) over `src/llm_router` (415 files):

| Metric | Claim | My recount |
|---|---:|---:|
| Broad except handlers | 1,020 | **1,020 (exact)** |
| ...body is bare `pass` | 270 (26.5%) | **270 (26.5%, exact)** |
| ...handler directly calls `failopen.record(` (literal-text match) | 61 (6.0%) | **61 (exact, but see below)** |
| Grep-wide `failopen.record(` call sites | 162 | **116** repo-wide literal grep; **88** via an alias-aware AST scan restricted to confirmed `failopen` import aliases (src/ only) |
| Distinct fail-open codes | 59 | **85** (alias-aware AST scan, unique string literal first-arg) |

The 61-in-handler number matches because both the original count and mine used a literal
`"failopen.record("` substring match — which misses the common `from llm_router import failopen
as _fo` alias used throughout `cost.py`, `hooks/auto-route.py`, `router.py`, etc. Redoing the
handler-body scan alias-aware (matching a `Call` to `.record()` on *any* confirmed failopen
alias, not just the literal name) finds **83** handlers directly calling it, not 61. The 162
grep-wide figure does not reproduce under any grep or AST method I tried (repo-wide literal grep:
116; alias-aware AST, src/ only: 88); I cannot identify what produced 162 and flag it as
unreproduced/likely erroneous.

Net effect on the finding: correcting for the alias blind spot gives a **true coverage ceiling of
~88/1,020 ≈ 8.6%**, below the original's claimed "~16%" ceiling — i.e. the corrected numbers make
the underlying problem (most broad excepts are uninstrumented) *slightly worse* than claimed, not
better. The core conclusion (`failopen.py` is well-designed but reaches a small minority of the
codebase's broad excepts) is CONFIRMED; the specific "162" and "59" figures should not be
republished without correction.

---

## OBS-01 — `savings_log_path` reimplemented in 6 modules

**Verdict: CONFIRMED exactly.**

`def _savings_log_path(` / `def _savings_log_file(` found in exactly 6 files:
`hooks/opencode-post-tool.py:40`, `hooks/savings_logger.py:171`, `hooks/codex-post-tool.py:42`,
`hooks/gemini-cli-post-tool.py:40`, `hooks/session-end.py:66`, `hooks/usage-refresh.py:98` — vs.
the canonical `cost.py:198: def savings_log_path()`. `grep -rn "AC-5"` → exactly 2 hits,
`cost.py:3382` and `hooks/session-end.py:340`, both describing the same dual-writer race from
each side, as claimed. No corrections needed.

---

## Summary table

| Finding | Verdict | Severity (corrected if changed) |
|---|---|---|
| ST-01 | CONFIRMED | HIGH |
| ST-02 | CONFIRMED (stronger evidence: call-graph proves zero production callers, independent of session-wipe behavior) | HIGH |
| ST-04 | CONFIRMED (mechanism detail for cost.py corrected: OperationalError catch, not PRAGMA table_info) | MEDIUM-HIGH |
| ST-06 | CONFIRMED exactly | HIGH |
| CTX-04 | CONFIRMED (near-exact recount; true "genuine data" fraction is even lower) | HIGH |
| ACC-01 | PARTIALLY CONFIRMED (provenance-filter gap real; $495.04/4,500× headline is the auditor's own arithmetic, not what the code returns or a user sees — real gap is $372.66 vs $0.11, ≈3,534×, and the unverified split IS visibly disclosed) | HIGH (core defect) |
| ACC-02 | CONFIRMED exactly | MEDIUM |
| ACC-03 | CONFIRMED exactly | MEDIUM |
| ACC-06 | CONFIRMED exactly | MEDIUM |
| PERF-01 | PARTIALLY CONFIRMED (write pattern and swallow are real; "30,000ms via sqlite_wal.enable_wal" is factually wrong — cost.py uses its own inline 5,000ms, so the real gap is 2.5×, not 15×) | MEDIUM (down from HIGH) |
| ERR-01 | CONFIRMED (1,020/270/26.5% exact; "162 call sites"/"59 codes" don't reproduce — true numbers are 88/85, making coverage ~8.6%, worse not better than claimed) | HIGH |
| OBS-01 | CONFIRMED exactly | HIGH |
